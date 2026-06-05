"""Abstract source-separation engine.

The rest of the backend talks only to this interface. Concrete engines (MLX,
ONNX, CUDA, ...) translate the calls below into framework-specific code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

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

    ``stems`` maps a stem name to a 16-bit PCM WAV path on disk. We pass paths
    rather than in-memory arrays so the processor can hand chunks to ffmpeg for
    crossfading without an extra round-trip through Python.
    """

    stems: dict[str, Path]
    sample_rate: int
    duration_seconds: float
    # Wall-clock seconds spent in the actual model inference (the GPU/accelerator
    # call), excluding decode + stem write-out. Lets the processor report a GPU
    # duty cycle vs the rest of the per-chunk pipeline. ``None`` if the engine
    # doesn't measure it (e.g. test stubs).
    gpu_seconds: float | None = None


class Engine(ABC):
    """Source-separation engine contract.

    An engine wraps a single backend (MLX, ONNX, ...). It is responsible for
    loading model weights, running inference, and writing per-stem WAVs to a
    directory chosen by the caller.
    """

    @abstractmethod
    def capabilities(self) -> EngineCapabilities:
        """Static metadata. Must not load model weights."""

    @abstractmethod
    def separate(
        self,
        audio_path: Path,
        out_dir: Path,
        *,
        model: str | None = None,
    ) -> SeparationResult:
        """Separate ``audio_path`` into stems written under ``out_dir``.

        ``model`` defaults to ``capabilities().default_model``.
        """

    def warmup(self) -> None:
        """Optional: load weights ahead of the first request. No-op by default."""
        return None


@dataclass
class EngineRegistration:
    """For tests and future plugin discovery. Unused at runtime today."""

    name: str
    factory: object
    extras: dict[str, str] = field(default_factory=dict)
