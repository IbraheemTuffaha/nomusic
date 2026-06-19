"""Pipeline unit tests.

These tests run without torch/demucs by stubbing the engine and the downloader
so they're suitable for CI on any platform.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from engines.base import Engine, EngineCapabilities, SeparationResult
from pipeline.cache import JobCache
from pipeline.export import (
    mp3_transcode_cmd,
    mux_video_cmd,
    snapshot_chunk_files,
)
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

    def __init__(self) -> None:
        # Every batch size handed to infer_batch, so a test can assert the
        # processor never exceeds the configured batch (or stops batching).
        self.batch_sizes: list[int] = []

    def capabilities(self) -> EngineCapabilities:
        return EngineCapabilities(
            name="fake",
            device="cpu",
            supported_models=("fake",),
            default_model="fake",
        )

    def prepare(self, audio_path: Path, *, model: str | None = None):
        audio, sr = sf.read(str(audio_path), always_2d=True, dtype="float32")
        return (audio, sr)

    def infer_batch(self, prepared) -> list[SeparationResult]:
        self.batch_sizes.append(len(prepared))
        out = []
        for audio, sr in prepared:
            # Vocals: half the source; other: a quarter; drums/bass: zeros.
            out.append(SeparationResult(
                stems={
                    "vocals": audio * 0.5,
                    "other": audio * 0.25,
                    "drums": audio * 0.0,
                    "bass": audio * 0.0,
                },
                sample_rate=sr,
                duration_seconds=audio.shape[0] / sr,
            ))
        return out


def _write_tone(path: Path, *, seconds: float, sample_rate: int = 44100) -> None:
    t = np.arange(int(seconds * sample_rate), dtype=np.float32) / sample_rate
    tone = (0.2 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
    stereo = np.stack([tone, tone], axis=1)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), stereo, sample_rate, subtype="PCM_16")


def _fake_fetcher_class(source_tone: Path, duration: float = 10.0):
    """A stand-in for downloader.SourceFetcher: extract() returns fixed
    metadata; download() drops the pre-written tone into out_dir. Patch it onto
    ``processor.SourceFetcher`` to exercise the first-run path without yt-dlp."""
    from pipeline.downloader import VideoMetadata

    class _FakeFetcher:
        def __init__(self, url, out_dir):
            self.url = url
            self.out_dir = Path(out_dir)

        def extract(self):
            return VideoMetadata(
                id="fake", title="Fake", duration_seconds=duration,
                extractor="fake", webpage_url=self.url,
            )

        def download(self, progress_hook=None):
            self.out_dir.mkdir(parents=True, exist_ok=True)
            dst = self.out_dir / "source.wav"
            if not dst.exists():
                dst.write_bytes(source_tone.read_bytes())
            if progress_hook:
                progress_hook({"status": "finished", "downloaded_bytes": 1, "total_bytes": 1})
            return dst

    return _FakeFetcher


def test_processor_end_to_end_with_fake_engine(tmp_path, monkeypatch):
    # Bypass yt-dlp by writing a deterministic source and intercepting
    # probe, download_source, and slice_source. ``download_source`` returns
    # the path to the pre-written master WAV; ``slice_source`` does a
    # straightforward sample-level cut into the requested WAV path.
    source = tmp_path / "source.wav"
    _write_tone(source, seconds=10.0)

    from pipeline import processor as proc

    def fake_slice_source(src, out_path, *, start, end):
        audio, sr = sf.read(str(src), always_2d=True, dtype="float32")
        slice_audio = audio[int(start * sr) : int(end * sr)]
        sf.write(str(out_path), slice_audio, sr, subtype="PCM_16", format="WAV")
        return out_path

    # First run goes through SourceFetcher; patch it (and the slicer) so no
    # yt-dlp is touched.
    monkeypatch.setattr(proc, "SourceFetcher", _fake_fetcher_class(source))
    monkeypatch.setattr(proc, "slice_source", fake_slice_source)

    cache = JobCache(tmp_path / "cache")
    engine = _FakeEngine()
    processor = Processor(
        engine=engine,
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
    # Pipeline phases: the GPU stage emits "separating" when it picks up a
    # chunk, and the write stage emits "chunk_complete" once it's on disk.
    # (The old serial "downloading"/"mixing" intra-chunk hints are gone — with
    # chunks overlapping across stages they no longer denote a single chunk's
    # progress.) Verify both fire.
    assert {"separating", "chunk_complete"}.issubset(set(phases_seen))

    # The GPU stage batches opportunistically. We can't pin exact batch sizes
    # (they depend on decode-vs-infer timing), but every call must stay within
    # [1, gpu_batch] — a regression that passed the whole queue in one call, or
    # broke the per-call list, would land outside that bound. The batch count
    # must also account for every chunk exactly once.
    from config import SETTINGS

    cap = max(1, SETTINGS.gpu_batch)
    assert engine.batch_sizes, "infer_batch was never called"
    assert all(1 <= n <= cap for n in engine.batch_sizes), engine.batch_sizes
    assert sum(engine.batch_sizes) == meta.total_chunks

    # Re-running is a no-op: cache hit short-circuits the pipeline.
    key2 = processor.run("fake://video", model="fake", keep_stems=["vocals", "other"])
    assert key2 == key


def test_processor_progressive_produces_correct_chunks(tmp_path, monkeypatch):
    # Progressive mode must produce the same correct, sample-accurate chunks as
    # the download-once path. The fake download reports "finished" immediately,
    # so the duration gate opens at once and we exercise the _ProgressiveSource
    # plumbing + the gated loop without timing flakiness.
    source = tmp_path / "source.wav"
    _write_tone(source, seconds=10.0)

    from pipeline import processor as proc

    def fake_slice_source(src, out_path, *, start, end):
        audio, sr = sf.read(str(src), always_2d=True, dtype="float32")
        sf.write(str(out_path), audio[int(start * sr) : int(end * sr)], sr,
                 subtype="PCM_16", format="WAV")
        return out_path

    # Progressive first-run downloads via SourceFetcher on a background thread;
    # the fake reports "finished" immediately so the duration gate opens at once.
    monkeypatch.setattr(proc, "SourceFetcher", _fake_fetcher_class(source))
    monkeypatch.setattr(proc, "slice_source", fake_slice_source)

    cache = JobCache(tmp_path / "cache")
    processor = Processor(
        engine=_FakeEngine(), cache=cache,
        chunk_seconds=4.0, chunk_overlap_seconds=0.5, progressive=True,
    )
    key = processor.run("fake://video", model="fake", keep_stems=["vocals"])

    meta = cache.load_meta(key)
    assert meta is not None and meta.complete and meta.total_chunks > 1
    plans = plan_chunks(meta.duration_seconds, meta.chunk_seconds, meta.chunk_overlap_seconds)
    for plan in plans:
        path = cache.chunk_path(key, plan.index)
        assert path.exists()
        info = sf.info(str(path))
        assert abs(info.duration - (plan.play_end - plan.play_start)) < 0.05


def test_prepare_skips_reprobe_on_resume(tmp_path, monkeypatch):
    # A prior run already probed this job and processed some chunks; a resume
    # (after idle-abandon or a page refresh) must NOT pay the yt-dlp probe
    # again, and must preserve the already-completed chunks.
    from pipeline import processor as proc
    from pipeline.cache import CacheMeta

    class _BoomFetcher:
        def __init__(self, *a, **k):
            raise AssertionError("resume must not extract/download — no fetcher")

    # Resume must reuse cached meta: no probe, no SourceFetcher construction.
    monkeypatch.setattr(proc, "probe", lambda url: (_ for _ in ()).throw(
        AssertionError("probe must be skipped on resume")))
    monkeypatch.setattr(proc, "SourceFetcher", _BoomFetcher)

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

    k, meta, info, returned_plans, fetcher = processor.prepare(
        "fake://video", model="fake", keep_stems=["vocals"]
    )
    assert k == key
    assert fetcher is None  # resume path: no extraction session
    assert meta.chunks_ready == [0, 1, 2]  # not wiped
    assert info.duration_seconds == 120.0  # reused, not re-probed
    assert len(returned_plans) == len(plans)


# --- Download export helpers (back the /audio?format=mp3 and /video endpoints) ---


def _has(*bins: str) -> bool:
    return all(shutil.which(b) for b in bins)


def _ffmpeg_has_encoder(name: str) -> bool:
    if not shutil.which("ffmpeg"):
        return False
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True
    )
    return name in out.stdout


def _ffprobe_streams(path: Path) -> list[dict]:
    out = subprocess.run(
        ["ffprobe", "-hide_banner", "-loglevel", "error", "-show_entries",
         "stream=codec_name,codec_type", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout)["streams"]


def _ffprobe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-hide_banner", "-loglevel", "error", "-show_entries",
         "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(json.loads(out.stdout)["format"]["duration"])


def test_video_format_honours_height_cap():
    from pipeline.downloader import _video_format

    capped = _video_format(720)
    assert capped == "bestvideo[height<=720]/bestvideo/best"
    # Must NOT pin a codec in the selector: avc1 tops out at 1080p on YouTube,
    # so an avc1 filter would silently cap 1440p/4K requests at 1080p.
    assert "vcodec" not in capped and "avc1" not in capped

    assert _video_format(None) == "bestvideo/best"  # no cap, no redundant dup
    assert _video_format(0) == "bestvideo/best"  # 0 == best available


def test_video_dir_keyed_by_resolution(tmp_path):
    cache = JobCache(tmp_path / "cache")
    url = "https://example.com/watch?v=abc"
    # Different resolutions must not share a cache dir (else switching quality
    # would reuse the wrong-resolution download); same args are stable.
    assert cache.video_dir(url, 1080) != cache.video_dir(url, 720)
    assert cache.video_dir(url, 1080) == cache.video_dir(url, 1080)
    assert cache.video_dir(url, None) != cache.video_dir(url, 1080)


def test_snapshot_chunk_files_returns_contiguous_prefix(tmp_path):
    # A gap in the chunk sequence must truncate the snapshot — the export must
    # never advertise/serve chunks past the first hole.
    cache = JobCache(tmp_path / "cache")
    key = "job0"
    cache.dir_for(key)  # create the dir without going through the processor
    for idx in (0, 1, 2, 4):  # note: 3 is missing
        cache.chunk_path(key, idx).write_bytes(f"chunk{idx}".encode())

    files = snapshot_chunk_files(cache, key, total_chunks=5)
    assert [p.name for p, _ in files] == [
        "chunk_000.opus", "chunk_001.opus", "chunk_002.opus"
    ]
    # Sizes are captured in the same pass and match what's on disk.
    assert [size for _, size in files] == [len(b"chunk0"), len(b"chunk1"), len(b"chunk2")]


def _make_opus_chunk(wav: Path, out: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(wav),
         "-c:a", "libopus", "-b:a", "96k", "-f", "ogg", str(out)],
        check=True,
    )


@pytest.mark.skipif(
    not (_ffmpeg_has_encoder("libopus") and _ffmpeg_has_encoder("libmp3lame")
         and _has("ffprobe")),
    reason="needs ffmpeg with libopus + libmp3lame and ffprobe",
)
def test_mp3_transcode_produces_playable_mp3(tmp_path):
    # The /audio?format=mp3 path: concatenated Opus chunks must transcode to a
    # single MP3 of the combined duration.
    tone = tmp_path / "tone.wav"
    _write_tone(tone, seconds=2.0)
    chunk_files = []
    for i in range(3):
        c = tmp_path / f"chunk_{i:03d}.opus"
        _make_opus_chunk(tone, c)
        chunk_files.append((c, c.stat().st_size))

    out = tmp_path / "full.mp3"
    subprocess.run(mp3_transcode_cmd(chunk_files, out), check=True, capture_output=True)

    streams = _ffprobe_streams(out)
    assert [s["codec_type"] for s in streams] == ["audio"]
    assert streams[0]["codec_name"] == "mp3"
    # ~6s total (three 2s chunks). The concat filter splices every chunk onto one
    # timeline, so the duration reflects all of them (a regression guard against
    # byte-concatenation, which left the file unseekable).
    assert abs(_ffprobe_duration(out) - 6.0) < 0.3


@pytest.mark.skipif(
    not (_ffmpeg_has_encoder("libopus") and _ffmpeg_has_encoder("libmp3lame")
         and _has("ffprobe")),
    reason="needs ffmpeg with libopus + libmp3lame and ffprobe",
)
def test_concat_is_sample_accurate_no_drift(tmp_path):
    # Regression for end-of-video A/V drift. Each chunk is encoded to Opus
    # independently, which adds a fixed encoder pre-skip and pads the final
    # packet to a 20 ms frame. The concat *demuxer* (the old approach) left that
    # per-file slop in at every boundary, so the joined audio grew ~10+ ms per
    # chunk and slid behind the video — worst at the end. The concat *filter*
    # decodes each input on its own, so the splice is sample-accurate.
    #
    # Use a chunk length that is NOT a multiple of 20 ms (0.45 s) so the padding
    # is real, and enough chunks that any per-boundary slop would accumulate far
    # past the tolerance (the old join drifted ~0.2 s over these 16 chunks).
    n, dur = 16, 0.45
    chunk_files = []
    for i in range(n):
        tone = tmp_path / f"tone_{i}.wav"
        _write_tone(tone, seconds=dur)
        c = tmp_path / f"chunk_{i:03d}.opus"
        _make_opus_chunk(tone, c)
        chunk_files.append((c, c.stat().st_size))

    out = tmp_path / "full.mp3"
    subprocess.run(mp3_transcode_cmd(chunk_files, out), check=True, capture_output=True)

    # Tolerance absorbs the MP3 encoder's own small priming/padding but is far
    # tighter than the accumulated drift the demuxer join produced.
    assert abs(_ffprobe_duration(out) - n * dur) < 0.1


@pytest.mark.skipif(
    not (_ffmpeg_has_encoder("libopus") and _ffmpeg_has_encoder("libx264")
         and _ffmpeg_has_encoder("aac") and _has("ffprobe")),
    reason="needs ffmpeg with libopus + libx264 + aac and ffprobe",
)
def test_mux_video_replaces_audio_with_stripped_track(tmp_path):
    # The /video path: a copied video stream plus the stripped audio re-encoded
    # to AAC, in one MP4.
    tone = tmp_path / "tone.wav"
    _write_tone(tone, seconds=2.0)
    chunk_files = []
    for i in range(2):
        c = tmp_path / f"chunk_{i:03d}.opus"
        _make_opus_chunk(tone, c)
        chunk_files.append((c, c.stat().st_size))

    video = tmp_path / "video.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "testsrc=duration=4:size=320x240:rate=10", "-c:v", "libx264",
         "-pix_fmt", "yuv420p", str(video)],
        check=True, capture_output=True,
    )

    out = tmp_path / "out.mp4"
    subprocess.run(mux_video_cmd(video, chunk_files, out), check=True, capture_output=True)

    streams = _ffprobe_streams(out)
    kinds = sorted(s["codec_type"] for s in streams)
    assert kinds == ["audio", "video"]
    audio_stream = next(s for s in streams if s["codec_type"] == "audio")
    assert audio_stream["codec_name"] == "aac"
    video_stream = next(s for s in streams if s["codec_type"] == "video")
    assert video_stream["codec_name"] == "h264"  # copied through, not re-encoded

    # apad + -shortest must align the streams: audio padded/trimmed to the
    # video length so both end together (guards the end-of-video A/V drift fix).
    def _sdur(stream):
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", stream,
             "-show_entries", "stream=duration", "-of", "default=nw=1:nk=1", str(out)],
            capture_output=True, text=True,
        ).stdout.strip()
        return float(res)
    assert abs(_sdur("a:0") - _sdur("v:0")) < 0.1


@pytest.mark.skipif(
    not (_ffmpeg_has_encoder("libopus") and _ffmpeg_has_encoder("libvpx-vp9")
         and _ffmpeg_has_encoder("libx264") and _ffmpeg_has_encoder("aac")
         and _has("ffprobe")),
    reason="needs ffmpeg with libopus + libvpx-vp9 + libx264 + aac and ffprobe",
)
def test_mux_video_reencodes_vp9_to_h264(tmp_path):
    # VP9 (YouTube's codec above 1080p) can be byte-copied into MP4 but won't
    # play in QuickTime/Safari, so the export re-encodes it to H.264. This
    # guards the playability fix.
    tone = tmp_path / "tone.wav"
    _write_tone(tone, seconds=2.0)
    audio = tmp_path / "chunk_000.opus"
    _make_opus_chunk(tone, audio)
    chunk_files = [(audio, audio.stat().st_size)]

    video = tmp_path / "video.webm"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "testsrc=duration=2:size=320x240:rate=10", "-c:v", "libvpx-vp9",
         "-b:v", "200k", str(video)],
        check=True, capture_output=True,
    )
    assert _ffprobe_streams(video)[0]["codec_name"] == "vp9"

    out = tmp_path / "out.mp4"
    subprocess.run(
        mux_video_cmd(video, chunk_files, out, reencode_video=True),
        check=True, capture_output=True,
    )
    video_stream = next(s for s in _ffprobe_streams(out) if s["codec_type"] == "video")
    assert video_stream["codec_name"] == "h264"  # re-encoded, QuickTime-playable


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


def test_infer_batch_matches_single_mlx(tmp_path):
    """Batched separation must produce the same per-chunk output as separate
    single calls — otherwise batching for throughput would change results.
    Uses the real MLX engine, so it's skipped where a GPU/torch isn't available."""
    try:
        import torch
    except Exception:
        pytest.skip("torch not installed")
    if not (torch.backends.mps.is_available() or torch.cuda.is_available()):
        pytest.skip("no GPU (MPS/CUDA) available")

    from engines.mlx_engine import MLXEngine

    def _tone(name: str, freq: float, seconds: float) -> Path:
        t = np.arange(int(seconds * 44100)) / 44100
        tone = (0.2 * np.sin(2 * math.pi * freq * t)).astype(np.float32)
        path = tmp_path / f"{name}.wav"
        sf.write(str(path), np.stack([tone, tone], axis=1), 44100, subtype="PCM_16")
        return path

    eng = MLXEngine()

    # Batched vs single won't be bit-exact: a batched run reorders float
    # reductions vs a singleton, and a zero-padded short chunk makes demucs
    # segment its (split/overlap) windows differently than a native-length one.
    # Measured worst case across these tones is ~5e-3 (-47 dB) on the noisy
    # `other` residual, spread UNIFORMLY across the signal (not a tail glitch) —
    # inaudible. The tolerance guards against gross failure (cross-item leakage,
    # padding bleeding through), which would be orders of magnitude larger; the
    # exact-shape check below is the real trim/length guard.
    _ATOL = 1e-2

    def _assert_batch_matches_singles(paths):
        singles = [eng.infer(eng.prepare(p)) for p in paths]
        batched = eng.infer_batch([eng.prepare(p) for p in paths])
        for i, single in enumerate(singles):
            # Output length must be exactly the un-padded chunk length — catches
            # a trim that leaves zero-padding in (or trims real audio out).
            for name in ("vocals", "drums", "bass", "other"):
                assert batched[i].stems[name].shape == single.stems[name].shape, (
                    f"{name} shape {batched[i].stems[name].shape} != "
                    f"{single.stems[name].shape}"
                )
                assert np.allclose(
                    batched[i].stems[name], single.stems[name], atol=_ATOL
                ), f"chunk {i} stem {name}"

    # Equal-length batch: two DISTINCT tones, so a batch that leaked across
    # items would be caught. No padding path — a clean apples-to-apples cmp.
    _assert_batch_matches_singles([_tone("a", 220.0, 2.0), _tone("b", 330.0, 2.0)])

    # Unequal-length batch: the SHORTER item is zero-padded up to the longer
    # one inside infer_batch, then trimmed back. This is the path that runs on
    # every real track (the final chunk is always short), and where demucs's
    # internal split/overlap windowing could let padding bleed into the tail.
    # Lengths chosen NOT to be multiples of the segment size so the boundary is
    # exercised, not coincidentally aligned.
    _assert_batch_matches_singles([_tone("c", 220.0, 2.0), _tone("d", 330.0, 1.3)])


def _fake_torch(capability, arch_list):
    """A stand-in for ``torch`` exposing just what ``_cuda_is_usable`` reads."""
    from types import SimpleNamespace

    return SimpleNamespace(
        cuda=SimpleNamespace(
            get_device_capability=lambda: capability,
            get_arch_list=lambda: arch_list,
        )
    )


# A modern CUDA 12.x wheel's arch list (no Pascal/Volta) — what a GTX 1050 Ti
# (sm_61) gets matched against.
_MODERN_ARCHS = ["sm_75", "sm_80", "sm_86", "sm_90", "sm_100", "sm_120"]


def test_cuda_usable_rejects_too_old_gpu():
    from engines.mlx_engine import _cuda_is_usable

    # GTX 1050 Ti is sm_61; the modern wheel ships nothing it can run.
    assert _cuda_is_usable(_fake_torch((6, 1), _MODERN_ARCHS)) is False


def test_cuda_usable_accepts_exact_and_minor_compatible():
    from engines.mlx_engine import _cuda_is_usable

    # Exact real-arch match (RTX 2080, sm_75).
    assert _cuda_is_usable(_fake_torch((7, 5), _MODERN_ARCHS)) is True
    # Same-major, higher-minor device runs an older minor's cubin (sm_80 -> 8.6).
    assert _cuda_is_usable(_fake_torch((8, 6), ["sm_75", "sm_80"])) is True
    # Backward minor is NOT compatible: an sm_86-only wheel can't run on sm_80.
    assert _cuda_is_usable(_fake_torch((8, 0), ["sm_86"])) is False


def test_cuda_usable_accepts_forward_ptx_jit():
    from engines.mlx_engine import _cuda_is_usable

    # A PTX (compute_) arch JIT-compiles forward to any newer device.
    assert _cuda_is_usable(_fake_torch((9, 0), ["compute_80"])) is True
    # ...but not backward.
    assert _cuda_is_usable(_fake_torch((7, 0), ["compute_80"])) is False


def test_cuda_usable_defaults_true_when_undetectable():
    from engines.mlx_engine import _cuda_is_usable
    from types import SimpleNamespace

    def _boom():
        raise RuntimeError("no CUDA introspection")

    torch = SimpleNamespace(
        cuda=SimpleNamespace(get_device_capability=_boom, get_arch_list=_boom)
    )
    # Can't introspect -> don't second-guess torch.
    assert _cuda_is_usable(torch) is True
