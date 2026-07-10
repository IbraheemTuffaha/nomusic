"""Post-download size backstop: yt-dlp's max_filesize doesn't fire for fragmented
DASH/HLS, so an over-cap file must be deleted and rejected (public mode)."""

from __future__ import annotations

import config
import pytest
from pipeline import downloader


def _pub(**kw):
    saved = {k: getattr(config.SETTINGS, k) for k in kw}
    for k, v in kw.items():
        object.__setattr__(config.SETTINGS, k, v)
    return lambda: [object.__setattr__(config.SETTINGS, k, v) for k, v in saved.items()]


def test_oversize_file_deleted_and_rejected_in_public(tmp_path):
    f = tmp_path / "video.mp4"
    f.write_bytes(b"x" * 2000)
    restore = _pub(public=True)
    try:
        with pytest.raises(RuntimeError, match="exceeds size cap"):
            downloader._enforce_downloaded_size(f, cap_bytes=1000, what="video")
        assert not f.exists()  # deleted, so it can't be served or fill disk
    finally:
        restore()


def test_under_cap_file_kept(tmp_path):
    f = tmp_path / "video.mp4"
    f.write_bytes(b"x" * 500)
    restore = _pub(public=True)
    try:
        downloader._enforce_downloaded_size(f, cap_bytes=1000, what="video")
        assert f.exists()
    finally:
        restore()


def test_noop_in_dev(tmp_path):
    f = tmp_path / "video.mp4"
    f.write_bytes(b"x" * 2000)
    restore = _pub(public=False)
    try:
        downloader._enforce_downloaded_size(f, cap_bytes=1000, what="video")
        assert f.exists()  # dev: no cap enforced
    finally:
        restore()
