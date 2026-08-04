"""Entrypoint: path-token auth + streamable-HTTP app (D2, D3, D5).

Run with `python main.py`. Serves:

  POST/GET /mcp/<token>                                the MCP endpoint
  GET      /healthz                                    liveness + browser presence
  GET      /.well-known/http-message-signatures-directory   Web Bot Auth key

Auth is a secret path segment (D3) compared in constant time. A mismatch
returns 404, not 403 -- a 403 confirms the path space exists, which is exactly
what an unauthenticated scanner is trying to learn.
"""
from __future__ import annotations

import hmac
import logging
import os
import sys

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from app import browser as browser_mod
from app import config, identity
from app.server import mcp

logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("render-fetch")

# 404 for every unauthenticated shape, so the body never distinguishes "wrong
# token" from "no such route".
_NOT_FOUND = Response("Not Found", status_code=404, media_type="text/plain")


def _token_matches(candidate: str) -> bool:
    expected = config.MCP_PATH_TOKEN
    if not expected:
        return False
    return hmac.compare_digest(candidate.encode(), expected.encode())


class PathTokenGate:
    """ASGI middleware gating /mcp/<token>/... and rewriting it to /mcp/...

    Implemented at the ASGI layer rather than as a Starlette route so the
    token never reaches the MCP session manager's routing, and so a mismatch
    is refused before any MCP machinery is touched.
    """

    def __init__(self, app, mcp_app) -> None:
        self.app = app
        self.mcp_app = mcp_app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        if not path.startswith("/mcp"):
            await self.app(scope, receive, send)
            return

        rest = path[len("/mcp") :]
        if not rest.startswith("/"):
            await _NOT_FOUND(scope, receive, send)
            return

        segment, _, tail = rest[1:].partition("/")
        if not _token_matches(segment):
            logger.warning("rejected MCP request with bad path token from %s", _client(scope))
            await _NOT_FOUND(scope, receive, send)
            return

        # Re-point the request at the mounted MCP app's own root.
        scope = dict(scope)
        scope["path"] = "/mcp" + (f"/{tail}" if tail else "")
        scope["raw_path"] = scope["path"].encode()
        await self.mcp_app(scope, receive, send)


def _client(scope) -> str:
    client = scope.get("client")
    return client[0] if client else "?"


async def healthz(_request):
    """D5. Reports browser presence without launching it."""
    return JSONResponse(
        {
            "ok": True,
            "chromium_present": config.chromium_present(),
            "identity_mode": config.IDENTITY_MODE,
            "version": config.VERSION,
            "browser_running": browser_mod.is_running(),
            # None until the startup probe finishes; then whether a real
            # launch succeeded. Presence on disk alone does not prove this.
            "chromium_launchable": browser_mod.probe_result()[0],
        }
    )


async def signature_directory(_request):
    """Web Bot Auth key directory (section 3, T13)."""
    return JSONResponse(
        identity.directory_document(),
        media_type="application/http-message-signatures-directory+json",
        headers={"Cache-Control": "max-age=86400"},
    )


async def root(_request):
    return JSONResponse(
        {
            "service": "render-fetch",
            "version": config.VERSION,
            "health": "/status",
        }
    )


def build_app() -> PathTokenGate:
    if not config.MCP_PATH_TOKEN:
        logger.error(
            "MCP_PATH_TOKEN is not set. Every request to /mcp/... will 404 until it is. "
            "Generate one with: python -c \"import secrets;print(secrets.token_urlsafe(32))\""
        )
    elif len(config.MCP_PATH_TOKEN) < 32:
        logger.warning(
            "MCP_PATH_TOKEN is only %d characters; the spec calls for 32+.",
            len(config.MCP_PATH_TOKEN),
        )

    # D2: stateless, because Autoscale instances are ephemeral and there is no
    # guaranteed session affinity between requests.
    mcp_app = mcp.http_app(path="/mcp", transport="http", stateless_http=True)

    app = Starlette(
        routes=[
            Route("/", root),
            # D5's health endpoint, served at three names.
            #
            # /healthz is unreachable on a published Replit Autoscale app: the
            # fronting infrastructure intercepts that exact path and answers
            # with its own branded 404, which never reaches this process. The
            # two 404s are distinguishable -- ours is `text/plain` "Not Found"
            # with the Google Frontend passthrough headers, theirs is Google's
            # HTML error page with none of them -- but only if you already know
            # to look. Verified against the deployment on 2026-08-04.
            #
            # So /status is the canonical name in production. /healthz and
            # /health stay registered because they work everywhere else, and a
            # health check that changes name between dev and prod is its own
            # kind of trap.
            Route("/status", healthz),
            Route("/health", healthz),
            Route("/healthz", healthz),
            Route("/.well-known/http-message-signatures-directory", signature_directory),
            # Placeholder so /mcp resolves in the route table; the gate below
            # intercepts before Starlette dispatches here.
            Mount("/mcp", app=mcp_app),
        ],
        lifespan=mcp_app.lifespan,
    )
    return PathTokenGate(app, mcp_app)


app = build_app()


def main() -> None:
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(config.BROWSERS_PATH))
    logger.info(
        "listening on %s:%d (chromium_present=%s, identity_mode=%s, version=%s)",
        config.HOST,
        config.PORT,
        config.chromium_present(),
        config.IDENTITY_MODE,
        config.VERSION,
    )
    uvicorn.run(
        app,
        host=config.HOST,
        port=config.PORT,
        log_level=config.LOG_LEVEL.lower(),
        access_log=False,
    )


if __name__ == "__main__":
    sys.exit(main())
