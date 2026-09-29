"""Runtime selection and model provenance contracts; no GPU or network needed."""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from nomusic.engines import model_store
from nomusic.engines.mlx_engine import _pick_device


def _torch(*, mps=False, cuda=False, capability=(8, 6), archs=("sm_80",)):
    return SimpleNamespace(
        backends=SimpleNamespace(mps=SimpleNamespace(is_available=lambda: mps)),
        cuda=SimpleNamespace(
            is_available=lambda: cuda,
            get_device_capability=lambda: capability,
            get_arch_list=lambda: archs,
        ),
    )


@pytest.mark.parametrize("requested,torch,expected", [
    ("auto", _torch(mps=True, cuda=True), "mps"),
    ("auto", _torch(cuda=True), "cuda"),
    ("auto", _torch(cuda=True, capability=(6, 1)), "cpu"),
    ("auto", _torch(), "cpu"),
    ("cuda", _torch(mps=True, cuda=True), "cuda"),
    ("mps", _torch(mps=True), "mps"),
])
def test_device_selection(monkeypatch, requested, torch, expected):
    monkeypatch.setenv("NOMUSIC_DEVICE", requested)
    monkeypatch.setitem(sys.modules, "torch", torch)
    assert _pick_device() == expected


def test_cpu_override_does_not_probe_accelerators(monkeypatch):
    monkeypatch.setenv("NOMUSIC_DEVICE", "cpu")
    monkeypatch.setitem(sys.modules, "torch", None)
    assert _pick_device() == "cpu"


def test_unset_device_preserves_auto_cpu_fallback(monkeypatch):
    monkeypatch.delenv("NOMUSIC_DEVICE", raising=False)
    monkeypatch.setitem(sys.modules, "torch", None)
    assert _pick_device() == "cpu"


@pytest.mark.parametrize("requested", ["", "gpu", "cuda:0"])
def test_invalid_device_is_actionable(monkeypatch, requested):
    monkeypatch.setenv("NOMUSIC_DEVICE", requested)
    with pytest.raises(ValueError, match="choose auto, cpu, mps, or cuda"):
        _pick_device()


@pytest.mark.parametrize("requested,torch", [
    ("mps", _torch(cuda=True)),
    ("cuda", _torch(mps=True)),
    ("cuda", _torch(cuda=True, capability=(6, 1))),
    ("cuda", None),
])
def test_forced_unavailable_device_fails_instead_of_falling_back(
    monkeypatch, requested, torch,
):
    monkeypatch.setenv("NOMUSIC_DEVICE", requested)
    monkeypatch.setitem(sys.modules, "torch", torch)
    with pytest.raises(RuntimeError, match="NOMUSIC_DEVICE=cpu"):
        _pick_device()


@pytest.fixture(params=["htdemucs", "htdemucs_ft"])
def hub(monkeypatch, tmp_path, request):
    """Fake only the hub transport/model construction; exercise real validation."""
    name = request.param
    release = model_store.MODEL_RELEASES[name]
    signatures = [signature for signature, _ in release.members]
    weights = [
        [int(row == col) for col in range(len(signatures))]
        for row in range(len(signatures))
    ]
    bag = {"models": signatures, "weights": weights, "segment": 7}
    payloads = {f"{name}.yaml": json.dumps(bag).encode()}
    payloads.update({f"{sig}.safetensors": f"fake weights {sig}".encode() for sig in signatures})
    digest = lambda filename: hashlib.sha256(payloads[filename]).hexdigest()
    test_release = replace(
        release,
        config_sha256=digest(f"{name}.yaml"),
        members=tuple((sig, digest(f"{sig}.safetensors")) for sig in signatures),
    )
    monkeypatch.setattr(model_store, "MODEL_RELEASES", {name: test_release})
    paths = {}
    for filename, data in payloads.items():
        paths[filename] = tmp_path / filename
        paths[filename].write_bytes(data)
    calls = []
    loaded = []

    def download(**kwargs):
        calls.append(kwargs)
        return str(paths[kwargs["filename"]])

    def load(path):
        loaded.append(path.name)
        return path.stem

    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(hf_hub_download=download))
    monkeypatch.setitem(sys.modules, "demucs.hf", SimpleNamespace(load_safetensors_model=load))
    monkeypatch.setitem(sys.modules, "demucs.apply", SimpleNamespace(
        BagOfModels=lambda models, weights, segment: (models, weights, segment),
    ))
    return SimpleNamespace(
        name=name, release=test_release, paths=paths, calls=calls, loaded=loaded,
        signatures=signatures, bag=bag,
    )


def test_fetch_uses_immutable_revision_for_every_file_without_loading_models(hub):
    assert model_store.fetch_model_files(hub.name) == hub.paths
    assert hub.loaded == []
    assert hub.calls == [
        {"repo_id": hub.release.repo_id, "filename": filename, "revision": hub.release.revision}
        for filename in hub.paths
    ]


def test_loaded_bag_preserves_member_order_and_stem_weights(hub):
    model = model_store.load_model(hub.name)
    assert model == (hub.signatures, hub.bag["weights"], hub.bag["segment"])
    assert hub.loaded == [f"{sig}.safetensors" for sig in hub.signatures]


@pytest.mark.parametrize("name", ["other/htdemucs", "../htdemucs", "mdx_extra", ""])
def test_unknown_model_cannot_request_other_hub_artifacts(hub, name):
    with pytest.raises(ValueError, match="Unknown model"):
        model_store.load_model(name)
    assert hub.calls == []
    assert hub.loaded == []


@pytest.mark.parametrize("artifact", ["config", "weights"])
def test_corrupt_files_are_rejected_before_model_deserialization(hub, artifact):
    filename = f"{hub.name}.yaml" if artifact == "config" else f"{hub.signatures[-1]}.safetensors"
    hub.paths[filename].write_bytes(b"corrupt cache contents")
    with pytest.raises(RuntimeError, match="failed SHA-256 verification"):
        model_store.load_model(hub.name)
    assert hub.loaded == []


def test_bag_cannot_choose_unpinned_members(monkeypatch, hub):
    path = hub.paths[f"{hub.name}.yaml"]
    path.write_text('models: ["../../unexpected"]\n')
    # A future pin update must update the explicit member manifest as well.
    updated = replace(hub.release, config_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    monkeypatch.setattr(model_store, "MODEL_RELEASES", {hub.name: updated})
    with pytest.raises(RuntimeError, match="does not match its pinned members"):
        model_store.fetch_model_files(hub.name)
    assert len(hub.calls) == 1
    assert hub.loaded == []


def test_download_failure_propagates_without_trying_other_weights(monkeypatch, hub):
    failure = OSError("hub unavailable")

    def unavailable(**kwargs):
        raise failure

    monkeypatch.setitem(sys.modules, "huggingface_hub", SimpleNamespace(hf_hub_download=unavailable))
    with pytest.raises(OSError) as error:
        model_store.load_model(hub.name)
    assert error.value is failure
    assert hub.loaded == []
