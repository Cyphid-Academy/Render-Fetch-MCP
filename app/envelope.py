"""Response shaping, content_ok heuristics, and hints (D10, D11).

Upstream returns a bare string, or a string starting with "ERROR:". A calling
model cannot act on that -- it cannot tell "this page is empty" from "a bot
wall served me a challenge page", and so it retries the wall. The envelope
exists to make that distinction machine-readable.

content_ok is the load-bearing field. When it is false, `hint` names the next
thing to try, in the priority order set out in D10.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from . import config

# --- content_ok heuristics --------------------------------------------------

# Phrases that appear in interstitials rather than in articles. Matched against
# a lowercased extract; each is specific enough that a page merely *discussing*
# bot protection is unlikely to trip several at once.
_BOT_WALL_MARKERS = (
    "checking your browser",
    "verify you are human",
    "verifying you are human",
    "please verify you are a human",
    "enable javascript and cookies to continue",
    "ray id",
    "cf-browser-verification",
    "ddos protection by",
    "just a moment",
    "attention required! | cloudflare",
    "access denied",
    "you have been blocked",
    "why have i been blocked",
    "pardon our interruption",
    "are you a robot",
    "unusual traffic from your computer",
    "request unsuccessful. incapsula",
    "perimeterx",
    "datadome",
    "px-captcha",
)

_LOGIN_WALL_MARKERS = (
    "sign in to continue",
    "log in to continue",
    "please log in",
    "please sign in",
    "create an account to continue",
    "subscribe to read",
    "subscribers only",
    "this content is for members",
    "you must be logged in",
    "members only",
)

_JS_SHELL_MARKERS = (
    "enable javascript to run this app",
    "you need to enable javascript",
    "this site requires javascript",
    "please enable javascript",
    "<noscript>",
    "loading...",
)

# Below this, an extract is too short to be a page's main content unless the
# page really is a stub. Paired with the marker checks rather than used alone.
_IMPLAUSIBLY_SHORT = 200
_SHORT_BUT_MAYBE_FINE = 600

WAYBACK_HINT = (
    "This looks like a bot wall or paywall rather than the page's content. "
    "Do not retry this URL -- the block is not transient. Try the Wayback "
    "Machine MCP connector instead; archives frequently hold what a live wall "
    "will not serve."
)
LOGIN_HINT = (
    "This looks like a login or subscription wall. This server fetches with a "
    "clean, unauthenticated context and cannot log in, so retrying will not "
    "help. Stop here, or try the Wayback Machine connector for an archived copy."
)
SPA_HINT = (
    "This looks like a JavaScript shell whose content had not finished "
    "rendering when the budget expired. Re-call with a larger poll_budget_ms "
    "(for example 15000)."
)
SHORT_HINT = (
    "Extraction produced implausibly little text for this page. Try a CSS "
    "`selector` scoping extraction to the article subtree, or capture_page if "
    "the content is an image of text."
)


def _hits(haystack: str, needles: tuple[str, ...]) -> int:
    return sum(1 for n in needles if n in haystack)


def classify(content: str | None, *, tier_used: float | None = None) -> tuple[bool, str | None]:
    """Return (content_ok, hint).

    Ordering matters and follows D10: bot wall first (because naming Wayback is
    the most useful redirection), then login wall, then slow SPA, then merely
    short.
    """
    if not content or not content.strip():
        return False, SPA_HINT if tier_used == 3 else WAYBACK_HINT

    text = content.lower()
    length = len(content.strip())

    bot_hits = _hits(text, _BOT_WALL_MARKERS)
    login_hits = _hits(text, _LOGIN_WALL_MARKERS)
    js_hits = _hits(text, _JS_SHELL_MARKERS)

    # A real article can quote one of these phrases. An interstitial is short
    # AND matches; a long page needs corroboration before we call it a wall.
    if bot_hits and (length < _SHORT_BUT_MAYBE_FINE or bot_hits >= 2):
        return False, WAYBACK_HINT
    if login_hits and (length < _SHORT_BUT_MAYBE_FINE or login_hits >= 2):
        return False, LOGIN_HINT
    if js_hits and length < _SHORT_BUT_MAYBE_FINE:
        return False, SPA_HINT
    if length < _IMPLAUSIBLY_SHORT:
        return False, SPA_HINT if tier_used == 3 else SHORT_HINT

    return True, None


# --- truncation -------------------------------------------------------------


def apply_offset_and_truncate(
    content: str, offset: int = 0, max_chars: int | None = None
) -> tuple[str, bool, int | None]:
    """Slice `content` from `offset`, capped at `max_chars` (D11).

    Returns (slice, truncated, next_offset). next_offset is an absolute index
    into the full document, so a continuation call is `offset=next_offset` with
    no arithmetic on the caller's side and, importantly, no overlap or gap.
    """
    cap = config.MAX_CONTENT_CHARS if max_chars is None else max_chars
    offset = max(0, int(offset))
    if offset >= len(content):
        return "", False, None
    window = content[offset : offset + cap]
    end = offset + len(window)
    truncated = end < len(content)
    return window, truncated, (end if truncated else None)


# --- envelope ---------------------------------------------------------------


@dataclass
class FetchEnvelope:
    """The D10 response shape."""

    content: str = ""
    tier_used: float | None = None
    identity_mode: str = "stealth"
    final_url: str | None = None
    http_status: int | None = None
    content_ok: bool = True
    truncated: bool = False
    next_offset: int | None = None
    hint: str | None = None
    error: str | None = None
    timings_ms: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # tier_used is 2.5 at one tier and integral elsewhere; present integral
        # tiers as ints so the JSON reads as the spec's table does.
        t = d.get("tier_used")
        if isinstance(t, float) and t.is_integer():
            d["tier_used"] = int(t)
        if not d.get("error"):
            d.pop("error", None)
        return d


def error_envelope(reason: str, *, hint: str | None = None, url: str | None = None) -> dict[str, Any]:
    env = FetchEnvelope(
        content="",
        identity_mode=config.IDENTITY_MODE,
        final_url=url,
        content_ok=False,
        hint=hint,
        error=reason,
    )
    return env.to_dict()


_WS = re.compile(r"[ \t]+\n")


def tidy(markdown: str) -> str:
    """Light normalisation. Extraction leaves ragged trailing whitespace that
    costs tokens and helps nobody."""
    return _WS.sub("\n", markdown).strip()
