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
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .base import DEMUCS_STEMS, Engine, EngineCapabilities, SeparationResult

log = logging.getLogger(__name__)

# Models we expose. ``htdemucs`` is the project default: ~80 MB, 9.0 dB SDR,
# and the fastest of the four-source models on M-series silicon. ``htdemucs_ft``
# is more accurate but ~4x slower; never selected by default. The ``mdx_extra``
# models are dropped — they're slower (or merely compact), not better, and the
# quantized one needed an extra ``diffq`` dependency.
_SUPPORTED_MODELS: tuple[str, ...] = (
    "htdemucs",
    "htdemucs_ft",
)
_DEFAULT_MODEL = "htdemucs"


@dataclass
class _Prepared:
    """Opaque ``prepare()`` output handed back to ``infer()``. Holds the decoded,
    normalized input plus what's needed to denormalize and label the result."""

    bundle: Any
    wav: Any  # torch.Tensor (channels, samples), normalized
    ref: Any  # torch.Tensor used to undo normalization
    sources: list[str]
    sample_rate: int
    name: str
    model_name: str


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

    def prepare(self, audio_path: Path, *, model: str | None = None) -> Any:
        """Decode + normalize ``audio_path`` into a model-ready input (CPU/IO).

        Split from :meth:`infer` so the pipeline can run this on a producer
        thread while the GPU is busy with another chunk. No GPU work here.
        """
        model_name = model or _DEFAULT_MODEL
        if model_name not in _SUPPORTED_MODELS:
            raise ValueError(
                f"Unknown model {model_name!r}. Supported: {_SUPPORTED_MODELS}"
            )

        bundle = self._ensure_loaded(model_name)

        from demucs.audio import AudioFile

        demucs_model = bundle.model
        sample_rate = int(demucs_model.samplerate)
        channels = int(demucs_model.audio_channels)
        sources: list[str] = list(demucs_model.sources)

        # Load and resample to whatever the model expects (htdemucs: 44.1 kHz
        # stereo). ``AudioFile`` decodes via ffmpeg under the hood.
        wav = AudioFile(audio_path).read(
            streams=0, samplerate=sample_rate, channels=channels
        )
        # Normalize to mean 0 / unit variance — demucs's own separate.py does the
        # same, and it noticeably improves quality on quiet inputs. Keep ``ref``
        # so :meth:`infer` can undo it.
        ref = wav.mean(0)
        wav = (wav - ref.mean()) / (ref.std() + 1e-8)
        return _Prepared(
            bundle=bundle,
            wav=wav,
            ref=ref,
            sources=sources,
            sample_rate=sample_rate,
            name=audio_path.name,
            model_name=model_name,
        )

    def infer_batch(self, prepared: list[Any]) -> list[SeparationResult]:
        """Run the model on a batch of :meth:`prepare` results (the GPU half).

        One ``apply_model`` call over the whole batch fills the GPU's cores that
        a single chunk leaves idle. Inputs may differ in length (the last chunk
        of a track is shorter); we zero-pad to the longest, then trim each output
        back. Each chunk keeps its own normalization ``ref``. Returns in-memory
        ``(samples, channels)`` float32 stems — no disk I/O.
        """
        import torch
        from demucs.apply import apply_model

        n = len(prepared)
        model = prepared[0].bundle.model
        lengths = [p.wav.shape[-1] for p in prepared]
        max_len = max(lengths)
        channels = prepared[0].wav.shape[0]
        log.info("Separating %d chunk(s) (%s) with %s on %s",
                 n, prepared[0].name, prepared[0].model_name, self._device)

        # Stack into (batch, channels, samples), zero-padding short members.
        x = torch.zeros(n, channels, max_len, dtype=prepared[0].wav.dtype)
        for i, p in enumerate(prepared):
            x[i, :, : lengths[i]] = p.wav

        # Time only the inference call — the real accelerator work.
        t_gpu0 = time.perf_counter()
        with torch.no_grad():
            estimates = apply_model(
                model, x, device=self._device, shifts=0, split=True,
                overlap=0.25, progress=False, num_workers=0,
            )
        # MPS and CUDA dispatch kernels asynchronously, so apply_model can return
        # before the GPU is done. Force a sync inside the timed region or we'd
        # measure only dispatch and wildly undercount. (CPU is synchronous.)
        if self._device == "mps":
            sync = getattr(getattr(torch, "mps", None), "synchronize", None)
            if sync:
                sync()
        elif self._device == "cuda":
            torch.cuda.synchronize()
        gpu_each = (time.perf_counter() - t_gpu0) / n

        results: list[SeparationResult] = []
        for i, p in enumerate(prepared):
            # Per-chunk denormalization, then trim off the zero-padding.
            est = estimates[i] * p.ref.std() + p.ref.mean()
            stems: dict[str, Any] = {}
            for stem_name, stem_tensor in zip(p.sources, est):
                arr = stem_tensor.detach().to("cpu").numpy().T  # (samples, channels)
                stems[stem_name] = arr[: lengths[i]]
                if stem_name not in DEMUCS_STEMS:
                    log.warning("Unexpected stem from %s: %s",
                                p.model_name, stem_name)
            sr = p.sample_rate
            results.append(SeparationResult(
                stems=stems,
                sample_rate=sr,
                duration_seconds=lengths[i] / sr if sr else 0.0,
                gpu_seconds=gpu_each,
            ))
        return results

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
    """Return the best torch device available on the current host.

    Priority: Apple MPS, then CUDA (NVIDIA), then CPU.
    """
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
