"""The URL host allowlist and the yt-dlp extractor allowlist must agree with the
sites the extension actually drives (youtube incl. nocookie, facebook incl.
reels)."""

from __future__ import annotations

import socket as _socket

import pytest

import config


@pytest.fixture
def public(monkeypatch):
    object.__setattr__(config.SETTINGS, "public", True)
    try:
        yield
    finally:
        object.__setattr__(config.SETTINGS, "public", False)


def _stub_public_dns(monkeypatch):
    monkeypatch.setattr(
        _socket,
        "getaddrinfo",
        lambda host, *a, **k: [
            (_socket.AF_INET, _socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))
        ],
    )


def test_youtube_nocookie_host_is_allowlisted(public, monkeypatch):
    import netsec

    _stub_public_dns(monkeypatch)
    # Privacy-embed page URL must pass the public host allowlist.
    assert netsec.validate_public_url(
        "https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ"
    )


def test_facebook_reel_extractor_allowlisted():
    assert "facebook:reel" in config.SETTINGS.allowed_extractors
    assert "facebook" in config.SETTINGS.allowed_extractors
