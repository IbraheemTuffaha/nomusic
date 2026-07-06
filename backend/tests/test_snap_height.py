"""_snap_height must keep /video bounded in public mode (F8).

In public mode every result is an allowlisted, bounded height — including the
"best" (omitted / <=0) case, which previously fell through to true
best-available and re-opened the 8K download + re-encode amplification.
"""

from __future__ import annotations

import config
from routes.media import _snap_height


def _pub(**kw):
    saved = {k: getattr(config.SETTINGS, k) for k in kw}
    for k, v in kw.items():
        object.__setattr__(config.SETTINGS, k, v)
    return lambda: [object.__setattr__(config.SETTINGS, k, v) for k, v in saved.items()]


def test_public_best_snaps_to_tallest_allowed_not_best_available():
    restore = _pub(public=True, allowed_video_heights=(360, 480, 720, 1080))
    try:
        assert _snap_height(None) == 1080  # was None (best-available) -> bypass
        assert _snap_height(0) == 1080
        assert _snap_height(-1) == 1080
        assert _snap_height(5000) == 1080  # 4K/8K request snapped down
        assert _snap_height(700) == 720  # nearest
        assert _snap_height(1080) == 1080  # exact allowed
    finally:
        restore()


def test_public_empty_allowlist_caps_hard():
    restore = _pub(public=True, allowed_video_heights=())
    try:
        assert _snap_height(None) == 1080
        assert _snap_height(9999) == 1080
    finally:
        restore()


def test_dev_keeps_best_and_clamp():
    restore = _pub(public=False)
    try:
        assert _snap_height(None) is None  # dev: true best-available
        assert _snap_height(0) is None
        assert _snap_height(9999) == 4320  # dev clamp
        assert _snap_height(100) == 144
    finally:
        restore()
