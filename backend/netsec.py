"""SSRF / URL policy for the public-facing ingest paths.

``validate_public_url`` is the single gate used both when a job is submitted
(``POST /process``) and again when its stored URL is fetched on demand
(``GET /video``), so a job seeded under one URL can't have a different one
fetched later, and the on-disk ``meta.url`` is re-checked on the delayed export.

It enforces, in order:

* http(s) scheme only (no ``file://`` local-file reads, no ``ftp`` etc.);
* a host that isn't ``localhost``;
* in public mode, a *positive* host allowlist (so the box can't be used as an
  open download proxy for arbitrary sites);
* an internal-IP block-list covering loopback / private / link-local / reserved
  / metadata addresses, including the non-canonical IPv4 encodings and plain
  hostnames that merely resolve to an internal IP.

Note the front-door IP check resolves DNS once; it does **not** by itself close
DNS-rebinding or redirect-to-internal (yt-dlp re-resolves per fetch and exposes
no per-redirect host hook). The durable control for those is the host-firewall
egress filter on the service user (see docs/remote-deployment/, Phase 4).
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlsplit

from config import SETTINGS

log = logging.getLogger(__name__)


class UrlNotAllowed(ValueError):
    """A submitted URL failed the scheme / host / IP policy. Subclasses
    ``ValueError`` so a pydantic field validator surfaces it as a 422."""


def _resolve_host_ip_candidates(host: str) -> list:
    """Every IP ``host`` could resolve to, for the SSRF block-list check.

    Checking only ``ipaddress.ip_address(host)`` was bypassable: it parses just
    canonical IPv4/IPv6, so decimal (``2130706433``), hex (``0x7f000001``), octal
    (``0177.0.0.1``) and short-form (``127.1``) literals — and plain hostnames
    that simply resolve to an internal IP — sailed through and yt-dlp would then
    fetch e.g. 127.0.0.1 or the cloud-metadata address. We normalise all of those
    to the real address instead. Raises ``ValueError`` for a hostname that won't
    resolve (yt-dlp couldn't fetch it anyway).
    """
    try:
        return [ipaddress.ip_address(host)]  # canonical literal
    except ValueError:
        pass
    try:
        # inet_aton normalises the non-canonical IPv4 encodings above
        # (decimal/hex/octal/short-form) with no DNS lookup.
        return [ipaddress.ip_address(socket.inet_aton(host))]
    except OSError:
        pass
    # A real hostname: resolve it the way the fetch will.
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, ValueError):
        raise ValueError("url host could not be resolved")
    candidates: list = []
    for info in infos:
        try:
            candidates.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            # A non-IP addrinfo entry (not expected for SOCK_STREAM); skip it but
            # log so a silent drop is at least traceable.
            log.debug("skipping unparseable resolved address %r", info[4][0])
    return candidates


def _is_blocked_host_ip(ip) -> bool:
    """True if ``ip`` is an internal/special address the backend must not fetch."""
    # Unwrap IPv4-mapped IPv6 (::ffff:127.0.0.1) so the v4 rules apply.
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_unspecified
        or ip.is_multicast
    )


def _host_allowed(host: str) -> bool:
    """True if ``host`` is, or is a subdomain of, an allowlisted host."""
    h = host.lower().rstrip(".")
    return any(h == d or h.endswith("." + d) for d in SETTINGS.allowed_url_hosts)


def validate_public_url(url: str) -> str:
    """Validate ``url`` for ingest; return the trimmed URL or raise
    :class:`UrlNotAllowed`. The host allowlist is enforced only in public mode,
    so local/dev use keeps accepting any public http(s) URL."""
    v = url.strip()
    parts = urlsplit(v)
    if parts.scheme not in ("http", "https"):
        raise UrlNotAllowed("url must be an http(s) URL")
    host = parts.hostname
    if not host:
        raise UrlNotAllowed("url must include a host")
    lowered = host.lower()
    if lowered == "localhost" or lowered.endswith(".localhost"):
        raise UrlNotAllowed("url host is not allowed")
    if SETTINGS.public and not _host_allowed(host):
        raise UrlNotAllowed("url host is not on the allowlist")
    for ip in _resolve_host_ip_candidates(host):
        if _is_blocked_host_ip(ip):
            raise UrlNotAllowed("url host is not allowed")
    return v
