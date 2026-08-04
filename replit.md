# Render-Fetch-MCP

MCP server (Python/Starlette/FastMCP) that returns readable page content as
Markdown, escalating through 4 fetch tiers up to headless Chromium
(patchright). Connects to claude.ai as a custom connector. See README.md,
DECISIONS.md, and AGENTS.md — they are authoritative on design decisions and
the upstream update policy.

## How to run
- Workflow "Start application" runs `python main.py` on port 5000.
- Replit Secret `MCP_PATH_TOKEN` holds the secret path segment; MCP
  endpoint is `POST /mcp/<token>`. Health check: `GET /status` (also `/health`, `/healthz`; use
  `/status` in production — Replit's frontend intercepts `/healthz`).
- Chromium lives in `.browsers/` (`PLAYWRIGHT_BROWSERS_PATH` set in `.replit`).
  Reinstall with `patchright install chromium` if missing — install at build
  time, never at request time.
- Tests: `python -m pytest tests/` (91 tests).

## Deployment
Autoscale; build must run
`pip install -r requirements.txt && patchright install chromium && python scripts/verify_build.py`
(already configured in `.replit`).

## Conventions
- Dependencies are pinned in requirements.txt with explanatory comments —
  keep the file hand-curated; don't append duplicates.
- Read AGENTS.md "Working notes" before editing fetch/extraction code
  (content_ok contract, escalation gating, Accept-Encoding, tile rules).

## User preferences
(none recorded yet)
