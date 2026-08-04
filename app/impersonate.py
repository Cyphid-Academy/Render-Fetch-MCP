"""Tier 2.5 -- curl_cffi browser-TLS impersonation.

Sits between "plain HTTP" and "launch Chromium". A meaningful share of blocks
are decided on the TLS/JA3 handshake alone, before a single byte of HTTP is
read: the request never had a chance, and no amount of header tuning at tier 2
changes that. curl_cffi replays a real browser's TLS and HTTP/2 fingerprint,
which clears those blocks at roughly 1/10th the cost of a browser launch.

It does not execute JavaScript. A page that is a JS shell still falls through
to tier 3.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from . import identity
from .guards import SSRFError, assert_url_allowed

logger = logging.getLogger(__name__)

# The impersonation target. Kept roughly in step with identity.CHROME_MAJOR;
# curl_cffi only ships fingerprints for specific releases, so this is chosen
# from what the installed version actually supports rather than set freely.
_IMPERSONATE = "chrome"

_MAX_REDIRECTS = 5


class ImpersonateUnavailable(RuntimeError):
    """curl_cffi is not installed or could not be imported."""


def _session_factory():
    try:
        from curl_cffi import requests as cffi_requests
    except Exception as exc:  # pragma: no cover - import guard
        raise ImpersonateUnavailable(str(exc)) from exc
    return cffi_requests


def _pick_impersonate(cffi_requests: Any) -> str:
    """Choose a fingerprint the installed curl_cffi actually knows.

    The named targets churn between releases (chrome116, chrome124, chrome...);
    asking for one that is gone raises at request time. Prefer the generic
    "chrome" alias when available since it tracks the newest supported build.
    """
    try:
        from curl_cffi.requests import BrowserType

        names = {b.value if hasattr(b, "value") else str(b) for b in BrowserType}
        if _IMPERSONATE in names:
            return _IMPERSONATE
        chromes = sorted(n for n in names if n.startswith("chrome") and n[6:].isdigit())
        if chromes:
            return chromes[-1]
    except Exception:
        pass
    return _IMPERSONATE


async def fetch(url: str, mode: str, timeout_s: float) -> tuple[str, str, int] | None:
    """Fetch with browser-TLS impersonation.

    Returns (body_text, final_url, status) or None if the tier could not
    produce a usable response. Every redirect hop is SSRF-checked, so
    redirects are followed manually rather than by the library (D8).

    SSRFError propagates -- a refusal is a result, not a tier failure to be
    swallowed and retried at the next tier.
    """
    try:
        cffi_requests = _session_factory()
    except ImpersonateUnavailable as exc:
        logger.warning("tier 2.5 unavailable: %s", exc)
        return None

    impersonate = _pick_impersonate(cffi_requests)
    headers = identity.headers_for(mode, "GET", url)
    # curl_cffi sets its own UA and TLS profile from `impersonate`; letting our
    # stealth UA override it would desynchronise the fingerprint from the
    # header set, which is worse than either alone.
    if mode == "stealth":
        headers = {k: v for k, v in headers.items() if k.lower() not in ("user-agent", "accept-encoding")}

    # curl_cffi is synchronous, and each redirect hop needs an await between it
    # and the next for the SSRF check. So: one hop per thread call, loop here.
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        await assert_url_allowed(current)
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(_one_hop, cffi_requests, current, headers, impersonate, timeout_s),
                timeout=timeout_s + 2,
            )
        except SSRFError:
            raise
        except asyncio.TimeoutError:
            logger.debug("tier 2.5 timed out on %s", current)
            return None
        except Exception as exc:
            logger.debug("tier 2.5 failed on %s: %s", current, exc)
            return None

        if result is None:
            return None
        kind, payload = result
        if kind == "redirect":
            current = payload
            continue
        body, status = payload
        return body, current, status

    logger.debug("tier 2.5 exceeded redirect limit on %s", url)
    return None


def _one_hop(
    cffi_requests: Any, url: str, headers: dict[str, str], impersonate: str, timeout_s: float
) -> tuple[str, Any] | None:
    """One request, no redirect following. Runs in a worker thread."""
    from urllib.parse import urljoin

    resp = cffi_requests.get(
        url,
        headers=headers,
        impersonate=impersonate,
        timeout=timeout_s,
        allow_redirects=False,
        verify=True,
    )
    status = resp.status_code
    if status in (301, 302, 303, 307, 308):
        location = resp.headers.get("location")
        if not location:
            return None
        return "redirect", urljoin(url, location)
    return "response", (resp.text, status)
