#!/usr/bin/env python3
"""Build-time assertion that Chromium actually landed on disk (D4).

Run as the last step of the build command. Prints the resolved absolute path
and exits non-zero if the binary is not there, so a broken image fails at build
time -- loudly, once -- rather than as a mystery timeout on the first tier-3
fetch weeks later.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402


def stamp_version() -> None:
    """Record the build identity while the checkout is still intact.

    The deployed image has no `.git`, so this is the only moment a real SHA is
    obtainable. Without it /status reports "unknown" in production and there
    is no way to tell which build is serving.
    """
    import subprocess

    version = ""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=config.REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0:
            version = out.stdout.strip()
    except Exception:
        pass

    if not version:
        for var in ("GIT_SHA", "REPL_DEPLOYMENT_ID", "REPLIT_DEPLOYMENT_ID", "REPL_SLUG"):
            if os.environ.get(var, "").strip():
                version = os.environ[var].strip()
                break

    if not version:
        print("note: no git SHA or Replit identifier available; leaving VERSION unstamped")
        return

    config.VERSION_FILE.write_text(version + "\n")
    print(f"stamped VERSION                     : {version}")


def main() -> int:
    stamp_version()
    print(f"PLAYWRIGHT_BROWSERS_PATH (env)      : {os.environ.get('PLAYWRIGHT_BROWSERS_PATH')!r}")
    print(f"PLAYWRIGHT_BROWSERS_PATH (resolved) : {config.BROWSERS_PATH}")
    print(f"exists                              : {config.BROWSERS_PATH.is_dir()}")

    if config.BROWSERS_PATH.is_dir():
        for child in sorted(config.BROWSERS_PATH.iterdir()):
            print(f"  - {child.name}")

    path = config.chromium_path()
    if path is None:
        print(
            "\nFAIL: no Chromium executable found.\n"
            "The build must run `patchright install chromium` with "
            "PLAYWRIGHT_BROWSERS_PATH pointing at a project-relative directory, "
            "and that directory must be part of the deployment snapshot.",
            file=sys.stderr,
        )
        return 1

    size_mb = path.stat().st_size / (1024 * 1024)
    print(f"\nchromium executable : {path}")
    print(f"size                : {size_mb:.1f} MiB")
    if not os.access(path, os.X_OK):
        print("FAIL: chromium is present but not executable.", file=sys.stderr)
        return 1
    print("OK: chromium is installed and executable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
