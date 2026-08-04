"""capture_page -- screenshot tiling and encoding (section 11).

The constraint that drives every decision here: the obvious implementation,
`full_page=True` returning one tall PNG, is close to useless for a vision
model. Claude resizes an image so neither edge exceeds the long-edge limit and
the visual token cost ceil(w/28) * ceil(h/28) fits the budget. A
1280x12000 full-page capture is ~15 megapixels; by the time it has been
resized to fit, body text is a few pixels tall and unreadable. The screenshot
arrives, costs a full image's tokens, and conveys nothing.

So: tile, do not stretch. Each tile is sized so that no resize happens at all,
which is what keeps text crisp.

The tile size is computed from the model's published limits rather than
hard-coded, so the constant survives a tier change (section 11, step 4).
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import math
import time

from . import browser as browser_mod
from . import config, identity
from .guards import SSRFError, assert_url_allowed

logger = logging.getLogger(__name__)

# --- token geometry ---------------------------------------------------------

# Claude's image tokenisation: cost is ceil(w/28) * ceil(h/28) visual tokens,
# and the image is downscaled if either edge exceeds the pixel limit or the
# cost exceeds the token budget.
PATCH = 28

# Standard tier. The high-resolution tier (4784 tokens / 2576 px) is wider, but
# sizing to it would produce tiles that get resized for any caller on the
# standard tier -- the failure this module exists to avoid. Sizing to the
# smaller budget is correct on both.
TOKEN_BUDGET = 1568
PIXEL_LIMIT = 1568

VIEWPORT_W = 1280
VIEWPORT_H = 800

# Beyond this a tile is re-encoded as JPEG (section 11, step 8).
_MAX_TILE_BYTES = 4 * 1024 * 1024

# Entropy below this means the tile is blank or near-blank (step 5). Shannon
# entropy of a greyscale histogram: a flat white region lands near 0, a region
# with a heading and a paragraph lands well above 2.
_BLANK_ENTROPY = 1.2
# A tile can be low-entropy yet still carry a little text; require both signals.
_BLANK_INK_RATIO = 0.004


def tile_geometry(
    width: int = VIEWPORT_W,
    viewport_height: int = VIEWPORT_H,
    token_budget: int = TOKEN_BUDGET,
    pixel_limit: int = PIXEL_LIMIT,
) -> tuple[int, int, int]:
    """Return (tile_width, tile_height, tokens_per_tile).

    Derived, not hard-coded: the tallest tile of this width whose token cost
    still fits the budget and whose edges stay under the pixel limit. At the
    standard tier and a 1280 px viewport this yields 1280x800 at 1334 tokens.
    """
    width = min(width, pixel_limit)
    cols = math.ceil(width / PATCH)
    max_rows = token_budget // cols
    max_height = min(pixel_limit, max_rows * PATCH)
    height = min(viewport_height, max_height)
    if height <= 0:
        raise ValueError(f"no tile height fits a {width}px-wide tile in {token_budget} tokens")
    tokens = cols * math.ceil(height / PATCH)
    return width, height, tokens


TILE_W, TILE_H, TOKENS_PER_TILE = tile_geometry()


# --- page settling ----------------------------------------------------------

_FREEZE_CSS = """
*, *::before, *::after {
  animation-duration: 0s !important;
  animation-delay: 0s !important;
  animation-iteration-count: 1 !important;
  transition-duration: 0s !important;
  transition-delay: 0s !important;
  scroll-behavior: auto !important;
  caret-color: transparent !important;
}
html { scroll-behavior: auto !important; }
"""

_SCROLL_AND_RETURN = """
async () => {
  const step = Math.floor(window.innerHeight * 0.9);
  const limit = 30;
  let last = -1;
  for (let i = 0; i < limit; i++) {
    window.scrollTo(0, window.scrollY + step);
    await new Promise(r => setTimeout(r, 120));
    const h = document.documentElement.scrollHeight;
    if (window.scrollY + window.innerHeight >= h - 2) break;
    if (h === last && i > 3) break;
    last = h;
  }
  window.scrollTo(0, 0);
  await new Promise(r => setTimeout(r, 200));
  return document.documentElement.scrollHeight;
}
"""


async def _settle(page, poll_budget_ms: int) -> int:
    """Trigger lazy-loading, freeze animation, return to the top (step 2).

    Returns the full scroll height, measured only once it has stopped
    changing. Measuring it any earlier is a race that quietly truncates the
    capture: a page whose content is injected a few hundred ms after load
    reports its pre-render height, and mode="full" then tiles only the shell.
    """
    await page.add_style_tag(content=_FREEZE_CSS)
    await page.emulate_media(reduced_motion="reduce")
    try:
        await asyncio.wait_for(
            page.evaluate(_SCROLL_AND_RETURN), timeout=max(2.0, poll_budget_ms / 1000.0)
        )
    except Exception as exc:
        logger.debug("scroll settle incomplete: %s", exc)
    try:
        await page.wait_for_load_state("networkidle", timeout=min(3000, poll_budget_ms))
    except Exception:
        pass
    return await _stable_height(page, poll_budget_ms)


async def _stable_height(page, budget_ms: int) -> int:
    """Poll scrollHeight until two consecutive reads agree, or the budget ends."""
    deadline = time.monotonic() + max(1.0, min(budget_ms, 8000) / 1000.0)
    last = -1
    height = VIEWPORT_H
    while True:
        try:
            height = int(await page.evaluate("document.documentElement.scrollHeight") or VIEWPORT_H)
        except Exception:
            return max(int(height), VIEWPORT_H)
        if height == last:
            break
        if time.monotonic() >= deadline:
            break
        last = height
        await asyncio.sleep(0.25)
    return max(height, VIEWPORT_H)


# --- tile scoring -----------------------------------------------------------


def score_tile(png_bytes: bytes) -> tuple[float, float]:
    """Return (entropy, ink_ratio) for a tile.

    Entropy is over the greyscale histogram; ink_ratio is the share of pixels
    that differ meaningfully from the tile's modal (background) value. Long
    pages have large dead regions and each one costs ~1300 tokens to say
    nothing, so this is what makes mode="full" affordable.
    """
    try:
        from PIL import Image
    except Exception:  # pragma: no cover - Pillow is a hard dep, but never fail a capture on scoring
        return 99.0, 1.0

    with Image.open(io.BytesIO(png_bytes)) as im:
        grey = im.convert("L")
        # Downsample: entropy of the layout, not of font antialiasing.
        grey.thumbnail((320, 320))
        hist = grey.histogram()

    total = sum(hist)
    if total <= 0:
        return 0.0, 0.0
    entropy = 0.0
    modal = max(hist)
    for count in hist:
        if count:
            p = count / total
            entropy -= p * math.log2(p)
    ink_ratio = 1.0 - (modal / total)
    return entropy, ink_ratio


def is_blank(entropy: float, ink_ratio: float) -> bool:
    return entropy < _BLANK_ENTROPY and ink_ratio < _BLANK_INK_RATIO


def _encode(png_bytes: bytes) -> tuple[bytes, str]:
    """PNG unless it is too large, then JPEG q85 (step 8).

    PNG keeps small text sharp; JPEG artefacts around small text are exactly
    what hurts legibility, so this is a size fallback and nothing more.
    """
    if len(png_bytes) <= _MAX_TILE_BYTES:
        return png_bytes, "image/png"
    try:
        from PIL import Image

        with Image.open(io.BytesIO(png_bytes)) as im:
            buf = io.BytesIO()
            im.convert("RGB").save(buf, format="JPEG", quality=85, optimize=True)
            logger.debug("tile re-encoded to JPEG (%d -> %d bytes)", len(png_bytes), buf.tell())
            return buf.getvalue(), "image/jpeg"
    except Exception as exc:
        logger.warning("JPEG fallback failed (%s); returning oversized PNG", exc)
        return png_bytes, "image/png"


# --- capture ----------------------------------------------------------------


async def capture(
    url: str,
    *,
    mode: str = "viewport",
    selector: str | None = None,
    identity_mode: str | None = None,
    max_tiles: int | None = None,
    wait_until: str = "domcontentloaded",
    timeout_ms: int | None = None,
    poll_budget_ms: int | None = None,
) -> dict:
    """Screenshot the rendered page as vision-ready tiles.

    Returns a dict with `blocks` (alternating text/image payloads in document
    order) and metadata. The server layer converts these into MCP content
    blocks; keeping the conversion out of here makes the module testable
    without an MCP session.
    """
    started = time.monotonic()
    ident = identity.resolve_mode(identity_mode)
    cap = max_tiles or config.MAX_TILES
    cap = max(1, min(cap, 8))
    nav_timeout = timeout_ms or config.NAV_TIMEOUT_MS
    poll_budget = poll_budget_ms or config.POLL_BUDGET_MS
    budget_s = min(config.CAPTURE_BUDGET_MS, config.TOTAL_BUDGET_MS) / 1000.0

    if mode == "element" and not selector:
        # Validate before launching a browser: an argument error should not
        # cost a Chromium start and a page load to discover.
        return {"error": "mode='element' requires a `selector`", "blocks": []}

    try:
        await assert_url_allowed(url)
    except SSRFError as exc:
        return {"error": f"refused to fetch {url}: {exc}", "blocks": []}

    def remaining() -> float:
        return budget_s - (time.monotonic() - started)

    try:
        return await asyncio.wait_for(
            _capture_inner(
                url,
                mode=mode,
                selector=selector,
                ident=ident,
                cap=cap,
                nav_timeout=nav_timeout,
                poll_budget=poll_budget,
                wait_until=wait_until,
                remaining=remaining,
            ),
            timeout=budget_s,
        )
    except browser_mod.HeadedModeUnsupported as exc:
        return {"error": str(exc), "blocks": []}
    except browser_mod.BrowserUnavailable as exc:
        # Structural, not transient: name the real cause and tell the caller
        # not to retry rather than implying a bigger budget would help.
        return {
            "error": (
                f"{exc} Browser tier unavailable on this deployment -- this is "
                "not transient; do not retry. The deployment image must be rebuilt."
            ),
            "blocks": [],
        }
    except SSRFError as exc:
        return {"error": f"refused to fetch {url}: {exc}", "blocks": []}
    except asyncio.TimeoutError:
        return {
            "error": f"capture of {url} exceeded the {int(budget_s * 1000)} ms budget",
            "blocks": [],
        }
    except Exception as exc:
        msg = str(exc)
        if "imeout" in msg:
            return {"error": f"navigation to {url} timed out after {nav_timeout} ms", "blocks": []}
        return {"error": f"failed to capture {url}: {exc}", "blocks": []}


async def _capture_inner(
    url: str,
    *,
    mode: str,
    selector: str | None,
    ident: str,
    cap: int,
    nav_timeout: int,
    poll_budget: int,
    wait_until: str,
    remaining,
) -> dict:
    blocks: list[dict] = []
    notes: list[str] = []

    async with browser_mod.tier3_page(
        ident, headless=True, viewport={"width": VIEWPORT_W, "height": VIEWPORT_H}
    ) as page:
        await page.goto(url, wait_until=wait_until, timeout=nav_timeout)
        page_height = await _settle(page, poll_budget)
        final_url = page.url

        if mode == "element":
            if not selector:
                return {"error": "mode='element' requires a `selector`", "blocks": []}
            element = await page.query_selector(selector)
            if element is None:
                return {"error": f"selector {selector!r} matched no element on {url}", "blocks": []}
            raw = await element.screenshot(type="png")
            data, mime = _encode(raw)
            blocks.append(
                {
                    "type": "text",
                    "text": f"element {selector!r} on {final_url} — ~{TOKENS_PER_TILE} tokens",
                }
            )
            blocks.append({"type": "image", "data": data, "mime": mime})
            return _result(blocks, final_url, page_height, 1, 0, notes, ident, mode)

        if mode == "viewport":
            raw = await page.screenshot(
                type="png", clip={"x": 0, "y": 0, "width": TILE_W, "height": TILE_H}
            )
            data, mime = _encode(raw)
            blocks.append(
                {
                    "type": "text",
                    "text": (
                        f"tile 1 of 1 — 0–{TILE_H} px of {page_height} px — "
                        f"~{TOKENS_PER_TILE} tokens (viewport mode; "
                        f"call again with mode='full' for the rest)"
                    ),
                }
            )
            blocks.append({"type": "image", "data": data, "mime": mime})
            return _result(blocks, final_url, page_height, 1, 0, notes, ident, mode)

        # mode == "full": tile by clip with capture-beyond-viewport, so the
        # browser composites each region without scrolling. This is what stops
        # a sticky header repeating in every tile (step 3).
        total_tiles = max(1, math.ceil(page_height / TILE_H))
        kept: list[tuple[int, int, bytes]] = []
        skipped: list[tuple[int, int]] = []
        examined = 0

        for index in range(total_tiles):
            if remaining() < 3:
                notes.append(
                    f"stopped after {examined} of {total_tiles} regions — capture budget expiring"
                )
                break
            y = index * TILE_H
            height = min(TILE_H, page_height - y)
            if height <= 0:
                break
            # full_page=True is what makes `clip` address the whole document
            # rather than the visible viewport, so the browser composites each
            # region without scrolling. Without it, any y beyond 800 px is
            # "outside the resulting image". This is also what avoids the
            # classic tiling artefact of a sticky header repeating per tile.
            raw = await page.screenshot(
                type="png",
                full_page=True,
                clip={"x": 0, "y": y, "width": TILE_W, "height": height},
                animations="disabled",
                caret="hide",
            )
            examined += 1
            entropy, ink = score_tile(raw)
            if is_blank(entropy, ink) and total_tiles > 1:
                skipped.append((y, y + height))
                continue
            kept.append((y, y + height, raw))
            if len(kept) >= cap:
                break

        if skipped:
            ranges = ", ".join(f"{a}–{b} px" for a, b in skipped)
            notes.append(f"skipped {len(skipped)} blank or near-blank region(s): {ranges}")

        omitted = total_tiles - examined
        if omitted > 0:
            notes.append(
                f"page is {page_height} px tall ({total_tiles} regions); returning the first "
                f"{len(kept)} tile(s), {omitted} region(s) not captured (MAX_TILES={cap})"
            )

        shown = len(kept)
        for i, (top, bottom, raw) in enumerate(kept, start=1):
            data, mime = _encode(raw)
            blocks.append(
                {
                    "type": "text",
                    "text": (
                        f"tile {i} of {shown} — {top}–{bottom} px of {page_height} px — "
                        f"~{TOKENS_PER_TILE} tokens (running ~{TOKENS_PER_TILE * i})"
                    ),
                }
            )
            blocks.append({"type": "image", "data": data, "mime": mime})

        return _result(blocks, final_url, page_height, shown, len(skipped), notes, ident, mode)


def _result(
    blocks: list[dict],
    final_url: str,
    page_height: int,
    tiles: int,
    skipped: int,
    notes: list[str],
    ident: str,
    mode: str,
) -> dict:
    return {
        "blocks": blocks,
        "final_url": final_url,
        "page_height_px": page_height,
        "tiles_returned": tiles,
        "tiles_skipped_blank": skipped,
        "tile_size": [TILE_W, TILE_H],
        "tokens_per_tile": TOKENS_PER_TILE,
        "estimated_tokens": TOKENS_PER_TILE * tiles,
        "identity_mode": ident,
        "mode": mode,
        "notes": notes,
    }


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()
