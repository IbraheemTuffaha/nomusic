"""MLX-backed implementation of the :class:`Engine` interface.

This module intentionally stays small: it advertises capabilities, lazily
imports ``demucs_mlx`` only when :meth:`separate` is called, and writes one WAV
per stem. Anything fancier (chunking, crossfade, caching) belongs upstream in
the processor / cache layers.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .base import DEMUCS_STEMS, Engine, EngineCapabilities, SeparationResult

log = logging.getLogger(__name__)

# Models known to demucs / demucs-mlx. ``htdemucs`` is the project default:
# 80 MB, 9.0 dB SDR, ~20-25x realtime on an M3 Pro. ``htdemucs_ft`` is more
# accurate but ~4x slower; we keep it available but never select it by default.
_SUPPORTED_MODELS: tuple[str, ...] = (
    "htdemucs",
    "htdemucs_ft",
    "mdx_extra",
)
_DEFAULT_MODEL = "htdemucs"


class MLXEngine(Engine):
    def __init__(self) -> None:
        self._loaded_model: str | None = None
        self._separator = None  # demucs_mlx Separator instance, lazy-loaded

    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            name="mlx",
            device=self._device_label(),
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

        self._ensure_loaded(model_name)
        out_dir.mkdir(parents=True, exist_ok=True)

        # Imported here so a missing soundfile/numpy on a sidecar process can't
        # crash module import.
        import numpy as np
        import soundfile as sf

        audio, sample_rate = sf.read(str(audio_path), always_2d=True, dtype="float32")
        # demucs_mlx expects shape (channels, samples).
        audio_t = audio.T
        if audio_t.shape[0] == 1:
            audio_t = np.repeat(audio_t, 2, axis=0)

        log.info(
            "Separating %s (%.1fs, %d Hz) with %s",
            audio_path.name,
            audio_t.shape[1] / sample_rate,
            sample_rate,
            model_name,
        )

        stems = self._separator(audio_t, sample_rate=sample_rate)

        stem_paths: dict[str, Path] = {}
        for stem_name, stem_audio in stems.items():
            stem_path = out_dir / f"{stem_name}.wav"
            sf.write(
                str(stem_path),
                stem_audio.T,  # back to (samples, channels)
                sample_rate,
                subtype="PCM_16",
            )
            stem_paths[stem_name] = stem_path

        return SeparationResult(
            stems=stem_paths,
            sample_rate=sample_rate,
            duration_seconds=audio_t.shape[1] / sample_rate,
        )

    # -- internals -----------------------------------------------------------

    def _ensure_loaded(self, model_name: str) -> None:
        if self._separator is not None and self._loaded_model == model_name:
            return
        # ``demucs_mlx`` lives under ``vendor/demucs-mlx`` after install.sh runs.
        # The vendor path is added to ``sys.path`` by server.py's bootstrap.
        from demucs_mlx import Separator  # type: ignore[import-not-found]

        log.info("Loading MLX model %s", model_name)
        self._separator = Separator(model=model_name)
        self._loaded_model = model_name

    @staticmethod
    def _device_label() -> str:
        try:
            import mlx.core as mx  # type: ignore[import-not-found]

            # MLX runs on the unified GPU on Apple Silicon; no device picker.
            return f"mlx/{mx.default_device()}"
        except Exception:
            return "mlx/uninitialized"
