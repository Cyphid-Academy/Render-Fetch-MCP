"""Configuration, read once from the environment at import time.

Every value here has a default except MCP_PATH_TOKEN, which is required and
deliberately has none -- a server that invents its own auth token would be a
public fetch proxy the operator never knowingly opened.

See BUILD-SPEC section 5 for the table this mirrors.
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("ignoring non-integer %s=%r, using %d", name, raw, default)
        return default


# --- auth -------------------------------------------------------------------
MCP_PATH_TOKEN = os.environ.get("MCP_PATH_TOKEN", "").strip()

# --- identity ---------------------------------------------------------------
IDENTITY_MODE = os.environ.get("IDENTITY_MODE", "stealth").strip().lower()
if IDENTITY_MODE not in ("stealth", "declared"):
    logger.warning("unknown IDENTITY_MODE=%r, falling back to 'stealth'", IDENTITY_MODE)
    IDENTITY_MODE = "stealth"

SIGNING_KEY_PEM = os.environ.get("SIGNING_KEY_PEM", "")

# Public origin, used for the Signature-Agent header and for the own-origin
# SSRF check (D8). Empty is tolerated: signing falls back to a relative
# directory reference and the own-origin check relies on the loopback rules.
PUBLIC_ORIGIN = os.environ.get("PUBLIC_ORIGIN", "").strip().rstrip("/")

CONTACT_URL = os.environ.get(
    "CONTACT_URL", "https://github.com/Cyphid-Academy/render-fetch-mcp"
).strip()

# --- budgets ----------------------------------------------------------------
MAX_CONTENT_CHARS = _int_env("MAX_CONTENT_CHARS", 40_000)
POLL_BUDGET_MS = _int_env("POLL_BUDGET_MS", 8_000)

# D9. Replit does not publish an Autoscale request-duration limit; 55s sits
# under the 60s figure the platform is commonly observed to enforce. Tunable
# by Secret so a lower real limit is a config change, not a redeploy.
TOTAL_BUDGET_MS = _int_env("TOTAL_BUDGET_MS", 55_000)

# D9 stage budgets.
CHEAP_TIER_BUDGET_MS = _int_env("CHEAP_TIER_BUDGET_MS", 12_000)
NAV_TIMEOUT_MS = _int_env("NAV_TIMEOUT_MS", 25_000)
CAPTURE_BUDGET_MS = _int_env("CAPTURE_BUDGET_MS", 40_000)

BROWSER_IDLE_TIMEOUT_S = _int_env("BROWSER_IDLE_TIMEOUT_S", 120)

# --- capture ----------------------------------------------------------------
MAX_TILES = max(1, min(_int_env("MAX_TILES", 4), 8))  # hard max 8 per section 11.6

# --- runtime ----------------------------------------------------------------
PORT = _int_env("PORT", 5000)
HOST = os.environ.get("HOST", "0.0.0.0")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").strip().upper()

# D4: project-relative browser path so the binary is inside the snapshotted
# tree rather than a home-directory cache that may not survive the build.
BROWSERS_PATH = Path(
    os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or (REPO_ROOT / ".browsers")
).resolve()


VERSION_FILE = REPO_ROOT / "VERSION"


def _git_sha() -> str:
    """Best available build identity for /status and D5.

    Order matters. The deployed Autoscale image has no `.git` directory, so
    asking git at runtime returns "unknown" there -- which is exactly where
    the answer is most needed, since that is the only way to tell which build
    is actually serving. The build step therefore stamps a VERSION file while
    the checkout is still intact, and that file is consulted first.
    """
    for var in ("GIT_SHA", "REPL_DEPLOYMENT_ID", "REPLIT_DEPLOYMENT_ID", "REPL_SLUG"):
        value = os.environ.get(var, "").strip()
        if value:
            return value

    try:
        stamped = VERSION_FILE.read_text().strip()
        if stamped:
            return stamped
    except OSError:
        pass

    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return "unknown"


VERSION = _git_sha()


def chromium_path() -> Path | None:
    """Return the Chromium executable patchright would launch, if it exists.

    D5 wants a boot-time answer to "is the browser there" without paying the
    launch cost, so this is a filesystem walk rather than a driver call.
    """
    explicit = os.environ.get("CHROMIUM_EXECUTABLE_PATH", "").strip()
    if explicit:
        p = Path(explicit)
        return p if p.exists() else None

    if not BROWSERS_PATH.is_dir():
        return None
    # Layout: <browsers>/chromium-<rev>/chrome-linux/chrome
    for pattern in ("chromium-*/chrome-linux/chrome", "chromium-*/chrome-*/chrome"):
        for candidate in sorted(BROWSERS_PATH.glob(pattern), reverse=True):
            if candidate.exists():
                return candidate
    return None


def chromium_present() -> bool:
    return chromium_path() is not None
