"""Reproducible Demucs weights, cached in the standard Hugging Face cache.

These are the upstream Demucs author's safetensors exports. Each release pins
an immutable repository commit and the SHA-256 of every file we interpret.
The hashes come from the repository's LFS records (weights) and the downloaded
YAML bytes (bag definitions). To update a model, review and update both pins;
never fall back to a moving revision or the legacy pickle download path.

Provenance:
https://huggingface.co/adefossez/HTDemucs
https://huggingface.co/adefossez/HTDemucs-ft
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any


@dataclass(frozen=True)
class ModelRelease:
    repo_id: str
    revision: str
    config_sha256: str
    # The order is significant: HTDemucs-ft assigns each member to one stem.
    members: tuple[tuple[str, str], ...]


MODEL_RELEASES = MappingProxyType({
    "htdemucs": ModelRelease(
        repo_id="adefossez/HTDemucs",
        revision="cbc8a9b1a87023b7fd74e7b3412e6321c0eab003",
        config_sha256="239c445d0b14454d541ad8bd9bb271c9e536d267e8a4625208744cbb2e7bb66c",
        members=(
            ("955717e8", "d9fa14133cfcc034a6758923bb3a8ca9f8dfd0b582134643bbf83f72c17576dd"),
        ),
    ),
    "htdemucs_ft": ModelRelease(
        repo_id="adefossez/HTDemucs-ft",
        revision="d74ac89c3a1e874fc78f152555cf4d8533f06cd4",
        config_sha256="69470b8c1bbd674437b51bc9fb491327a10ab0396b702c93389b9cf750016346",
        members=(
            ("f7e0c4bc", "2c85ab3c62dd6edd8e0b965e38b16fd1cdde357cc25de6b6bc9ce7c83f60925f"),
            ("d12395a8", "5b01a97567ae9a3178a6236fb520251045c03eb8834bc8c24a4eec11d6c8fb56"),
            ("92cfc3b6", "a241863551f30d01c42bd7b97da40839922ead3acb0f1fcab25682f55b4eeb59"),
            ("04573f0d", "68854b0d7c2b3274723b5761f6fd9f5aec5f1bcd3f0de7c1669546fdb7871b7c"),
        ),
    ),
})


def _download_verified(
    release: ModelRelease, filename: str, sha256: str, *, local_files_only: bool = False,
) -> Path:
    from huggingface_hub import hf_hub_download

    path = Path(hf_hub_download(
        repo_id=release.repo_id,
        filename=filename,
        revision=release.revision,
        local_files_only=local_files_only,
    ))
    with path.open("rb") as source:
        actual = hashlib.file_digest(source, "sha256").hexdigest()
    if actual != sha256:
        raise RuntimeError(
            f"Model artifact {filename!r} failed SHA-256 verification. "
            f"Remove the corrupt cache file {path.resolve()} and retry."
        )
    return path


def _release(name: str) -> ModelRelease:
    try:
        return MODEL_RELEASES[name]
    except KeyError:
        raise ValueError(
            f"Unknown model {name!r}. Supported: {tuple(MODEL_RELEASES)}"
        ) from None


def _read_bag(name: str, config: Path, release: ModelRelease) -> dict[str, Any]:
    import yaml

    with config.open(encoding="utf-8") as source:
        bag = yaml.safe_load(source)
    expected = [signature for signature, _ in release.members]
    if not isinstance(bag, dict) or bag.get("models") != expected:
        raise RuntimeError(f"Model bag {name!r} does not match its pinned members.")
    return bag


def fetch_model_files(name: str = "htdemucs", *, local_files_only: bool = False) -> dict[str, Path]:
    """Download and verify a model without importing torch or selecting a GPU.

    Returns filenames mapped to their paths in the standard Hub cache (honoring
    HF_HOME/HF_HUB_CACHE). Both fetching and loading reject unknown model names.
    With local_files_only=True, missing files fail without a network request.
    """
    release = _release(name)
    filename = f"{name}.yaml"
    config = _download_verified(release, filename, release.config_sha256, local_files_only=local_files_only)
    _read_bag(name, config, release)
    paths = {filename: config}
    for signature, digest in release.members:
        filename = f"{signature}.safetensors"
        paths[filename] = _download_verified(release, filename, digest, local_files_only=local_files_only)
    return paths


def load_model(name: str, *, local_files_only: bool = False) -> Any:
    """Load one supported bag using verified safetensors and pinned metadata.

    Hub/network/cache failures propagate to the caller. In particular, a failed
    download must never silently select different weights or a pickle loader.
    """
    release = _release(name)
    paths = fetch_model_files(name, local_files_only=local_files_only)
    bag = _read_bag(name, paths[f"{name}.yaml"], release)

    from demucs.apply import BagOfModels
    from demucs.hf import load_safetensors_model

    models = [
        load_safetensors_model(paths[f"{sig}.safetensors"])
        for sig, _ in release.members
    ]
    return BagOfModels(models, bag.get("weights"), bag.get("segment"))
