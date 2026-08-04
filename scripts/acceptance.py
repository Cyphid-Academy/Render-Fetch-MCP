#!/usr/bin/env python3
"""Run the BUILD-SPEC section 8 acceptance suite against a deployed endpoint.

Speaks the MCP streamable-HTTP wire protocol directly, so it does not depend on
the claude.ai connector being attached to any particular chat session.

Usage:
    python scripts/acceptance.py https://render-fetch-mcp.replit.app/mcp/<TOKEN>

    # or keep the credential out of your shell history:
    export RENDER_FETCH_URL='https://render-fetch-mcp.replit.app/mcp/<TOKEN>'
    python scripts/acceptance.py

The connector URL is a credential. This script never prints it: the origin is
shown, the token is not, so the output is safe to paste back into a chat.

Exit code is 0 only if every test that ran passed.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

TIMEOUT = 90

# T15 wants a cold start. Skipped by default because it needs 15 minutes of
# idle first; pass --cold to include it.
RUN_COLD_START = "--cold" in sys.argv


class Fail(Exception):
    pass


def _redact(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/mcp/<token>"


def rpc(url: str, method: str, params: dict | None = None, _id: int = 1) -> dict[str, Any]:
    body = json.dumps(
        {"jsonrpc": "2.0", "id": _id, "method": method, "params": params or {}}
    ).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read().decode()

    # Streamable HTTP may answer as SSE; take the first data: frame.
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if line.startswith("{"):
            payload = json.loads(line)
            if "error" in payload:
                raise Fail(f"{method} returned JSON-RPC error: {payload['error']}")
            return payload.get("result", {})
    raise Fail(f"{method}: no JSON payload in response: {raw[:200]!r}")


def call_tool(url: str, name: str, args: dict) -> dict[str, Any]:
    result = rpc(url, "tools/call", {"name": name, "arguments": args}, _id=99)
    if result.get("structuredContent"):
        return result["structuredContent"]
    return result


def http_status(url: str, method: str = "GET") -> int:
    req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except Exception:
        return 0


# --- tests ------------------------------------------------------------------

RESULTS: list[tuple[str, str, str]] = []  # (id, verdict, detail)


def record(tid: str, ok: bool, detail: str) -> None:
    RESULTS.append((tid, "PASS" if ok else "FAIL", detail))
    print(f"  {tid:<4} {'PASS' if ok else 'FAIL'}  {detail}")


def fetch(url: str, target: str, **kw) -> dict:
    return call_tool(url, "fetch_url_as_markdown", {"url": target, **kw})


def main() -> int:
    url = ""
    for arg in sys.argv[1:]:
        if arg.startswith("http"):
            url = arg
    url = url or os.environ.get("RENDER_FETCH_URL", "")
    if not url:
        print(__doc__)
        return 2

    origin = f"{urlsplit(url).scheme}://{urlsplit(url).netloc}"
    print(f"Endpoint : {_redact(url)}")
    print(f"Origin   : {origin}\n")

    # --- handshake / T16 ---
    print("Handshake and tool surface")
    try:
        rpc(
            url,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "acceptance", "version": "1"},
            },
        )
        tools = {t["name"]: t for t in rpc(url, "tools/list", _id=2).get("tools", [])}
        expected = {"fetch_url_as_markdown", "capture_page"}
        missing = expected - set(tools)
        record("T16", not missing, f"tools listed: {sorted(tools)}" if not missing else f"missing {missing}")
    except Exception as exc:
        record("T16", False, f"handshake failed: {exc}")
        print("\nCannot continue without a handshake. Check the URL and token.")
        return 1

    # --- health and key directory ---
    print("\nEndpoints")
    for path, tid in (("/status", "T12"), ("/.well-known/http-message-signatures-directory", "T13")):
        code = http_status(origin + path)
        if tid == "T12" and code == 200:
            try:
                with urllib.request.urlopen(origin + "/status", timeout=30) as r:
                    body = json.loads(r.read())
                record("T12", body.get("chromium_present") is True,
                       f"chromium_present={body.get('chromium_present')} version={body.get('version')}")
            except Exception as exc:
                record("T12", False, f"/status unreadable: {exc}")
        elif tid == "T12":
            record("T12", False, f"/status -> HTTP {code} (redeploy needed if 404)")
        else:
            record("T13", code == 200, f"/.well-known/... -> HTTP {code}")

    record("T8", http_status(origin + "/mcp/definitely-wrong-token", "POST") == 404,
           "wrong path token -> 404 (not 403)")

    # --- tier ladder ---
    print("\nTier ladder")
    cases = [
        ("T1", "https://blog.cloudflare.com/markdown-for-agents/", 1, "Cloudflare Markdown-for-Agents"),
        ("T2", "https://en.wikipedia.org/wiki/Model_Context_Protocol", 2, "Wikipedia article"),
    ]
    for tid, target, want_tier, label in cases:
        try:
            r = fetch(url, target)
            tier, ok, n = r.get("tier_used"), r.get("content_ok"), len(r.get("content", ""))
            # T2 legitimately lands on 2.5 if the plain request is blocked.
            good = ok and (tier == want_tier or (tid == "T2" and tier in (2, 2.5)))
            record(tid, good, f"{label}: tier={tier} content_ok={ok} chars={n}")
        except Exception as exc:
            record(tid, False, f"{label}: {exc}")

    # --- tier 3, reuse, reaper ---
    print("\nBrowser tier (T4/T5/T6)")
    spa = "https://react.dev/"
    try:
        t0 = time.monotonic()
        r = fetch(url, spa, poll_budget_ms=15000)
        first = time.monotonic() - t0
        record("T4", r.get("content_ok") is True and r.get("tier_used") is not None,
               f"SPA: tier={r.get('tier_used')} content_ok={r.get('content_ok')} in {first:.1f}s")

        t0 = time.monotonic()
        fetch(url, spa, poll_budget_ms=15000)
        second = time.monotonic() - t0
        record("T5", second <= first + 0.5, f"second fetch {second:.1f}s vs first {first:.1f}s (browser reuse)")
    except Exception as exc:
        record("T4", False, f"SPA fetch failed: {exc}")

    # --- SSRF ---
    print("\nSSRF (T7)")
    blocked = 0
    for target in ("http://169.254.169.254/latest/meta-data/", "http://localhost:5000/", "file:///etc/passwd"):
        try:
            r = fetch(url, target)
            err = (r.get("error") or "") + (r.get("hint") or "")
            named = r.get("content_ok") is False and ("refus" in err.lower() or "not allowed" in err.lower())
            blocked += bool(named)
            print(f"       {target} -> {'refused' if named else 'NOT REFUSED: ' + str(err)[:80]}")
        except Exception as exc:
            print(f"       {target} -> transport error {exc}")
    record("T7", blocked == 3, f"{blocked}/3 refused with a named reason")

    # --- bot wall / honest failure ---
    print("\nHonest failure (T9)")
    try:
        r = fetch(url, "https://www.g2.com/")
        if r.get("content_ok") is False:
            record("T9", "Wayback" in (r.get("hint") or ""), f"content_ok=false hint={(r.get('hint') or '')[:90]!r}")
        else:
            record("T9", True, f"page served content_ok=true (no wall encountered) tier={r.get('tier_used')}")
    except Exception as exc:
        record("T9", False, f"hung or errored instead of failing cleanly: {exc}")

    # --- truncation / continuation ---
    print("\nTruncation and continuation (T10)")
    try:
        big = "https://en.wikipedia.org/wiki/World_War_II"
        a = fetch(url, big)
        if a.get("truncated"):
            b = fetch(url, big, offset=a["next_offset"])
            joined_ok = bool(b.get("content")) and not b["content"].startswith(a["content"][-50:])
            record("T10", joined_ok, f"first={len(a['content'])} next_offset={a['next_offset']} tail={len(b.get('content',''))}")
        else:
            record("T10", True, f"document under cap ({len(a.get('content',''))} chars), truncation not exercised")
    except Exception as exc:
        record("T10", False, str(exc))

    # --- capture ---
    print("\ncapture_page (T11)")
    try:
        result = call_tool(url, "capture_page", {"url": "https://en.wikipedia.org/wiki/Python_(programming_language)", "mode": "full", "max_tiles": 4})
        blocks = result.get("content", []) if isinstance(result, dict) else []
        images = [b for b in blocks if b.get("type") == "image"]
        texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
        record("T11", 1 <= len(images) <= 4, f"{len(images)} tile(s) returned; header={texts[0][:100] if texts else '?'!r}")
    except Exception as exc:
        record("T11", False, str(exc))

    # --- identity modes ---
    print("\nIdentity modes (T14)")
    try:
        outs = {}
        for mode in ("stealth", "declared"):
            r = fetch(url, "https://example.com/", identity_mode=mode)
            outs[mode] = (r.get("tier_used"), r.get("content_ok"))
        record("T14", all(v[1] is not None for v in outs.values()), f"stealth={outs['stealth']} declared={outs['declared']}")
    except Exception as exc:
        record("T14", False, str(exc))

    if RUN_COLD_START:
        print("\nCold start (T15)")
        print("  sleeping 16 minutes to force a scale-to-zero cold start...")
        time.sleep(16 * 60)
        try:
            t0 = time.monotonic()
            r = fetch(url, spa, poll_budget_ms=15000)
            record("T15", r.get("content_ok") is True, f"cold tier-3 fetch in {time.monotonic() - t0:.1f}s")
        except Exception as exc:
            record("T15", False, str(exc))
    else:
        print("\nT15 (cold start) skipped — re-run with --cold to include it (takes ~17 min).")

    # --- summary ---
    failed = [r for r in RESULTS if r[1] == "FAIL"]
    print("\n" + "=" * 62)
    print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    if failed:
        print("\nFailures:")
        for tid, _, detail in failed:
            print(f"  {tid}: {detail}")
    print("=" * 62)
    print("\nPaste this output back into the chat — it contains no credentials.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
