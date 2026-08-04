"""URL validation and SSRF checks (D8).

Builds on the vendored upstream guard rather than replacing it: upstream
already handles scheme filtering, DNS resolution, IP-literal obfuscation
(integer and hex forms), and the IPv4-in-IPv6 embeddings. Two things D8
requires that it does not cover are added here:

  * CGNAT, 100.64.0.0/10. Python's ``ip_address.is_private`` returns False
    for this range, so upstream's classifier lets it through. It is carrier
    space and routinely reachable from inside a datacenter.
  * The server's own origin. A public fetch endpoint that will fetch itself
    is a trivially available amplification loop.

Everything raises SSRFError, which the envelope layer turns into a structured
refusal naming the reason (T7).
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

from vendor.web_to_markdown_mcp._ssrf import (  # noqa: F401  (re-exported)
    SSRFError,
    assert_url_allowed as _upstream_assert,
)

from . import config

# Ranges Python's ipaddress does not classify as private but which must not be
# reachable from a public fetch endpoint.
_EXTRA_BLOCKED = [
    (ipaddress.ip_network("100.64.0.0/10"), "cgnat"),
    (ipaddress.ip_network("192.0.0.0/24"), "ietf-protocol-assignment"),
    (ipaddress.ip_network("198.18.0.0/15"), "benchmarking"),
    (ipaddress.ip_network("64:ff9b::/96"), "nat64"),
]

ALLOWED_SCHEMES = ("http", "https")


def _extra_reason(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Classify the ranges upstream's guard does not treat as blocked."""
    # Unwrap IPv4-in-IPv6 so a mapped CGNAT address is judged on the IPv4.
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = ip.ipv4_mapped or ip.sixtofour
        if embedded is None and ip.teredo is not None:
            embedded = ip.teredo[1]
        if embedded is not None:
            inner = _extra_reason(embedded)
            if inner:
                return inner
    for net, reason in _EXTRA_BLOCKED:
        if ip.version == net.version and ip in net:
            return reason
    return None


def _own_origin_hosts() -> set[str]:
    hosts: set[str] = set()
    if config.PUBLIC_ORIGIN:
        host = urlsplit(config.PUBLIC_ORIGIN).hostname
        if host:
            hosts.add(host.lower())
    return hosts


async def _assert_not_extra_blocked(url: str) -> None:
    """Resolve the host and reject the ranges upstream misses."""
    parts = urlsplit(url)
    host = parts.hostname
    if not host:
        return
    scheme = parts.scheme.lower()
    port = parts.port or (443 if scheme == "https" else 80)

    # IP literal: classify directly, no resolution needed.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        reason = _extra_reason(literal)
        if reason:
            raise SSRFError(f"{host} maps to a {reason} address")
        return

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        # Upstream's guard already raised or will raise on this; nothing to add.
        return
    for info in infos:
        reason = _extra_reason(ipaddress.ip_address(info[4][0]))
        if reason:
            raise SSRFError(f"{host} maps to a {reason} address")


async def assert_url_allowed(url: str) -> None:
    """Raise SSRFError unless the URL is safe to fetch.

    Safe to call on every redirect hop and every browser subresource, which is
    exactly what D8 requires -- the initial URL being clean says nothing about
    where a 302 points.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise SSRFError(f"scheme {parts.scheme or '(none)'!r} not allowed (http/https only)")

    host = (parts.hostname or "").lower()
    if not host:
        raise SSRFError("no host in URL")
    if host in _own_origin_hosts():
        raise SSRFError(f"{host} is this server's own origin")

    await _upstream_assert(url)
    await _assert_not_extra_blocked(url)


async def is_url_allowed(url: str) -> bool:
    try:
        await assert_url_allowed(url)
        return True
    except SSRFError:
        return False
