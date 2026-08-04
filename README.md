# Render Fetch MCP

MCP server for retrieving bot-unfriendly page content.

A self-hosted remote MCP server that returns the readable content of a URL as
Markdown — and, on request, a screenshot — escalating to headless Chromium
only when cheaper methods fail. Connects to claude.ai as a custom connector.

Built for one operator, low request frequency, latency-tolerant. Correctness
and clarity of failure beat speed everywhere.

Upstream: [`sidney/web-to-markdown-mcp`](https://github.com/sidney/web-to-markdown-mcp)
(MIT), vendored at a pinned SHA — see [DECISIONS.md](DECISIONS.md).

## Tools

### `fetch_url_as_markdown`

Escalates through four tiers, using the cheapest that produces **usable**
content:

| Tier | Mechanism | Typical cost | Handles |
|---|---|---|---|
| 1 | Plain HTTP GET with `Accept: text/markdown` | ~300 ms | Cloudflare Markdown-for-Agents sites, anything doing content negotiation |
| 2 | Plain HTTP GET + trafilatura | ~500 ms | Ordinary static HTML and server-rendered pages |
| 2.5 | `curl_cffi` browser-TLS impersonation + trafilatura | ~700 ms | Sites blocking on TLS/JA3 fingerprint alone |
| 3 | patchright (anti-detection Playwright fork) + Chromium | 2–8 s | JS-rendered SPAs, soft bot walls, JS-shell pages |

Tier 3 is the rare path. It exists so that the rare page is readable at all,
not so that it is readable quickly.

Returns a structured envelope:

```json
{
  "content": "<markdown>",
  "tier_used": 1,
  "identity_mode": "stealth",
  "final_url": "https://example.com/",
  "http_status": 200,
  "content_ok": true,
  "truncated": false,
  "next_offset": null,
  "hint": null
}
```

`content_ok` is the load-bearing field: **false** when the extraction looks
like a bot-challenge interstitial, a JS shell, a login wall, or is
implausibly short. When it is false, `hint` names the next thing to try — for
a wall, that is the Wayback Machine connector, so the calling model stops
retrying something that will not start working.

### `capture_page`

Screenshots the rendered page as vision-ready image **tiles**. Always tier 3.

One tall full-page PNG is close to useless to a vision model: it gets resized
until the body text is a few pixels tall, costs a full image's tokens, and
conveys nothing. So the page is tiled at a size computed from the model's
published token and pixel limits such that no resize happens at all. Blank and
near-blank regions are detected by entropy and ink ratio and skipped, with the
dropped vertical ranges stated in the response.

Modes: `viewport` (1 tile, the cheap default), `full` (tiled), `element`
(single CSS-selected clip).

**Cost:** ~1334 visual tokens per tile. Four tiles ≈ 5 336; eight ≈ 10 672.

## Limits

- No authenticated or logged-in pages. Clean context, no cookies.
- Interactive Cloudflare Turnstile, DataDome, PerimeterX and Kasada are not
  bypassed. Datacenter egress makes encountering them more likely, not less.
- Slow progressive-render SPAs may return partial content at budget expiry.
- These are **readers**, not automation tools. They cannot click, fill, or log in.
- `capture_page` is expensive in tokens. Use `fetch_url_as_markdown` for
  anything fundamentally textual.

## Endpoints

| Route | Purpose |
|---|---|
| `POST /mcp/<token>` | The MCP endpoint (streamable HTTP, stateless) |
| `GET /status` | `{"ok", "chromium_present", "identity_mode", "version"}` — also at `/health` and `/healthz` |
| `GET /.well-known/http-message-signatures-directory` | Web Bot Auth key |

Auth is a secret path segment compared in constant time. A mismatch returns
**404, not 403** — a 403 confirms the path space exists.

Use **`/status`** in production. Replit's fronting infrastructure intercepts
the exact path `/healthz` on a published Autoscale app and answers with its
own branded 404 that never reaches this process, so a `/healthz` 404 in
production says nothing about the app's health. All three names work locally.

## Configuration

| Secret | Default | Purpose |
|---|---|---|
| `MCP_PATH_TOKEN` | none — **required** | Secret path segment |
| `IDENTITY_MODE` | `stealth` | `stealth` or `declared` |
| `SIGNING_KEY_PEM` | generated | Ed25519 private key for Web Bot Auth |
| `PUBLIC_ORIGIN` | none | Public origin, for signing and own-origin SSRF checks |
| `MAX_CONTENT_CHARS` | `40000` | Truncation cap |
| `POLL_BUDGET_MS` | `8000` | Tier 3 stabilisation budget |
| `TOTAL_BUDGET_MS` | `55000` | Hard wall-clock cap |
| `BROWSER_IDLE_TIMEOUT_S` | `120` | Idle browser reaper |
| `MAX_TILES` | `4` | Screenshot tile cap (hard max 8) |
| `LOG_LEVEL` | `INFO` | |

## Identity

`stealth` (default) presents a realistic browser identity: current Chrome UA,
browser header ordering, and `curl_cffi` TLS impersonation at tier 2.5.

`declared` self-identifies honestly and signs requests with Web Bot Auth —
Ed25519, HTTP Message Signatures (RFC 9421), and a `Signature-Agent` header
pointing at the directory this server publishes. Registering with Cloudflare's
bot directory is a later manual step; the plumbing is already here, so
registering is a form submission rather than a rebuild.

**robots.txt is not consulted in either mode.** This server acts for one
identified human making individual requests, not as a crawler. See
[DECISIONS.md](DECISIONS.md) for the recorded reasoning and its tension with
`declared` mode.

## Local development

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/patchright install chromium
export MCP_PATH_TOKEN=$(python -c "import secrets;print(secrets.token_urlsafe(32))")
.venv/bin/python main.py            # http://localhost:5000
.venv/bin/python -m pytest tests/   # 91 tests
```

## Deployment

Replit **Autoscale**, 2 vCPU / 4 GiB, max 1 instance.

```
build : pip install -r requirements.txt && patchright install chromium && python scripts/verify_build.py
run   : python main.py
```

Chromium is installed at **build** time (~300 MB, baked into the image) and
launched at **request** time (~2 s, lazily, on the first tier-3 call).
Conflating the two is the main way this deployment goes wrong: installing at
runtime on a scale-to-zero container means a 300 MB download on every cold
start, inside a request timeout.

See [WORK-ORDER.md](WORK-ORDER.md) for the console steps, and
[AGENTS.md](AGENTS.md) for the upstream update policy.
