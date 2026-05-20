"""Apple Silicon source-separation engine.

The class name reads "MLX" because that's the strategic backend we want; the
*current* implementation runs htdemucs via the upstream ``demucs`` PyTorch
package on Apple's MPS device, which is what's stable on M-series silicon
today. Swapping in a real MLX backend later means replacing the
``_make_separator`` factory below — nothing else in the codebase needs to
change.

Why the indirection: source separation libraries change their APIs frequently.
Keeping the boundary at ``_make_separator`` lets tests stub the engine without
loading torch, and lets us pin a concrete backend per checkout.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from .base import DEMUCS_STEMS, Engine, EngineCapabilities, SeparationResult

log = logging.getLogger(__name__)

# Models known to demucs. ``htdemucs`` is the project default: ~80 MB, 9.0 dB
# SDR, and the fastest of the four-source models on M-series silicon.
# ``htdemucs_ft`` is more accurate but ~4x slower; never selected by default.
_SUPPORTED_MODELS: tuple[str, ...] = (
    "htdemucs",
    "htdemucs_ft",
    "mdx_extra",
    "mdx_extra_q",
)
_DEFAULT_MODEL = "htdemucs"


class MLXEngine(Engine):
    def __init__(
        self,
        separator_factory: Callable[[str, str], Any] | None = None,
    ) -> None:
        # Cached separator keyed by model name; demucs loads weights lazily and
        # we only want to pay that cost once per process.
        self._separators: dict[str, Any] = {}
        self._factory = separator_factory or _make_separator
        self._device = _pick_device()

    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            name="mlx",
            device=f"{self._device} (via demucs)",
            supported_models=_SUPPORTED_MODELS,
            default_model=_DEFAULT_MODEL,
            supported_stems=DEMUCS_STEMS,
        )

    def warmup(self) -> None:
        self._ensure_loaded(_DEFAULT_MODEL)

    def separate(
        self,
        audio_path: Path,
        out_dir: Path,
        *,
        model: str | None = None,
    ) -> SeparationResult:
        model_name = model or _DEFAULT_MODEL
        if model_name not in _SUPPORTED_MODELS:
            raise ValueError(
                f"Unknown model {model_name!r}. Supported: {_SUPPORTED_MODELS}"
            )

        bundle = self._ensure_loaded(model_name)
        out_dir.mkdir(parents=True, exist_ok=True)

        import soundfile as sf
        import torch
        from demucs.apply import apply_model
        from demucs.audio import AudioFile, convert_audio

        log.info(
            "Separating %s with %s on %s",
            audio_path.name,
            model_name,
            self._device,
        )

        demucs_model = bundle.model
        sample_rate = int(demucs_model.samplerate)
        channels = int(demucs_model.audio_channels)
        sources: list[str] = list(demucs_model.sources)

        # Load and resample to whatever the model expects (htdemucs: 44.1 kHz
        # stereo). ``AudioFile`` decodes via ffmpeg under the hood.
        wav = AudioFile(audio_path).read(
            streams=0, samplerate=sample_rate, channels=channels
        )
        # Normalize to mean 0 / unit variance per channel — demucs's own
        # separate.py does the same, and it noticeably improves quality on
        # quiet inputs.
        ref = wav.mean(0)
        wav -= ref.mean()
        wav /= ref.std() + 1e-8

        with torch.no_grad():
            estimates = apply_model(
                demucs_model,
                wav[None],
                device=self._device,
                shifts=0,
                split=True,
                overlap=0.25,
                progress=False,
                num_workers=0,
            )[0]
        # Undo the normalization so output amplitude matches the input.
        estimates = estimates * ref.std() + ref.mean()

        stem_paths: dict[str, Path] = {}
        duration_samples = 0
        for stem_name, stem_tensor in zip(sources, estimates):
            arr = stem_tensor.detach().to("cpu").numpy().T  # (samples, channels)
            duration_samples = max(duration_samples, arr.shape[0])
            stem_path = out_dir / f"{stem_name}.wav"
            sf.write(str(stem_path), arr, sample_rate, subtype="PCM_16", format="WAV")
            stem_paths[stem_name] = stem_path

        for name in stem_paths:
            if name not in DEMUCS_STEMS:
                log.warning("Unexpected stem from %s: %s", model_name, name)

        # silence "unused" linters for symbols we only use via apply_model
        del convert_audio
        return SeparationResult(
            stems=stem_paths,
            sample_rate=sample_rate,
            duration_seconds=duration_samples / sample_rate if sample_rate else 0.0,
        )

    # -- internals -----------------------------------------------------------

    def _ensure_loaded(self, model_name: str) -> Any:
        cached = self._separators.get(model_name)
        if cached is not None:
            return cached
        log.info("Loading model %s on %s", model_name, self._device)
        sep = self._factory(model_name, self._device)
        self._separators[model_name] = sep
        return sep


def _pick_device() -> str:
    """Return the best torch device available on the current host."""
    try:
        import torch

        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
    except Exception:  # torch not installed yet
        pass
    return "cpu"


class _ModelBundle:
    """Holds a pretrained demucs model bound to a device. The wrapper exists
    so the cache key in ``MLXEngine._separators`` is a single object that's
    cheap to swap."""

    def __init__(self, model_name: str, device: str) -> None:
        from demucs.pretrained import get_model

        self.model = get_model(model_name)
        self.model.to(device)
        self.model.eval()
        self.device = device


def _make_separator(model_name: str, device: str) -> Any:
    return _ModelBundle(model_name, device)
