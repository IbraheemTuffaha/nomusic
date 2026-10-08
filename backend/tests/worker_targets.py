"""Importable multiprocessing targets used by supervised-worker tests."""

from __future__ import annotations

import time


def stuck_worker(_settings, _commands, _events):
    """A native-call stand-in that ignores every cooperative command."""
    while True:
        time.sleep(1)


def fixture_worker(settings, commands, events):
    """Run the real child loop with a tiny importable local acquisition/engine."""
    import numpy as np
    import soundfile as sf

    from nomusic import worker as worker_module
    from nomusic.engines.base import EngineCapabilities, SeparationResult
    from nomusic.pipeline import processor as processor_module
    from nomusic.pipeline.downloader import VideoMetadata

    class FixtureEngine:
        def warmup(self):
            return None

        def prepare(self, path, *, model=None):
            return sf.read(str(path), always_2d=True, dtype="float32")

        def infer_batch(self, prepared):
            results = []
            for audio, sample_rate in prepared:
                zeros = np.zeros_like(audio)
                results.append(SeparationResult(
                    stems={"vocals": audio, "drums": zeros, "bass": zeros, "other": zeros},
                    sample_rate=sample_rate,
                    duration_seconds=len(audio) / sample_rate,
                ))
            return results

        def capabilities(self):
            return EngineCapabilities("fixture", "cpu", ("fixture",), "fixture")

    class FixtureFetcher:
        def __init__(self, url, out_dir):
            self.url = url
            self.out_dir = out_dir
            self.limits = None

        def extract(self):
            return VideoMetadata("fixture", "fixture", 1.0, "fixture", self.url)

        def download(self, progress_hook=None):
            self.out_dir.mkdir(parents=True, exist_ok=True)
            path = self.out_dir / "source.wav"
            sf.write(str(path), np.zeros((44100, 2), dtype=np.float32), 44100)
            if progress_hook:
                progress_hook({"status": "finished", "downloaded_bytes": path.stat().st_size,
                               "total_bytes": path.stat().st_size})
            return path

        def close(self):
            return None

    def slice_source(source, out_path, *, start, end, pass_fds=(), limits=None):
        audio, sample_rate = sf.read(str(source), always_2d=True, dtype="float32")
        sf.write(str(out_path), audio[int(start * sample_rate):int(end * sample_rate)], sample_rate)
        return out_path

    worker_module.get_engine = lambda _name: FixtureEngine()
    processor_module.SourceFetcher = FixtureFetcher
    processor_module.slice_source = slice_source
    worker_module._worker_main(settings, commands, events)
