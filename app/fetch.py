"""Tier orchestration over the vendored fetch pipeline (section 1).

Four tiers, cheapest first, stopping at the first that produces usable content:

  1   plain GET with Accept: text/markdown      ~300 ms   content-negotiating sites
  2   plain GET + trafilatura                   ~500 ms   ordinary static HTML
  2.5 curl_cffi browser-TLS + trafilatura       ~700 ms   TLS/JA3-fingerprint blocks
  3   patchright Chromium + trafilatura         2-8 s     JS shells, soft bot walls

Tier 3 is the rare path. It exists so the rare page is readable at all, not so
it is readable quickly.

Division of labour with the vendored upstream (D1/D2): extraction settings and
the content-stabilisation algorithm are upstream's and are called, not copied.
Tiers 1 and 2 are re-implemented here only because upstream's `_try_http`
discards the final URL and status code that the D10 envelope must report --
the wrapper owns the tool surface, the vendored code owns the fetching.
"""
from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import urljoin, urlsplit

import httpx
import trafilatura

from . import browser as browser_mod
from . import config, envelope, identity, impersonate
from .envelope import FetchEnvelope
from .guards import SSRFError, assert_url_allowed

logger = logging.getLogger(__name__)

_MAX_REDIRECTS = 5
_MIN_EXTRACTED_CHARS = 200
_MIN_RAW_BYTES = 5000
_POLL_INTERVAL_MS = 250


def _extract(html: str, url: str) -> str | None:
    """Trafilatura with upstream's settings. One place, so tiers agree."""
    return trafilatura.extract(
        html,
        output_format="markdown",
        include_links=True,
        include_tables=True,
        url=url,
    )


def _scope_to_selector(html: str, selector: str | None) -> str:
    """Reduce the document to the subtree matching `selector` (D12).

    Applies at every tier that has a DOM. The natural fix when a page's
    boilerplate defeats the extractor. If the selector matches nothing the
    full document is returned unchanged -- a selector typo should degrade to
    normal extraction, not to an empty result.
    """
    if not selector:
        return html
    try:
        from lxml import html as lxml_html

        tree = lxml_html.fromstring(html)
        nodes = tree.cssselect(selector)
        if not nodes:
            logger.debug("selector %r matched nothing; using full document", selector)
            return html
        return "".join(lxml_html.tostring(n, encoding="unicode") for n in nodes)
    except Exception as exc:
        logger.debug("selector %r could not be applied (%s); using full document", selector, exc)
        return html


def _looks_like_js_shell(md: str | None, raw: str) -> bool:
    """Upstream's tier-2 fall-through heuristic."""
    if not md:
        return True
    if len(md) < _MIN_EXTRACTED_CHARS and "javascript" in md.lower():
        return True
    return len(md) < _MIN_EXTRACTED_CHARS and len(raw) > _MIN_RAW_BYTES


# --- tiers 1 and 2 ----------------------------------------------------------


async def _guarded_get(
    client: httpx.AsyncClient, url: str, headers: dict[str, str], timeout: float
) -> httpx.Response:
    """GET following redirects manually so every hop is SSRF-checked (D8)."""
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        await assert_url_allowed(current)
        r = await client.get(current, headers=headers, timeout=timeout)
        if r.is_redirect and r.has_redirect_location:
            current = urljoin(current, r.headers["location"])
            continue
        return r
    raise SSRFError(f"too many redirects following {url}")


async def _tier_1_2(
    url: str, mode: str, selector: str | None, timeout: float
) -> tuple[float, str, str, int] | None:
    """Tiers 1 and 2 in one request. Returns (tier, content, final_url, status)."""
    headers = identity.headers_for(mode, "GET", url)
    async with httpx.AsyncClient(follow_redirects=False, verify=True) as client:
        r = await _guarded_get(client, url, headers, timeout)

    final_url = str(r.url)
    if not r.is_success:
        logger.debug("tiers 1-2 got HTTP %d for %s", r.status_code, url)
        return None

    ctype = r.headers.get("content-type", "").split(";", 1)[0].strip().lower()

    # Tier 1: the server negotiated Markdown for us.
    if ctype in ("text/markdown", "text/x-markdown"):
        return 1, r.text, final_url, r.status_code

    if ctype and not (ctype.startswith("text/") or ctype in ("application/xhtml+xml", "application/xml")):
        logger.debug("tiers 1-2 skipping non-text content-type %r for %s", ctype, url)
        return None

    # Tier 2: extract from the HTML we already have.
    md = _extract(_scope_to_selector(r.text, selector), final_url)
    if md and not _looks_like_js_shell(md, r.text):
        return 2, md, final_url, r.status_code
    return None


# --- tier 2.5 ---------------------------------------------------------------


async def _tier_25(
    url: str, mode: str, selector: str | None, timeout: float
) -> tuple[float, str, str, int] | None:
    result = await impersonate.fetch(url, mode, timeout)
    if result is None:
        return None
    body, final_url, status = result
    if status >= 400:
        logger.debug("tier 2.5 got HTTP %d for %s", status, url)
        return None
    md = _extract(_scope_to_selector(body, selector), final_url)
    if md and not _looks_like_js_shell(md, body):
        return 2.5, md, final_url, status
    return None


# --- tier 3 -----------------------------------------------------------------


async def _poll_tier3(page, url: str, selector: str | None, budget_ms: int) -> str | None:
    """Upstream's stabilisation rule, plus: do not settle on unusable content.

    Upstream returns as soon as two consecutive polls extract identical
    non-empty text. That is right for a page that renders once, but it is
    fooled by anything that holds a *stable* placeholder -- a "Loading..."
    shell, or a bot-challenge interstitial that sits still while it works.
    Both extract cleanly and identically on consecutive polls, so the budget
    is abandoned at 250 ms and tier 3 returns the placeholder instead of the
    page. That is precisely the case tier 3 exists to handle.

    So stabilisation is necessary but not sufficient: content that classifies
    as a wall, a shell, or implausibly short does not end the poll early. It is
    kept as the running best and polling continues until either real content
    appears or the budget genuinely expires. This is what D9's raised
    poll_budget_ms is for -- challenge interstitials need settling time.
    """
    deadline = time.monotonic() + budget_ms / 1000.0
    interval = _POLL_INTERVAL_MS / 1000.0
    last: str | None = None
    best: str | None = None

    while True:
        html = await page.content()
        md = _extract(_scope_to_selector(html, selector), url)
        if md and (best is None or len(md) > len(best)):
            best = md
        if md and md == last:
            ok, _ = envelope.classify(envelope.tidy(md), tier_used=3)
            if ok:
                return md
        if time.monotonic() >= deadline:
            return best or md
        last = md
        await asyncio.sleep(interval)


async def _tier_3(
    url: str,
    mode: str,
    selector: str | None,
    *,
    headless: bool,
    wait_until: str,
    nav_timeout_ms: int,
    poll_budget_ms: int,
) -> tuple[float, str, str, int] | None:
    async with browser_mod.tier3_page(mode, headless=headless) as page:
        response = await page.goto(url, wait_until=wait_until, timeout=nav_timeout_ms)
        status = response.status if response is not None else 0
        md = await _poll_tier3(page, url, selector, poll_budget_ms)
        final_url = page.url
    if not md:
        return None
    return 3, md, final_url, status


# --- orchestration ----------------------------------------------------------


async def fetch_markdown(
    url: str,
    *,
    selector: str | None = None,
    offset: int = 0,
    identity_mode: str | None = None,
    headless: bool = True,
    wait_until: str = "domcontentloaded",
    timeout_ms: int | None = None,
    poll_budget_ms: int | None = None,
    max_chars: int | None = None,
) -> dict:
    """Run the tier ladder and return a D10 envelope."""
    started = time.monotonic()
    mode = identity.resolve_mode(identity_mode)
    nav_timeout = timeout_ms or config.NAV_TIMEOUT_MS
    poll_budget = poll_budget_ms or config.POLL_BUDGET_MS
    timings: dict[str, int] = {}

    total_budget_s = config.TOTAL_BUDGET_MS / 1000.0

    def remaining() -> float:
        return total_budget_s - (time.monotonic() - started)

    try:
        await assert_url_allowed(url)
    except SSRFError as exc:
        return envelope.error_envelope(f"refused to fetch {url}: {exc}", url=url)

    best: tuple[float, str, str, int] | None = None
    cheap_budget = config.CHEAP_TIER_BUDGET_MS / 1000.0

    def usable(candidate: tuple[float, str, str, int] | None) -> bool:
        """A tier's output is only good enough to stop on if it is usable.

        "Cheapest that produces usable content" has to mean content_ok, not
        merely non-empty. A JS shell that extracts to "Loading..." is non-empty
        and would otherwise end the ladder with a useless answer -- which is
        exactly the case tier 3 exists for. Keeping the candidate around means
        a failed escalation still returns the cheap result rather than nothing.
        """
        if candidate is None:
            return False
        ok, _ = envelope.classify(envelope.tidy(candidate[1]), tier_used=candidate[0])
        return ok

    # Tiers 1 + 2.
    t0 = time.monotonic()
    try:
        best = await asyncio.wait_for(
            _tier_1_2(url, mode, selector, min(cheap_budget / 2, max(1.0, remaining()))),
            timeout=max(1.0, min(cheap_budget, remaining())),
        )
    except SSRFError as exc:
        return envelope.error_envelope(f"refused to fetch {url}: {exc}", url=url)
    except asyncio.TimeoutError:
        logger.debug("tiers 1-2 timed out for %s", url)
    except Exception as exc:
        logger.debug("tiers 1-2 failed for %s: %s", url, exc)
    timings["tier1_2"] = int((time.monotonic() - t0) * 1000)

    # Tier 2.5.
    if not usable(best) and remaining() > 2:
        t0 = time.monotonic()
        try:
            candidate = await _tier_25(url, mode, selector, min(cheap_budget / 2, remaining()))
        except SSRFError as exc:
            return envelope.error_envelope(f"refused to fetch {url}: {exc}", url=url)
        except Exception as exc:
            logger.debug("tier 2.5 failed for %s: %s", url, exc)
            candidate = None
        if candidate is not None and (best is None or usable(candidate)):
            best = candidate
        timings["tier2_5"] = int((time.monotonic() - t0) * 1000)

    # Tier 3. `fallback` is whatever the cheap tiers managed: if the browser
    # tier fails outright we still return that rather than nothing, since a
    # thin answer beats an error the caller cannot act on.
    fallback = best
    if not usable(best):
        headed_error: str | None = None
        headed_hint: str | None = None
        if remaining() < 5 and fallback is None:
            return envelope.error_envelope(
                f"budget exhausted before the browser tier could run for {url}",
                hint=envelope.SPA_HINT,
                url=url,
            )
        if remaining() >= 5:
            t0 = time.monotonic()
            # Leave 2s of headroom so we return an envelope rather than being cut off.
            nav_ms = int(min(nav_timeout, max(1000, (remaining() - 2) * 1000)))
            poll_ms = int(min(poll_budget, max(500, (remaining() - 2) * 1000)))
            candidate = None
            try:
                candidate = await asyncio.wait_for(
                    _tier_3(
                        url,
                        mode,
                        selector,
                        headless=headless,
                        wait_until=wait_until,
                        nav_timeout_ms=nav_ms,
                        poll_budget_ms=poll_ms,
                    ),
                    timeout=max(2.0, remaining()),
                )
            except browser_mod.HeadedModeUnsupported as exc:
                # D6: always a hard error, never silently downgraded -- the
                # point is that the failure documents itself.
                return envelope.error_envelope(str(exc), url=url)
            except browser_mod.BrowserUnavailable as exc:
                # A launch failure is structural, not transient: advising a
                # larger poll_budget_ms would send the caller in circles.
                headed_error = str(exc)
                headed_hint = envelope.BROWSER_UNAVAILABLE_HINT
            except SSRFError as exc:
                return envelope.error_envelope(f"refused to fetch {url}: {exc}", url=url)
            except asyncio.TimeoutError:
                headed_error = (
                    f"total budget of {config.TOTAL_BUDGET_MS} ms expired while rendering {url}"
                )
            except Exception as exc:
                msg = str(exc)
                headed_error = (
                    f"navigation to {url} timed out after {nav_ms} ms"
                    if "imeout" in msg
                    else f"failed to fetch {url}: {exc}"
                )
            timings["tier3"] = int((time.monotonic() - t0) * 1000)
            if candidate is not None:
                best = candidate

        if best is None:
            if fallback is not None:
                best = fallback
            else:
                return envelope.error_envelope(
                    headed_error or f"no extractable content found at {url}",
                    hint=headed_hint or envelope.SPA_HINT,
                    url=url,
                )

    if best is None:
        env = FetchEnvelope(
            content="",
            tier_used=3,
            identity_mode=mode,
            final_url=url,
            content_ok=False,
            hint=envelope.WAYBACK_HINT,
            error=f"no extractable content found at {url}",
            timings_ms=timings,
        )
        return env.to_dict()

    tier, raw_md, final_url, status = best
    full = envelope.tidy(raw_md)
    content_ok, hint = envelope.classify(full, tier_used=tier)
    window, truncated, next_offset = envelope.apply_offset_and_truncate(full, offset, max_chars)

    env = FetchEnvelope(
        content=window,
        tier_used=tier,
        identity_mode=mode,
        final_url=final_url,
        http_status=status,
        content_ok=content_ok,
        truncated=truncated,
        next_offset=next_offset,
        hint=hint,
        timings_ms=timings,
    )
    _log_call(url, "fetch_url_as_markdown", tier, mode, timings, content_ok, len(window))
    return env.to_dict()


def _log_call(
    url: str,
    tool: str,
    tier: float | None,
    mode: str,
    timings: dict[str, int],
    content_ok: bool,
    size: int,
) -> None:
    """One structured line per call (section 7). Host only, never the full URL."""
    host = urlsplit(url).hostname or "?"
    logger.info(
        "tool=%s host=%s tier=%s identity=%s timings=%s content_ok=%s bytes=%d",
        tool,
        host,
        tier,
        mode,
        ",".join(f"{k}={v}" for k, v in timings.items()) or "-",
        content_ok,
        size,
    )
