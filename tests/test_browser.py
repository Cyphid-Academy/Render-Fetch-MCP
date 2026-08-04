"""Browser lifecycle: install/launch split, reaper, serialisation (D4, D6, D7, D14)."""
from __future__ import annotations

import asyncio

import pytest

from app import browser as browser_mod
from app import config


@pytest.mark.asyncio
async def test_headed_mode_raises_before_touching_the_browser():
    """D6: reject headed mode with an explanation, not a launch error."""
    with pytest.raises(browser_mod.HeadedModeUnsupported) as exc:
        async with browser_mod.tier3_page("stealth", headless=False):
            pass
    message = str(exc.value)
    assert "display" in message
    assert "headless=False" in message


@pytest.mark.asyncio
async def test_missing_binary_raises_a_named_error_not_a_download(monkeypatch):
    """D4: never install at runtime; say so instead."""
    monkeypatch.setattr(config, "chromium_path", lambda: None)
    with pytest.raises(browser_mod.BrowserUnavailable) as exc:
        browser_mod._resolve_executable()
    message = str(exc.value)
    assert "build time" in message
    assert "never downloaded at request time" in message


def test_launch_args_survive_a_container(monkeypatch):
    """/dev/shm is small in containers; without this Chromium crashes on heavy pages."""
    assert "--disable-dev-shm-usage" in browser_mod._LAUNCH_ARGS
    assert "--no-sandbox" in browser_mod._LAUNCH_ARGS


def test_browsers_path_is_project_relative():
    """D4: the binary must land inside the snapshotted tree."""
    assert config.BROWSERS_PATH.is_absolute()


def test_chromium_present_does_not_launch():
    """D5: boot-time presence check is a filesystem walk, not a driver call."""
    before = browser_mod.is_running()
    config.chromium_present()
    assert browser_mod.is_running() == before


@pytest.mark.asyncio
async def test_tier3_is_serialised(monkeypatch):
    """D14: one page at a time, even under concurrent callers."""
    concurrent = 0
    peak = 0

    class FakeContext:
        async def new_page(self):
            return object()

        async def close(self):
            return None

    class FakeBrowser:
        def is_connected(self):
            return True

        async def new_context(self, **kwargs):
            return FakeContext()

    async def fake_ensure():
        return FakeBrowser()

    async def fake_route(*args, **kwargs):
        return None

    monkeypatch.setattr(browser_mod, "_ensure_browser", fake_ensure)

    async def use():
        nonlocal concurrent, peak
        async with browser_mod._tier3_lock:
            concurrent += 1
            peak = max(peak, concurrent)
            await asyncio.sleep(0.02)
            concurrent -= 1

    await asyncio.gather(*(use() for _ in range(5)))
    assert peak == 1


@pytest.mark.asyncio
async def test_reaper_does_not_fire_mid_fetch():
    """The reaper must never close a browser a live tier-3 fetch is using."""
    async with browser_mod._tier3_lock:
        assert browser_mod._tier3_lock.locked() is True
    assert browser_mod._tier3_lock.locked() is False


@pytest.mark.asyncio
async def test_shutdown_is_safe_when_nothing_is_running():
    await browser_mod.shutdown()
    assert browser_mod.is_running() is False


def test_idle_timeout_default_matches_the_spec():
    """D7: 120 seconds."""
    assert config.BROWSER_IDLE_TIMEOUT_S == 120
