"""Tier ladder orchestration and escalation policy (section 1, D6, D12)."""
from __future__ import annotations

import pytest

from app import browser as browser_mod
from app import envelope, fetch

ARTICLE_HTML = """
<html><head><title>T</title></head><body>
<nav>Home About Contact Subscribe</nav>
<article><h1>Genuine Heading</h1><p>{body}</p></article>
<footer>Copyright</footer>
</body></html>
""".format(
    body="This is real article prose with enough substance to extract cleanly. " * 12
)

SHELL_HTML = '<html><body><div id="root">Loading...</div></body></html>'


def test_selector_scopes_extraction():
    html = "<html><body><div class='ad'>BUY NOW</div><main><p>Real content here.</p></main></body></html>"
    scoped = fetch._scope_to_selector(html, "main")
    assert "Real content" in scoped
    assert "BUY NOW" not in scoped


def test_selector_that_matches_nothing_degrades_to_full_document():
    """A selector typo should degrade to normal extraction, not to nothing."""
    assert fetch._scope_to_selector(ARTICLE_HTML, ".does-not-exist") == ARTICLE_HTML


def test_selector_none_is_a_passthrough():
    assert fetch._scope_to_selector(ARTICLE_HTML, None) is ARTICLE_HTML


def test_extraction_drops_nav_chrome():
    """T2's expectation: article body without nav chrome."""
    md = fetch._extract(ARTICLE_HTML, "https://example.com/")
    assert "Genuine Heading" in md
    assert "Subscribe" not in md


def test_js_shell_detection():
    assert fetch._looks_like_js_shell(None, "x" * 9000) is True
    assert fetch._looks_like_js_shell("", "x" * 9000) is True
    # Short extract from a big document: a shell.
    assert fetch._looks_like_js_shell("Loading", "x" * 9000) is True
    # Substantial extract: not a shell.
    assert fetch._looks_like_js_shell("y" * 500, "x" * 9000) is False


@pytest.mark.asyncio
async def test_ssrf_refusal_short_circuits_before_any_tier():
    """T7 through the tool surface, not just the guard."""
    result = await fetch.fetch_markdown("http://169.254.169.254/latest/meta-data/")
    assert result["content_ok"] is False
    assert "refused" in result["error"]
    assert result["content"] == ""
    assert result["tier_used"] is None


@pytest.mark.asyncio
async def test_file_scheme_refused():
    result = await fetch.fetch_markdown("file:///etc/passwd")
    assert result["content_ok"] is False
    assert "refused" in result["error"]


@pytest.mark.asyncio
async def test_headed_mode_is_rejected_with_a_self_documenting_error(monkeypatch):
    """D6: keep the parameter, reject it with an explanation.

    Forced past the cheap tiers so the request actually reaches the browser.
    """

    async def no_cheap_content(*args, **kwargs):
        return None

    monkeypatch.setattr(fetch, "_tier_1_2", no_cheap_content)
    monkeypatch.setattr(fetch, "_tier_25", no_cheap_content)

    result = await fetch.fetch_markdown("https://8.8.8.8/", headless=False)
    assert result["content_ok"] is False
    assert "display" in result["error"]
    assert "headless=False" in result["error"]


@pytest.mark.asyncio
async def test_missing_browser_is_reported_clearly_not_as_a_timeout(monkeypatch):
    """D5/D4: a missing binary must never look like a mystery hang."""

    async def no_cheap_content(*args, **kwargs):
        return None

    async def unavailable(*args, **kwargs):
        raise browser_mod.BrowserUnavailable("Chromium is not installed in this image.")

    monkeypatch.setattr(fetch, "_tier_1_2", no_cheap_content)
    monkeypatch.setattr(fetch, "_tier_25", no_cheap_content)
    monkeypatch.setattr(fetch, "_tier_3", unavailable)

    result = await fetch.fetch_markdown("https://8.8.8.8/")
    assert result["content_ok"] is False
    assert "not installed" in result["error"]


@pytest.mark.asyncio
async def test_launch_failure_hint_is_honest_and_non_transient(monkeypatch):
    """A launch failure must not advise a larger poll_budget_ms: the cause is
    structural (missing shared libraries), so the hint must say do-not-retry."""

    async def no_cheap_content(*args, **kwargs):
        return None

    async def launch_fails(*args, **kwargs):
        raise browser_mod.BrowserUnavailable(
            "Chromium failed to launch: error while loading shared libraries: libnspr4.so"
        )

    monkeypatch.setattr(fetch, "_tier_1_2", no_cheap_content)
    monkeypatch.setattr(fetch, "_tier_25", no_cheap_content)
    monkeypatch.setattr(fetch, "_tier_3", launch_fails)

    result = await fetch.fetch_markdown("https://8.8.8.8/")
    assert result["content_ok"] is False
    assert "libnspr4.so" in result["error"]
    assert result["hint"] == envelope.BROWSER_UNAVAILABLE_HINT
    assert "not transient" in result["hint"]
    # The misleading SPA advice must not appear.
    assert result["hint"] != envelope.SPA_HINT


@pytest.mark.asyncio
async def test_render_timeout_still_gets_the_spa_hint(monkeypatch):
    """The non-transient treatment is only for launch failures; a slow SPA
    should still be told to raise poll_budget_ms."""

    async def no_cheap_content(*args, **kwargs):
        return None

    async def times_out(*args, **kwargs):
        raise RuntimeError("Timeout 30000ms exceeded")

    monkeypatch.setattr(fetch, "_tier_1_2", no_cheap_content)
    monkeypatch.setattr(fetch, "_tier_25", no_cheap_content)
    monkeypatch.setattr(fetch, "_tier_3", times_out)

    result = await fetch.fetch_markdown("https://8.8.8.8/")
    assert result["content_ok"] is False
    assert result["hint"] == envelope.SPA_HINT


@pytest.mark.asyncio
async def test_unusable_cheap_content_escalates(monkeypatch):
    """The ladder stops at the cheapest tier producing *usable* content.

    A stable "Loading..." shell is non-empty, so a naive check would stop at
    tier 2 and return it -- exactly the page tier 3 exists for.
    """
    calls = []

    async def shell_tier_1_2(url, mode, selector, timeout):
        calls.append("1_2")
        return 2, "Loading...", url, 200

    async def no_25(url, mode, selector, timeout):
        calls.append("2_5")
        return None

    async def good_tier_3(url, mode, selector, **kwargs):
        calls.append("3")
        return 3, "# Real Heading\n\n" + ("Genuine rendered prose. " * 30), url, 200

    monkeypatch.setattr(fetch, "_tier_1_2", shell_tier_1_2)
    monkeypatch.setattr(fetch, "_tier_25", no_25)
    monkeypatch.setattr(fetch, "_tier_3", good_tier_3)

    result = await fetch.fetch_markdown("https://8.8.8.8/")
    assert calls == ["1_2", "2_5", "3"]
    assert result["tier_used"] == 3
    assert result["content_ok"] is True
    assert "Real Heading" in result["content"]


@pytest.mark.asyncio
async def test_usable_cheap_content_stops_the_ladder(monkeypatch):
    """The corollary: do not pay for a browser when tier 2 already worked."""
    reached_3 = False

    async def good_tier_1_2(url, mode, selector, timeout):
        return 2, "# Heading\n\n" + ("Real prose. " * 40), url, 200

    async def tier_3(*args, **kwargs):
        nonlocal reached_3
        reached_3 = True
        return None

    monkeypatch.setattr(fetch, "_tier_1_2", good_tier_1_2)
    monkeypatch.setattr(fetch, "_tier_3", tier_3)

    result = await fetch.fetch_markdown("https://8.8.8.8/")
    assert result["tier_used"] == 2
    assert result["content_ok"] is True
    assert reached_3 is False


@pytest.mark.asyncio
async def test_cheap_result_is_kept_when_the_browser_tier_fails(monkeypatch):
    """A thin answer beats an error the caller cannot act on."""

    async def thin_tier_1_2(url, mode, selector, timeout):
        return 2, "Loading...", url, 200

    async def no_25(*args, **kwargs):
        return None

    async def broken_tier_3(*args, **kwargs):
        raise RuntimeError("chromium exploded")

    monkeypatch.setattr(fetch, "_tier_1_2", thin_tier_1_2)
    monkeypatch.setattr(fetch, "_tier_25", no_25)
    monkeypatch.setattr(fetch, "_tier_3", broken_tier_3)

    result = await fetch.fetch_markdown("https://8.8.8.8/")
    assert result["tier_used"] == 2
    assert result["content_ok"] is False
    assert result["hint"]


@pytest.mark.asyncio
async def test_offset_continues_a_truncated_document(monkeypatch):
    """T10 end to end through the tool surface."""
    doc = "# Doc\n\n" + ("word " * 30_000)

    async def big(url, mode, selector, timeout):
        return 2, doc, url, 200

    monkeypatch.setattr(fetch, "_tier_1_2", big)

    first = await fetch.fetch_markdown("https://8.8.8.8/", max_chars=40_000)
    assert first["truncated"] is True
    assert first["next_offset"] == 40_000

    second = await fetch.fetch_markdown(
        "https://8.8.8.8/", offset=first["next_offset"], max_chars=40_000
    )
    tidied = envelope.tidy(doc)
    assert first["content"] + second["content"] == tidied[: len(first["content"]) + len(second["content"])]


@pytest.mark.asyncio
async def test_bot_wall_returns_the_wayback_hint(monkeypatch):
    """T9 end to end: not a hang, not a wall of challenge HTML."""

    async def wall(url, mode, selector, timeout):
        return 2, "Just a moment...\nChecking your browser. Ray ID: abc123", url, 403

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(fetch, "_tier_1_2", wall)
    monkeypatch.setattr(fetch, "_tier_25", nothing)
    monkeypatch.setattr(fetch, "_tier_3", nothing)

    result = await fetch.fetch_markdown("https://8.8.8.8/")
    assert result["content_ok"] is False
    assert "Wayback" in result["hint"]
