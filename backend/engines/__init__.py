"""Source-separation engine implementations.

The package exposes a stable :class:`Engine` interface in :mod:`engines.base`.
Concrete engines (MLX today, ONNX or CUDA tomorrow) implement that interface and
are loaded by name from :func:`get_engine`.
"""

from __future__ import annotations

from .base import Engine, EngineCapabilities, SeparationResult

_REGISTRY: dict[str, str] = {
    "mlx": "engines.mlx_engine:MLXEngine",
}


def get_engine(name: str) -> Engine:
    """Instantiate the engine registered under ``name``.

    Engines are imported lazily so a missing optional dependency (e.g. MLX on a
    non-Apple-Silicon host) never breaks unrelated engines.
    """
    if name not in _REGISTRY:
        raise ValueError(
            f"Unknown engine {name!r}. Known engines: {sorted(_REGISTRY)}"
        )
    module_path, _, attr = _REGISTRY[name].partition(":")
    import importlib

    module = importlib.import_module(module_path)
    return getattr(module, attr)()


__all__ = ["Engine", "EngineCapabilities", "SeparationResult", "get_engine"]
