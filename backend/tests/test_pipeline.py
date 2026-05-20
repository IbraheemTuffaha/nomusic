"""Pipeline unit tests.

These tests run without torch/demucs by stubbing the engine and the downloader
so they're suitable for CI on any platform.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import soundfile as sf

from engines.base import Engine, EngineCapabilities, SeparationResult
from pipeline.cache import JobCache
from pipeline.processor import Processor, plan_chunks


def test_plan_chunks_covers_full_duration():
    plans = plan_chunks(duration=95.0, chunk_seconds=30.0, overlap_seconds=1.0)
    # Total play coverage must equal the source duration (modulo rounding).
    coverage = sum(p.play_end - p.play_start for p in plans)
    assert math.isclose(coverage, 95.0, rel_tol=1e-6, abs_tol=1e-6)
    # Last chunk's play_end is the source duration.
    assert math.isclose(plans[-1].play_end, 95.0)
    # First chunk has no pre-roll trim.
    assert plans[0].start == 0.0
    # Adjacent chunks' download windows overlap.
    assert plans[1].start < plans[0].end


def test_plan_chunks_rejects_overlap_eq_chunk():
    try:
        plan_chunks(duration=10.0, chunk_seconds=2.0, overlap_seconds=2.0)
    except ValueError:
        return
    raise AssertionError("expected ValueError for overlap == chunk")


class _FakeEngine(Engine):
    """Returns deterministic per-stem WAVs of the right length."""

    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            name="fake",
            device="cpu",
            supported_models=("fake",),
            default_model="fake",
        )

    def separate(
        self,
        audio_path: Path,
        out_dir: Path,
        *,
        model: str | None = None,
    ) -> SeparationResult:
        audio, sr = sf.read(str(audio_path), always_2d=True, dtype="float32")
        out_dir.mkdir(parents=True, exist_ok=True)
        stems: dict[str, Path] = {}
        # Vocals: half the source; other: a quarter; drums/bass: zeros.
        for name, gain in (
            ("vocals", 0.5),
            ("other", 0.25),
            ("drums", 0.0),
            ("bass", 0.0),
        ):
            arr = audio * gain
            p = out_dir / f"{name}.wav"
            sf.write(str(p), arr, sr, subtype="PCM_16")
            stems[name] = p
        return SeparationResult(
            stems=stems,
            sample_rate=sr,
            duration_seconds=audio.shape[0] / sr,
        )


def _write_tone(path: Path, *, seconds: float, sample_rate: int = 44100) -> None:
    t = np.arange(int(seconds * sample_rate), dtype=np.float32) / sample_rate
    tone = (0.2 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    stereo = np.stack([tone, tone], axis=1)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), stereo, sample_rate, subtype="PCM_16")


def test_processor_end_to_end_with_fake_engine(tmp_path, monkeypatch):
    # Bypass yt-dlp by writing a deterministic source and intercepting probe +
    # download_range to read slices of it.
    source = tmp_path / "source.wav"
    _write_tone(source, seconds=10.0)

    from pipeline import downloader as dl
    from pipeline.downloader import VideoMetadata

    def fake_probe(url: str) -> VideoMetadata:
        return VideoMetadata(
            id="fake",
            title="Fake video",
            duration_seconds=10.0,
            extractor="fake",
            webpage_url=url,
        )

    def fake_download(url, out_path, *, start, end):
        audio, sr = sf.read(str(source), always_2d=True, dtype="float32")
        slice_audio = audio[int(start * sr) : int(end * sr)]
        sf.write(str(out_path), slice_audio, sr, subtype="PCM_16")
        return out_path

    monkeypatch.setattr(dl, "probe", fake_probe)
    monkeypatch.setattr(dl, "download_range", fake_download)
    # Processor imports them by name from pipeline.downloader, so patch there too.
    from pipeline import processor as proc

    monkeypatch.setattr(proc, "probe", fake_probe)
    monkeypatch.setattr(proc, "download_range", fake_download)

    cache = JobCache(tmp_path / "cache")
    processor = Processor(
        engine=_FakeEngine(),
        cache=cache,
        chunk_seconds=4.0,
        chunk_overlap_seconds=0.5,
    )

    progress_seen: list[int] = []
    phases_seen: list[str] = []

    def _progress(meta, phase):
        phases_seen.append(phase)
        if phase == "chunk_complete":
            progress_seen.append(len(meta.chunks_ready))

    key = processor.run(
        "fake://video",
        model="fake",
        keep_stems=["vocals", "other"],
        on_progress=_progress,
    )

    meta = cache.load_meta(key)
    assert meta is not None
    assert meta.complete
    assert meta.total_chunks > 1
    # Every planned chunk landed on disk.
    for i in range(meta.total_chunks):
        assert cache.chunk_path(key, i).exists()
    assert cache.full_path(key).exists()

    # Each chunk's duration must equal play_end - play_start. If we drift
    # from this, the extension's "schedule chunk N at video time N*stride"
    # accumulates offset and audio falls out of sync with the video.
    from pipeline.processor import plan_chunks

    plans = plan_chunks(
        duration=meta.duration_seconds,
        chunk_seconds=meta.chunk_seconds,
        overlap_seconds=meta.chunk_overlap_seconds,
    )
    for plan in plans:
        info = sf.info(str(cache.chunk_path(key, plan.index)))
        expected = plan.play_end - plan.play_start
        assert abs(info.duration - expected) < 0.005, (
            f"chunk {plan.index}: duration {info.duration:.3f}s != "
            f"expected {expected:.3f}s (play_start={plan.play_start})"
        )
    # Progress callback fires for each completed chunk.
    assert progress_seen == sorted(progress_seen)
    assert progress_seen[-1] == meta.total_chunks
    # Phase ordering: each chunk emits downloading -> separating -> mixing
    # -> chunk_complete (in order, though phases for different chunks may
    # interleave). Just verify all four phases were seen at least once.
    assert {"downloading", "separating", "mixing", "chunk_complete"}.issubset(
        set(phases_seen)
    )

    # Re-running is a no-op: cache hit short-circuits the pipeline.
    key2 = processor.run("fake://video", model="fake", keep_stems=["vocals", "other"])
    assert key2 == key
