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
    # Bypass yt-dlp by writing a deterministic source and intercepting
    # probe, download_source, and slice_source. ``download_source`` returns
    # the path to the pre-written master WAV; ``slice_source`` does a
    # straightforward sample-level cut into the requested WAV path.
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

    def fake_download_source(url, out_dir, *, progress_hook=None):
        out_dir.mkdir(parents=True, exist_ok=True)
        dst = out_dir / "source.wav"
        if not dst.exists():
            dst.write_bytes(source.read_bytes())
        if progress_hook:
            progress_hook(
                {
                    "status": "finished",
                    "downloaded_bytes": dst.stat().st_size,
                    "total_bytes": dst.stat().st_size,
                }
            )
        return dst

    def fake_slice_source(src, out_path, *, start, end):
        audio, sr = sf.read(str(src), always_2d=True, dtype="float32")
        slice_audio = audio[int(start * sr) : int(end * sr)]
        sf.write(str(out_path), slice_audio, sr, subtype="PCM_16", format="WAV")
        return out_path

    monkeypatch.setattr(dl, "probe", fake_probe)
    monkeypatch.setattr(dl, "download_source", fake_download_source)
    monkeypatch.setattr(dl, "slice_source", fake_slice_source)
    # Processor imports them by name from pipeline.downloader, so patch there too.
    from pipeline import processor as proc

    monkeypatch.setattr(proc, "probe", fake_probe)
    monkeypatch.setattr(proc, "download_source", fake_download_source)
    monkeypatch.setattr(proc, "slice_source", fake_slice_source)

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

    # Each chunk's duration must equal play_end - play_start. If we drift
    # from this, the extension's "schedule chunk N at video time N*stride"
    # accumulates offset and audio falls out of sync with the video.
    # Opus encoding may add a few extra samples of priming at the boundary;
    # tolerance is loose enough to absorb that without hiding real drift.
    from pipeline.processor import plan_chunks

    plans = plan_chunks(
        duration=meta.duration_seconds,
        chunk_seconds=meta.chunk_seconds,
        overlap_seconds=meta.chunk_overlap_seconds,
    )
    for plan in plans:
        info = sf.info(str(cache.chunk_path(key, plan.index)))
        expected = plan.play_end - plan.play_start
        assert abs(info.duration - expected) < 0.05, (
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


def test_prepare_skips_reprobe_on_resume(tmp_path, monkeypatch):
    # A prior run already probed this job and processed some chunks; a resume
    # (after idle-abandon or a page refresh) must NOT pay the yt-dlp probe
    # again, and must preserve the already-completed chunks.
    from pipeline import processor as proc
    from pipeline.cache import CacheMeta

    def boom_probe(url):
        raise AssertionError("probe must be skipped on resume")

    monkeypatch.setattr(proc, "probe", boom_probe)

    cache = JobCache(tmp_path / "cache")
    processor = Processor(
        engine=_FakeEngine(),
        cache=cache,
        chunk_seconds=10.0,
        chunk_overlap_seconds=0.5,
    )
    key = cache.key(
        "fake://video",
        "fake",
        ["vocals"],
        chunk_seconds=10.0,
        chunk_overlap_seconds=0.5,
    )
    plans = plan_chunks(120.0, 10.0, 0.5)
    cache.save_meta(
        key,
        CacheMeta(
            url="fake://video",
            model="fake",
            keep_stems=["vocals"],
            duration_seconds=120.0,
            chunk_seconds=10.0,
            chunk_overlap_seconds=0.5,
            total_chunks=len(plans),
            title="Cached",
            extractor="fake",
            chunks_ready=[0, 1, 2],
        ),
    )

    k, meta, info, returned_plans = processor.prepare(
        "fake://video", model="fake", keep_stems=["vocals"]
    )
    assert k == key
    assert meta.chunks_ready == [0, 1, 2]  # not wiped
    assert info.duration_seconds == 120.0  # reused, not re-probed
    assert len(returned_plans) == len(plans)


def test_abandon_all_signals_workers_and_clears_state():
    # /cache/clear must tell live workers to stop (so they unwind cleanly
    # instead of writing into deleted dirs) and wipe the in-memory maps.
    from jobs import JobRegistry, JobState, JobStatus

    registry = JobRegistry(processor=None, cache=None)
    registry._jobs["k1"] = JobStatus(job_id="k1", state=JobState.PROCESSING)
    registry._jobs["k2"] = JobStatus(job_id="k2", state=JobState.DOWNLOADING)
    registry._subscribers["k1"] = []
    registry._last_disconnect_at["k1"] = 123.0

    registry.abandon_all()

    assert registry._jobs == {}
    assert registry._subscribers == {}
    assert registry._last_disconnect_at == {}
    assert registry._abandoning == {"k1", "k2"}


def test_submit_refuses_to_adopt_an_abandoning_job(monkeypatch):
    # The C7 race: a /process landing during a job's abandon-unwind must NOT
    # hand back the dying job (which would leave it stuck with no worker); it
    # must spawn a fresh worker instead.
    from jobs import JobRegistry, JobState, JobStatus

    class _StubCache:
        def key(self, *a, **k):
            return "k1"

        def load_meta(self, key):
            return None

    class _StubProcessor:
        chunk_seconds = 10.0
        chunk_overlap_seconds = 0.5

    registry = JobRegistry(processor=_StubProcessor(), cache=_StubCache())
    old = JobStatus(job_id="k1", state=JobState.PROCESSING)
    registry._jobs["k1"] = old
    registry._abandoning.add("k1")

    # Don't run the real worker; just let submit register a thread.
    monkeypatch.setattr(registry, "_run", lambda key, url, model, keep_stems: None)

    status = registry.submit("fake://video", model="fake", keep_stems=["vocals"])
    # Submit refused to adopt the dying job: it created a fresh QUEUED status,
    # registered a new worker thread, and cleared the stale abandon mark — all
    # synchronously under the lock, so these checks aren't racing the thread.
    assert status is not old
    assert status.state is JobState.QUEUED
    assert "k1" in registry._threads
    assert "k1" not in registry._abandoning
