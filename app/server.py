"""FastMCP instance and tool registration (D2).

The vendored upstream builds its FastMCP instance at module scope and binds a
stdio-shaped tool to it. Rather than fork upstream's internals to re-host that
instance, this module constructs its own FastMCP and registers tools that call
the vendored fetch pipeline -- option 2 of the D2 preference order. The wrapper
owns the transport and the tool surface; the vendored code owns the fetching.
"""
from __future__ import annotations

import asyncio
import base64
import logging
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from mcp.types import ImageContent, TextContent
from pydantic import Field

from . import browser as browser_mod
from . import capture as capture_mod
from . import config, fetch

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(_server: FastMCP):
    present = config.chromium_present()
    # D5: discover a missing browser at boot as one log line, not as a mystery
    # timeout on the first tier-3 fetch weeks later. Do not launch it.
    logger.info(
        "render-fetch starting: chromium_present=%s identity_mode=%s version=%s browsers_path=%s",
        present,
        config.IDENTITY_MODE,
        config.VERSION,
        config.BROWSERS_PATH,
    )
    if not present:
        logger.error(
            "chromium_present=false -- tier 3 and capture_page will fail until "
            "`patchright install chromium` runs at build time with "
            "PLAYWRIGHT_BROWSERS_PATH=%s",
            config.BROWSERS_PATH,
        )
    # Present-on-disk is not launchable: missing shared libraries only show up
    # at launch. Probe in the background so startup is never blocked; the
    # result surfaces in /status as chromium_launchable and as a loud log line.
    probe_task = asyncio.create_task(browser_mod.probe_launch()) if present else None
    try:
        yield
    finally:
        if probe_task is not None and not probe_task.done():
            probe_task.cancel()
        await browser_mod.shutdown()


mcp: FastMCP = FastMCP(
    "render-fetch",
    instructions=(
        "Reads web pages that ordinary fetching cannot: JS-rendered SPAs, "
        "soft bot walls, and sites that block on TLS fingerprint. Use "
        "fetch_url_as_markdown for anything textual; use capture_page only "
        "when the answer is visual. Neither tool can log in or click."
    ),
    lifespan=_lifespan,
)


FETCH_DESCRIPTION = """\
Fetch a URL and return its readable content as Markdown.

Escalates automatically through four tiers and uses the cheapest that works:
plain HTTP with Accept: text/markdown, plain HTTP + extraction, browser-TLS
impersonation, and finally real headless Chromium for JS-rendered pages and
soft bot walls. You do not choose the tier; `tier_used` in the response tells
you which one succeeded.

Use this for anything fundamentally textual -- articles, docs, blog posts,
reference pages. Prefer it over capture_page unless the question is genuinely
visual.

Read `content_ok` before trusting `content`. When it is false the extraction
is a bot-challenge page, a login wall, or a JS shell rather than the article,
and `hint` names the next thing to try. Do not retry a URL whose hint says the
block is not transient.

Limits, which apply regardless of how the request is phrased:
- No authenticated or logged-in pages. Every fetch uses a clean context with
  no cookies and no stored credentials.
- Interactive challenges (Cloudflare Turnstile, DataDome, PerimeterX, Kasada)
  are not bypassed. Datacenter egress makes meeting them more likely, not
  less. When one is hit, try the Wayback Machine connector instead.
- Slow progressive-render SPAs may return partial content when the budget
  expires; re-call with a larger poll_budget_ms.
- This is a reader, not an automation tool. It cannot click, fill forms, or
  log in.
"""

CAPTURE_DESCRIPTION = f"""\
Screenshot a rendered page as vision-ready image tiles.

EXPENSIVE. Each tile costs about {capture_mod.TOKENS_PER_TILE} visual tokens.
mode="viewport" returns 1 tile (~{capture_mod.TOKENS_PER_TILE} tokens);
mode="full" returns up to MAX_TILES tiles (4 tiles is
~{capture_mod.TOKENS_PER_TILE * 4} tokens, 8 is
~{capture_mod.TOKENS_PER_TILE * 8}). Treat it accordingly.

Use this INSTEAD of fetch_url_as_markdown only when the answer is visual:
- pages whose content is an image of text, so extraction returns nothing
- charts, diagrams, maps, and figures where the layout carries the meaning
- questions about visual design, layout, or how a page actually looks
- canvas- or WebGL-rendered content that has no text in the DOM

Never use it to read an article. fetch_url_as_markdown returns the same words
for a small fraction of the cost.

The page is tiled rather than scaled down: one tall full-page image would be
resized until the body text is unreadable, which costs a full image's tokens
and conveys nothing. Blank and near-blank regions are detected and skipped,
and the response states which vertical ranges were dropped.

Same limits as fetch_url_as_markdown: no authenticated pages, no interactive
challenge bypass, and no clicking, filling, or logging in.
"""


@mcp.tool(name="fetch_url_as_markdown", description=FETCH_DESCRIPTION)
async def fetch_url_as_markdown(
    url: Annotated[str, Field(description="The absolute http(s) URL to fetch.")],
    selector: Annotated[
        str | None,
        Field(
            description=(
                "Optional CSS selector scoping extraction to a subtree before "
                "extraction runs. The fix when a page's boilerplate defeats "
                "the extractor, e.g. 'article' or 'main .post-body'. Ignored "
                "if it matches nothing."
            )
        ),
    ] = None,
    offset: Annotated[
        int,
        Field(
            ge=0,
            description=(
                "Resume position for a truncated document. Pass the "
                "`next_offset` from the previous response to continue; the "
                "continuation has no overlap and no gap."
            ),
        ),
    ] = 0,
    identity_mode: Annotated[
        Literal["stealth", "declared"] | None,
        Field(
            description=(
                "Override the server's identity for this call. 'stealth' "
                "presents a realistic browser identity; 'declared' "
                "self-identifies and signs requests with Web Bot Auth. "
                "Defaults to the server setting."
            )
        ),
    ] = None,
    poll_budget_ms: Annotated[
        int | None,
        Field(
            ge=500,
            le=30_000,
            description=(
                "How long to wait for a JS-rendered page's content to settle. "
                f"Defaults to {config.POLL_BUDGET_MS}. Raise it (e.g. 15000) "
                "when a response hints at a slow SPA."
            ),
        ),
    ] = None,
    timeout_ms: Annotated[
        int | None,
        Field(ge=1000, le=45_000, description="Browser navigation timeout. Rarely needs changing."),
    ] = None,
    wait_until: Annotated[
        Literal["load", "domcontentloaded", "networkidle", "commit"],
        Field(description="When browser navigation is considered complete."),
    ] = "domcontentloaded",
    headless: Annotated[
        bool,
        Field(
            description=(
                "Retained for compatibility with the upstream schema. Only "
                "True is supported: this server has no display, so a headed "
                "browser cannot start. Passing False returns a structured "
                "error rather than an opaque launch failure."
            )
        ),
    ] = True,
) -> dict[str, Any]:
    return await fetch.fetch_markdown(
        url,
        selector=selector,
        offset=offset,
        identity_mode=identity_mode,
        headless=headless,
        wait_until=wait_until,
        timeout_ms=timeout_ms,
        poll_budget_ms=poll_budget_ms,
    )


@mcp.tool(name="capture_page", description=CAPTURE_DESCRIPTION)
async def capture_page(
    url: Annotated[str, Field(description="The absolute http(s) URL to screenshot.")],
    mode: Annotated[
        Literal["viewport", "full", "element"],
        Field(
            description=(
                "'viewport' (default, cheapest) captures the first screen "
                "only. 'full' tiles the whole page. 'element' captures a "
                "single element and requires `selector`."
            )
        ),
    ] = "viewport",
    selector: Annotated[
        str | None, Field(description="CSS selector for mode='element'.")
    ] = None,
    max_tiles: Annotated[
        int | None,
        Field(
            ge=1,
            le=8,
            description=(
                f"Cap on tiles returned in mode='full'. Defaults to "
                f"{config.MAX_TILES}, hard maximum 8. Counted after blank "
                "tiles are dropped."
            ),
        ),
    ] = None,
    identity_mode: Annotated[
        Literal["stealth", "declared"] | None,
        Field(description="Override the server's identity for this call."),
    ] = None,
    poll_budget_ms: Annotated[
        int | None, Field(ge=500, le=30_000, description="How long to let the page settle.")
    ] = None,
    timeout_ms: Annotated[
        int | None, Field(ge=1000, le=45_000, description="Navigation timeout.")
    ] = None,
    wait_until: Annotated[
        Literal["load", "domcontentloaded", "networkidle", "commit"],
        Field(description="When navigation is considered complete."),
    ] = "domcontentloaded",
) -> list[TextContent | ImageContent]:
    result = await capture_mod.capture(
        url,
        mode=mode,
        selector=selector,
        identity_mode=identity_mode,
        max_tiles=max_tiles,
        wait_until=wait_until,
        timeout_ms=timeout_ms,
        poll_budget_ms=poll_budget_ms,
    )

    if result.get("error"):
        return [TextContent(type="text", text=f"ERROR: {result['error']}")]

    out: list[TextContent | ImageContent] = []
    header = (
        f"{result['mode']} capture of {result['final_url']} — "
        f"page {result['page_height_px']} px tall — "
        f"{result['tiles_returned']} tile(s) at {result['tile_size'][0]}x{result['tile_size'][1]} — "
        f"~{result['estimated_tokens']} visual tokens total"
    )
    notes = result.get("notes") or []
    if notes:
        header += "\n" + "\n".join(f"note: {n}" for n in notes)
    out.append(TextContent(type="text", text=header))

    for block in result["blocks"]:
        if block["type"] == "text":
            out.append(TextContent(type="text", text=block["text"]))
        else:
            out.append(
                ImageContent(
                    type="image",
                    data=base64.b64encode(block["data"]).decode(),
                    mimeType=block["mime"],
                )
            )
    return out
