"""Chromium lifecycle: lazy launch, idle reaper, concurrency lock (D4, D7, D14).

The distinction that matters here, and the main way this deployment goes wrong
if it is blurred:

  * *Installing* Chromium is a ~300 MB download. It belongs in the build
    command and is baked into the deployed image. This module never installs.
  * *Launching* Chromium is a ~2-5 s process start. It belongs in the request
    path, lazily, on the first call that reaches tier 3. That latency is
    accepted by design.

If the binary is missing, this fails immediately with a clear message rather
than triggering a download inside a request timeout (D4).

D7: the browser is closed after BROWSER_IDLE_TIMEOUT_S seconds of no tier-3
activity and relaunched lazily on the next one. An instance that has scaled up
but gone quiet should not hold ~400 MB, and a long-lived browser accumulates
state that makes the next fetch less reliable, not more.

D14: one browser, one page at a time, behind a lock. Tiers 1, 2 and 2.5 stay
concurrent.
"""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager

from . import config, identity
from .guards import SSRFError, assert_url_allowed

logger = logging.getLogger(__name__)


class BrowserUnavailable(RuntimeError):
    """Chromium is not installed, or could not be launched."""


class HeadedModeUnsupported(RuntimeError):
    """headless=False was requested (D6). There is no display here."""


# D14: serialises the whole tier-3 page lifecycle, not just the launch.
_tier3_lock = asyncio.Lock()

# Guards the launch/close transitions themselves.
_state_lock = asyncio.Lock()

_playwright = None
_browser = None
_last_used: float = 0.0
_reaper_task: asyncio.Task | None = None

_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",  # /dev/shm is small in containers; without this Chromium crashes on heavy pages
    "--no-sandbox",  # no user namespaces in the Autoscale container
    "--disable-gpu",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
]


def _resolve_executable() -> str:
    path = config.chromium_path()
    if path is None:
        raise BrowserUnavailable(
            "Chromium is not installed in this image. It must be installed at "
            "build time with `patchright install chromium` and "
            f"PLAYWRIGHT_BROWSERS_PATH={config.BROWSERS_PATH}. It is never "
            "downloaded at request time."
        )
    return str(path)


async def _launch() -> None:
    """Start playwright and Chromium. Caller must hold _state_lock."""
    global _playwright, _browser
    from patchright.async_api import async_playwright

    executable = _resolve_executable()
    t0 = time.monotonic()
    _playwright = await async_playwright().start()
    try:
        _browser = await _playwright.chromium.launch(
            headless=True,
            executable_path=executable,
            args=_LAUNCH_ARGS,
        )
    except Exception as exc:
        try:
            await _playwright.stop()
        except Exception:
            pass
        _playwright = None
        raise BrowserUnavailable(f"Chromium failed to launch: {exc}") from exc
    logger.info(
        "chromium launched in %d ms from %s", int((time.monotonic() - t0) * 1000), executable
    )


async def _shutdown() -> None:
    """Close Chromium and stop playwright. Caller must hold _state_lock."""
    global _playwright, _browser
    if _browser is not None:
        try:
            await _browser.close()
        except Exception as exc:
            logger.debug("error closing browser: %s", exc)
        _browser = None
    if _playwright is not None:
        try:
            await _playwright.stop()
        except Exception as exc:
            logger.debug("error stopping playwright: %s", exc)
        _playwright = None


async def _reaper() -> None:
    """Close the browser once it has been idle past the timeout (D7)."""
    idle = max(5, config.BROWSER_IDLE_TIMEOUT_S)
    try:
        while True:
            await asyncio.sleep(min(10, idle))
            async with _state_lock:
                if _browser is None:
                    continue
                # Never reap mid-fetch: the lock being held means tier 3 is live.
                if _tier3_lock.locked():
                    continue
                if time.monotonic() - _last_used >= idle:
                    logger.info("reaping idle chromium after %ds", idle)
                    await _shutdown()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # a dead reaper must not take the server down
        logger.error("browser reaper stopped unexpectedly: %s", exc)


async def _ensure_browser():
    global _last_used, _reaper_task
    async with _state_lock:
        if _browser is None or not _browser.is_connected():
            if _browser is not None:
                await _shutdown()
            await _launch()
        if _reaper_task is None or _reaper_task.done():
            _reaper_task = asyncio.create_task(_reaper())
        _last_used = time.monotonic()
    return _browser


async def shutdown() -> None:
    """Close everything. Called from the server lifespan."""
    global _reaper_task
    if _reaper_task is not None:
        _reaper_task.cancel()
        try:
            await _reaper_task
        except (asyncio.CancelledError, Exception):
            pass
        _reaper_task = None
    async with _state_lock:
        await _shutdown()


def is_running() -> bool:
    return _browser is not None and _browser.is_connected()


async def _guard_route(route) -> None:
    """Abort any subresource or browser-driven redirect aimed somewhere D8 forbids.

    Installed on every page so the guard covers the vectors the httpx-side
    check cannot see once Chromium is driving.
    """
    url = route.request.url
    scheme = url.split(":", 1)[0].lower() if ":" in url else ""
    if scheme in ("http", "https"):
        try:
            await assert_url_allowed(url)
        except SSRFError:
            try:
                await route.abort()
            except Exception:
                pass
            return
        except Exception:
            pass
    try:
        await route.continue_()
    except Exception:
        pass


@asynccontextmanager
async def tier3_page(
    mode: str,
    *,
    headless: bool = True,
    viewport: dict[str, int] | None = None,
):
    """Yield a fresh guarded page in a fresh context, serialised (D14).

    A new context per call is deliberate: no cookie or storage carry-over
    between fetches, which is both the clean-context promise in the tool
    descriptions and one less source of "worked yesterday" flakiness.
    """
    if not headless:
        raise HeadedModeUnsupported(
            "headless=False needs a display. This server runs in a container "
            "with no X display and no virtual framebuffer, so a headed browser "
            "cannot start here. Run the upstream package on a local machine if "
            "you need headed mode."
        )

    global _last_used
    async with _tier3_lock:
        browser = await _ensure_browser()
        context = await browser.new_context(
            viewport=viewport or {"width": 1280, "height": 800},
            device_scale_factor=1,
            user_agent=identity.STEALTH_UA if mode == "stealth" else identity.DECLARED_UA,
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers=_context_headers(mode),
        )
        try:
            page = await context.new_page()
            await page.route("**/*", _guard_route)
            yield page
        finally:
            try:
                await context.close()
            except Exception as exc:
                logger.debug("error closing context: %s", exc)
            _last_used = time.monotonic()


def _context_headers(mode: str) -> dict[str, str]:
    """Extra headers for the browser context.

    Chromium sets its own UA, Accept, and Sec-Fetch-* headers; overriding them
    from Python desynchronises the header set from the TLS fingerprint, which
    is exactly the signal tier 3 exists to avoid producing. So in stealth mode
    we add nothing, and in declared mode we add only the identifying headers.
    """
    if mode != "declared":
        return {}
    return {"from": config.CONTACT_URL, "signature-agent": f'"{identity.signature_agent_url()}"'}
