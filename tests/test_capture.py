"""Tile geometry, entropy scoring, and encoding (section 11, T11)."""
from __future__ import annotations

import io

import pytest

from app import capture


def test_tile_geometry_matches_the_standard_tier_budget():
    """Step 4: computed from published limits, not hard-coded."""
    w, h, tokens = capture.tile_geometry(1280, 800, token_budget=1568, pixel_limit=1568)
    assert (w, h) == (1280, 800)
    # ceil(1280/28) * ceil(800/28) == 46 * 29
    assert tokens == 46 * 29 == 1334
    assert tokens <= 1568, "a tile must fit the budget or it gets resized and text blurs"


def test_tile_never_exceeds_the_token_budget_across_widths():
    for width in (320, 768, 1024, 1280, 1440, 1568):
        _, _, tokens = capture.tile_geometry(width, 800)
        assert tokens <= capture.TOKEN_BUDGET


def test_tile_edges_never_exceed_the_pixel_limit():
    w, h, _ = capture.tile_geometry(4000, 4000)
    assert w <= capture.PIXEL_LIMIT
    assert h <= capture.PIXEL_LIMIT


def test_high_resolution_tier_allows_a_taller_tile():
    """Step 4's reason for computing rather than hard-coding."""
    _, standard_h, _ = capture.tile_geometry(1280, 4000, token_budget=1568, pixel_limit=1568)
    _, high_h, _ = capture.tile_geometry(1280, 4000, token_budget=4784, pixel_limit=2576)
    assert high_h > standard_h


def test_documented_tile_costs():
    assert capture.TOKENS_PER_TILE * 4 == 5336


@pytest.mark.asyncio
async def test_capture_launch_failure_is_named_and_non_transient(monkeypatch):
    """A Chromium launch failure must name the real cause and mark itself
    non-transient, not present as a generic capture error."""
    from app import browser as browser_mod

    async def launch_fails(*args, **kwargs):
        raise browser_mod.BrowserUnavailable(
            "Chromium failed to launch: error while loading shared libraries: libnspr4.so"
        )

    monkeypatch.setattr(capture, "_capture_inner", launch_fails)

    result = await capture.capture("https://8.8.8.8/")
    assert result["blocks"] == []
    assert "libnspr4.so" in result["error"]
    assert "not transient" in result["error"]
    assert "do not retry" in result["error"]
    assert capture.TOKENS_PER_TILE * 8 == 10672


def _png(color, size=(1280, 800), draw_text=False):
    from PIL import Image, ImageDraw

    im = Image.new("RGB", size, color)
    if draw_text:
        d = ImageDraw.Draw(im)
        for row in range(0, size[1], 20):
            d.text((10, row), "The quick brown fox jumps over the lazy dog " * 3, fill=(0, 0, 0))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def test_blank_tile_is_detected():
    entropy, ink = capture.score_tile(_png((255, 255, 255)))
    assert capture.is_blank(entropy, ink) is True


def test_text_tile_is_not_detected_as_blank():
    entropy, ink = capture.score_tile(_png((255, 255, 255), draw_text=True))
    assert capture.is_blank(entropy, ink) is False


def test_a_low_entropy_text_tile_survives_on_ink_ratio():
    """Why is_blank requires BOTH signals.

    A page of black text on white has a strongly bimodal histogram and so a
    low entropy score; entropy alone would drop a perfectly readable tile.
    """
    png = _png((255, 255, 255), draw_text=True)
    entropy, ink = capture.score_tile(png)
    assert ink > 0.004
    assert capture.is_blank(entropy, ink) is False


def test_encode_keeps_png_when_small():
    data, mime = capture._encode(_png((255, 255, 255)))
    assert mime == "image/png"
    assert data[:8] == b"\x89PNG\r\n\x1a\n"


def test_encode_falls_back_to_jpeg_when_oversized(monkeypatch):
    monkeypatch.setattr(capture, "_MAX_TILE_BYTES", 10)
    data, mime = capture._encode(_png((255, 255, 255), draw_text=True))
    assert mime == "image/jpeg"
    assert data[:2] == b"\xff\xd8"


@pytest.mark.asyncio
async def test_element_mode_requires_a_selector():
    result = await capture.capture("https://8.8.8.8/", mode="element", selector=None)
    assert result["error"]
    assert "selector" in result["error"]


@pytest.mark.asyncio
async def test_capture_refuses_blocked_urls():
    result = await capture.capture("http://169.254.169.254/", mode="viewport")
    assert result["error"]
    assert "refused" in result["error"]
    assert result["blocks"] == []
