"""Abstract source-separation engine.

The rest of the backend talks only to this interface. Concrete engines (MLX,
ONNX, CUDA, ...) translate the calls below into framework-specific code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Demucs' four-stem model output. Kept here so callers don't need to import a
# concrete engine to validate stem names.
DEMUCS_STEMS: tuple[str, ...] = ("vocals", "drums", "bass", "other")


@dataclass(frozen=True)
class EngineCapabilities:
    """What an engine can do, surfaced via ``GET /capabilities``."""

    name: str
    device: str
    supported_models: tuple[str, ...]
    default_model: str
    supported_stems: tuple[str, ...] = DEMUCS_STEMS


@dataclass(frozen=True)
class SeparationResult:
    """Result of separating one audio buffer into stems.

    ``stems`` maps a stem name to its audio as an in-memory ``(samples,
    channels)`` float32 array. We keep stems in memory (not WAVs on disk) so the
    mixing stage doesn't round-trip through ffmpeg/disk on the hot path, and so
    the GPU stage can be a pure ``input -> arrays`` function in the pipeline.
    """

    stems: dict[str, Any]
    sample_rate: int
    duration_seconds: float
    # Wall-clock seconds spent in the actual model inference (the GPU/accelerator
    # call), excluding decode + stem hand-off. Lets the processor report a GPU
    # duty cycle vs the rest of the per-chunk pipeline. ``None`` if the engine
    # doesn't measure it (e.g. test stubs).
    gpu_seconds: float | None = None


class Engine(ABC):
    """Source-separation engine contract.

    An engine wraps a single backend (MLX, ONNX, ...). It is responsible for
    loading model weights and running inference, returning the separated stems
    as in-memory arrays (see :class:`SeparationResult`) — no disk I/O.
    """

    @abstractmethod
    def capabilities(self) -> EngineCapabilities:
        """Static metadata. Must not load model weights."""

    @abstractmethod
    def prepare(self, audio_path: Path, *, model: str | None = None) -> Any:
        """Decode + pre-process ``audio_path`` into a model-ready input.

        Returns an opaque object passed straight back to :meth:`infer`. This is
        the CPU/IO half (ffmpeg decode, resample, normalize), split from the
        accelerator half so a caller can run it on another thread — overlapping
        it with a GPU inference of a *different* chunk. ``model`` defaults to
        ``capabilities().default_model``.
        """

    @abstractmethod
    def infer_batch(self, prepared: list[Any]) -> list[SeparationResult]:
        """Separate a batch of :meth:`prepare` results in one accelerator call.

        Returns one :class:`SeparationResult` per input, in order, with in-memory
        stems (no disk I/O). Batching fills the GPU's cores — a single chunk
        leaves them partly idle — for higher throughput at identical per-chunk
        output. Each result's ``gpu_seconds`` is the batch's inference time split
        evenly across its members, so callers can still sum a GPU duty cycle.
        """

    def infer(self, prepared: Any) -> SeparationResult:
        """Separate one prepared input. Convenience wrapper over
        :meth:`infer_batch`."""
        return self.infer_batch([prepared])[0]

    def warmup(self) -> None:
        """Optional: load weights ahead of the first request. No-op by default
        (returns None implicitly); engines that pay a load cost override it."""
