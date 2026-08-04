"""Envelope shaping, content_ok heuristics, truncation (D10, D11, T9, T10)."""
from __future__ import annotations

from app import envelope

ARTICLE = (
    "# A Real Article\n\n"
    + "This is a substantial paragraph of ordinary prose that any extractor "
    "would be happy with, repeated enough times to look like a genuine page. " * 8
)


def test_real_article_is_ok():
    ok, hint = envelope.classify(ARTICLE)
    assert ok is True
    assert hint is None


def test_bot_wall_names_wayback():
    """T9: the honest-failure requirement."""
    ok, hint = envelope.classify("Just a moment...\nChecking your browser before accessing.\nRay ID: 8a1b")
    assert ok is False
    assert "Wayback" in hint
    assert "Do not retry" in hint


def test_login_wall_says_stop():
    ok, hint = envelope.classify("Please sign in\nSubscribe to read the full story.")
    assert ok is False
    assert "login" in hint.lower() or "subscription" in hint.lower()
    assert "cannot log in" in hint


def test_js_shell_suggests_bigger_poll_budget():
    ok, hint = envelope.classify("You need to enable JavaScript to run this app.")
    assert ok is False
    assert "poll_budget_ms" in hint


def test_empty_content_is_not_ok():
    ok, hint = envelope.classify("")
    assert ok is False
    assert hint

    ok, hint = envelope.classify(None)
    assert ok is False


def test_implausibly_short_is_not_ok():
    ok, hint = envelope.classify("Loading...")
    assert ok is False
    assert hint


def test_long_article_merely_mentioning_a_wall_phrase_stays_ok():
    """A page *about* bot walls must not be misread as one."""
    text = ARTICLE + "\n\nSome sites show 'checking your browser' when you visit."
    ok, _ = envelope.classify(text)
    assert ok is True


def test_truncation_and_continuation_have_no_overlap_or_gap():
    """T10: continuation returns the tail, no overlap or gap."""
    doc = "".join(f"{i:06d}." for i in range(20_000))  # 140k chars
    first, truncated, next_offset = envelope.apply_offset_and_truncate(doc, 0, 40_000)

    assert truncated is True
    assert len(first) == 40_000
    assert next_offset == 40_000

    second, truncated2, next2 = envelope.apply_offset_and_truncate(doc, next_offset, 40_000)
    assert second == doc[40_000:80_000]
    assert first + second == doc[:80_000]  # no overlap, no gap
    assert truncated2 is True
    assert next2 == 80_000


def test_final_window_is_not_truncated():
    doc = "x" * 100
    window, truncated, next_offset = envelope.apply_offset_and_truncate(doc, 0, 40_000)
    assert window == doc
    assert truncated is False
    assert next_offset is None


def test_offset_past_end_returns_empty():
    window, truncated, next_offset = envelope.apply_offset_and_truncate("short", 999, 40_000)
    assert window == ""
    assert truncated is False
    assert next_offset is None


def test_envelope_reports_integral_tiers_as_ints_and_keeps_2_5():
    env = envelope.FetchEnvelope(content="x", tier_used=2.0)
    assert env.to_dict()["tier_used"] == 2

    env = envelope.FetchEnvelope(content="x", tier_used=2.5)
    assert env.to_dict()["tier_used"] == 2.5


def test_envelope_has_the_d10_fields():
    d = envelope.FetchEnvelope(content="x", tier_used=1).to_dict()
    for field in (
        "content",
        "tier_used",
        "identity_mode",
        "final_url",
        "http_status",
        "content_ok",
        "truncated",
        "next_offset",
        "hint",
    ):
        assert field in d


def test_error_envelope_is_not_ok():
    d = envelope.error_envelope("refused", url="http://x/")
    assert d["content_ok"] is False
    assert d["error"] == "refused"
    assert d["content"] == ""
