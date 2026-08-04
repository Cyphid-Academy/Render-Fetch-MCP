"""SSRF guard tests (D8, acceptance test T7)."""
from __future__ import annotations

import pytest

from app import config, guards
from app.guards import SSRFError


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/x",
        "gopher://example.com/",
        "data:text/html,<h1>hi</h1>",
        "javascript:alert(1)",
    ],
)
async def test_non_http_schemes_rejected(url):
    with pytest.raises(SSRFError) as exc:
        await guards.assert_url_allowed(url)
    assert "not allowed" in str(exc.value) or "no host" in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url,reason",
    [
        ("http://127.0.0.1/", "loopback"),
        ("http://localhost:5000/", "loopback"),
        ("http://[::1]/", "loopback"),
        ("http://169.254.169.254/", "link-local"),
        ("http://10.0.0.1/", "private"),
        ("http://192.168.1.1/", "private"),
        ("http://172.16.0.1/", "private"),
        ("http://[fd00::1]/", "private"),
        # Python classifies 0.0.0.0 as private before it is ever judged
        # unspecified; either label is a refusal, which is what matters.
        ("http://0.0.0.0/", "private"),
        # Integer and hex IP literals that slip past dotted-quad checks.
        ("http://2130706433/", "loopback"),
        ("http://0x7f000001/", "loopback"),
    ],
)
async def test_blocked_address_classes(url, reason):
    with pytest.raises(SSRFError) as exc:
        await guards.assert_url_allowed(url)
    assert reason in str(exc.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://100.64.0.1/",  # CGNAT
        "http://100.127.255.254/",
        "http://198.18.0.1/",  # benchmarking
        "http://192.0.0.1/",  # IETF protocol assignments
    ],
)
async def test_ranges_python_does_not_call_private(url):
    """The gap this module exists to close.

    ipaddress.is_private is False for CGNAT and these reserved ranges, so the
    upstream classifier passes them. They are still reachable from inside a
    datacenter and must not be fetchable.
    """
    with pytest.raises(SSRFError):
        await guards.assert_url_allowed(url)


@pytest.mark.asyncio
async def test_ipv4_mapped_cgnat_is_unwrapped():
    with pytest.raises(SSRFError):
        await guards.assert_url_allowed("http://[::ffff:100.64.0.1]/")


@pytest.mark.asyncio
async def test_own_origin_rejected(monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_ORIGIN", "https://render-fetch.replit.app")
    with pytest.raises(SSRFError) as exc:
        await guards.assert_url_allowed("https://render-fetch.replit.app/mcp/token")
    assert "own origin" in str(exc.value)


@pytest.mark.asyncio
async def test_no_host():
    with pytest.raises(SSRFError):
        await guards.assert_url_allowed("http:///path")


@pytest.mark.asyncio
async def test_public_address_allowed():
    # 8.8.8.8 is a literal, so this asserts classification without a DNS round trip.
    await guards.assert_url_allowed("http://8.8.8.8/")


@pytest.mark.asyncio
async def test_is_url_allowed_returns_bool():
    assert await guards.is_url_allowed("http://8.8.8.8/") is True
    assert await guards.is_url_allowed("http://127.0.0.1/") is False
