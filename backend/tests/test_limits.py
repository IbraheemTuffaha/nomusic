"""M3-T3 input, download and working-set policy regressions."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from nomusic.pipeline import downloader
from nomusic.pipeline.cache import CacheMeta, JobCache
from nomusic.pipeline.downloader import ResourceLimitExceeded, ResourceLimits, VideoMetadata
from nomusic.pipeline.processor import Processor


def test_metadata_rejects_unknown_or_oversized_duration():
    limits = ResourceLimits(max_duration_seconds=10)
    with pytest.raises(RuntimeError, match="could not determine duration"):
        downloader._metadata_from_info({"title": "live", "duration": None}, "https://x")
    info = VideoMetadata("id", "long", 11, "fixture", "https://x")
    with pytest.raises(ResourceLimitExceeded, match="duration"):
        downloader.validate_metadata(info, limits)


def test_oversized_cached_source_is_removed(tmp_path):
    out = tmp_path / "source"
    out.mkdir()
    (out / "source.wav").write_bytes(b"x" * 11)
    with pytest.raises(ResourceLimitExceeded, match="source file"):
        downloader.download_source(
            "https://example.test/source", out,
            limits=ResourceLimits(max_source_bytes=10),
        )
    assert not (out / "source.wav").exists()


def test_download_progress_rejects_known_total_before_bytes_finish():
    seen = []
    with pytest.raises(ResourceLimitExceeded, match="download exceeded"):
        downloader._guard_download_progress(
            {"status": "downloading", "downloaded_bytes": 2, "total_bytes": 11},
            seen.append,
            ResourceLimits(max_source_bytes=10),
            "source",
        )
    assert seen == []


def test_short_completed_source_cannot_be_reused(monkeypatch, tmp_path):
    source = tmp_path / "source.wav"
    source.write_bytes(b"source")
    monkeypatch.setattr(
        downloader.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="2.0\n"),
    )
    with pytest.raises(ResourceLimitExceeded, match="metadata requires"):
        downloader.validate_source_file(
            source, 9.5, ResourceLimits(final_chunk_tolerance_seconds=1.0)
        )


def test_resumed_metadata_is_rejected_after_tightening(tmp_path):
    cache = JobCache(tmp_path / "cache")
    key = cache.key("https://example.test/v", "fake", ["vocals"], chunk_seconds=10, chunk_overlap_seconds=0.5)
    cache.save_meta(key, CacheMeta(
        url="https://example.test/v", model="fake", keep_stems=["vocals"],
        duration_seconds=100, chunk_seconds=10, chunk_overlap_seconds=0.5,
        total_chunks=11,
    ))
    processor = Processor(
        engine=object(), cache=cache, chunk_seconds=10, chunk_overlap_seconds=0.5,
        limits=ResourceLimits(max_duration_seconds=60),
    )
    with pytest.raises(RuntimeError, match="duration limit"):
        processor.prepare_job("https://example.test/v", model="fake", keep_stems=["vocals"])


def test_video_height_policy_rejects_2160_request():
    with pytest.raises(ResourceLimitExceeded, match="height"):
        downloader.download_video(
            "https://example.test/video", Path("/tmp/unused"), max_height=2160,
            limits=ResourceLimits(max_video_height=720),
        )

