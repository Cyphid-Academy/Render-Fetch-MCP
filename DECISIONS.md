# Decisions

Running record of pins, verifications, fallbacks taken, and measured
latencies. Append; do not rewrite history.

---

## 2026-08-03 — initial build

### Upstream pin (D1)

| | |
|---|---|
| Repository | `https://github.com/sidney/web-to-markdown-mcp` |
| Pinned SHA | `5afb9533cadce2bb768e3273e5ea234b8d60790f` |
| Commit date | 2026-07-26 |
| Commit subject | `logging setup` |
| Upstream version | 0.7.0 |
| Licence | MIT, preserved at `vendor/web_to_markdown_mcp/LICENSE` |
| Vendored at | `vendor/web_to_markdown_mcp/` |

Last upstream check: **2026-08-03**. Next check due after 2026-08-17 (AGENTS.md).

### The three "verify before writing code" items (§0)

**1. FastMCP constructor arguments (D2) — verified, spec was correct.**

Upstream depends on the standalone `fastmcp` package, not
`mcp.server.fastmcp`. This matters: `mcp` 2.0.0 **removed** the
`mcp.server.fastmcp` module entirely, so code written against that path would
not import. Installing `fastmcp` pins `mcp` back to 1.29.0.

`stateless_http=True` exists and is current in `fastmcp` 3.4.5, on both
`FastMCP.http_app()` and `FastMCP.run_http_async()`. There is now also a
shorter `stateless` alias; `stateless_http` is the one the spec names and is
still supported, so that is what is used. Verified by introspecting the
installed signature, not from documentation.

**2. `patchright install` flags (§6) — verified, spec was correct.**

`patchright install chromium` is valid on patchright 1.61.2. `--with-deps`
exists. Also available: `--only-shell`, `--no-shell`, `--force`, `--dry-run`,
`--no-progress`.

`--with-deps` is **not** used in the build command. It needs root to install
system packages and fails on Replit; the spec says not to let that failure
abort the build, and the simplest way to honour that is not to invoke it.
If Chromium later fails to launch for missing system libraries, take the
`replit.nix` fallback in §6 and record it here.

**3. Replit Autoscale maximum request duration (D9) — not published; fallback taken.**

Could not be verified. Replit's current documentation does not state a
request-duration limit for Autoscale anywhere I could find it: not in
`deployment-types`, `machine-configuration`, `autoscale-deployments`, or
`legal-and-security-info/usage`. Web search surfaced a 300 s *build* timeout
but no request timeout.

**Decision:** keep `TOTAL_BUDGET_MS = 55000` as specified. 55 s sits under the
60 s figure commonly reported for the platform, so the cap is safe under the
most likely real limit. It is read from the environment, so if the true limit
turns out to be lower this is a Secret change, not a rebuild.

**Action for the operator:** if a long tier-3 fetch ever returns a
platform-generated 5xx rather than our own envelope, the real limit is below
55 s. Set `TOTAL_BUDGET_MS` to that limit minus 5 s and append the number here.

### Deliberate decisions

**robots.txt is not consulted, in either identity mode (§3).**
This server acts on behalf of one identified human making individual
requests, not as a crawler. No robots parser is implemented and no per-call
override exists for a check that is not happening.

Recorded tension for anyone revisiting: `declared` mode advertises an
identity while ignoring the file that identity would normally respect. If
Cloudflare bot-directory registration is ever pursued, resolve this first —
their verified-bot bar includes obeying crawl directives.

**Out of scope, not relitigated (§9):** cache layer, persistent browser
context, Xvfb virtual display, robots.txt parsing.

### Deviations from the spec, and why

**1. Tier-3 polling is not a verbatim call into upstream's `_poll_until_stable`.**

The spec's D2 says the vendored code owns the fetching, and the initial
implementation did call upstream's polling function directly. Testing against
a JS-shell page showed it returns the *placeholder*: upstream's rule is "two
consecutive identical extractions", and a page holding a stable `Loading...`
div satisfies that at 250 ms, abandoning the remaining budget and returning
`Loading...` as the page content. Bot-challenge interstitials behave the same
way — they sit still while they work.

`app/fetch.py::_poll_tier3` keeps upstream's stabilisation rule and adds one
condition: stabilised content that classifies as unusable does not end the
poll early. Measured effect on the local JS-shell fixture: `content_ok=false`
with 10 characters before, `content_ok=true` with 1046 characters after.

This is a policy change about *when to stop*, not a change to extraction, and
upstream's `_ssrf` module is still used as-is via `app/guards.py`. Recorded
as a documented fallback under §0.

**2. Ladder escalation is gated on `content_ok`, not on "did the tier return
anything".**

Same root cause, one tier up. The spec says "using the cheapest that produces
usable content"; usable has to mean `content_ok`, or a cheap tier returning
`Loading...` ends the ladder before the browser ever runs. `fetch_markdown`
now treats a cheap tier's output as provisional and keeps escalating, while
retaining it as a fallback if the browser tier then fails outright.

**3. SSRF guard extended beyond upstream's classifier.**

D8 requires rejecting CGNAT. Python's `ipaddress.is_private` returns **False**
for `100.64.0.0/10`, so upstream's classifier passes it. `app/guards.py` adds
CGNAT, `192.0.0.0/24`, `198.18.0.0/15`, and `64:ff9b::/96`, unwraps
IPv4-in-IPv6 before classifying, and adds the own-origin rejection D8 asks for
(upstream has no notion of our origin). Upstream's guard still runs first.

### Bugs found and fixed during the build

**Accept-Encoding advertised codecs httpx could not decode.** The stealth
header set sent `gzip, deflate, br`. Without the `brotli` package installed,
httpx returned the response body as undecoded Brotli bytes. Extraction found
nothing, and *every* page silently fell through to tiers 2.5 and 3 — the
server would have worked, slowly and expensively, with no error anywhere.

Fixed twice over: `brotli` and `zstandard` are now pinned dependencies, and
`identity.ACCEPT_ENCODING` is derived from what is actually importable, so a
missing codec can only cost realism, never correctness.

**`clip` beyond the viewport failed.** `page.screenshot(clip=...)` addresses
the viewport unless `full_page=True` is also passed, so every tile below
800 px raised "Clipped area is either empty or outside the resulting image".
Fixed by passing both.

**Page height measured before render.** `capture` measured `scrollHeight`
immediately after the scroll pass, racing content injected a few hundred ms
after load. The same page reported 800 px in one run and 1636 px in another,
so `mode="full"` silently tiled only the shell. Now polled until two
consecutive reads agree.

### Measured latencies (local, sandboxed container)

Not the deployed figures — recorded as a baseline for comparison.

| Measurement | Result |
|---|---|
| T1 Cloudflare Markdown-for-Agents | `tier_used=1`, 15 359 chars, `content_ok=true` |
| Wikipedia article | `tier_used=2.5`, 19 122 chars, `content_ok=true` |
| python.org | `tier_used=2`, 2 288 chars, `content_ok=true` |
| JS-shell fixture | `tier_used=3`, 1 046 chars, `content_ok=true` |
| First tier-3 fetch (cold browser) | 1.76 s |
| Second tier-3 fetch (browser reused) | 0.99 s |
| Tier-3 fetch after reaper fired | 1.52 s |
| Tile geometry | 1280×800, 1334 tokens/tile |
| `capture_page` mode=full on the fixture | 2 tiles kept, 1 blank region skipped (800–1600 px) |

Wikipedia resolving to tier 2.5 rather than the spec's expected tier 2 is
likely an artefact of the sandbox's egress proxy; re-check on the deployment.

Local suite: **91 tests, all passing.**

### Acceptance suite (§8)

The §8 tests must be run against the **deployed** URL, which requires the
manual work order in `WORK-ORDER.md` to be completed first. Results are to be
recorded here once the deployment exists. T1, T2-equivalent, T4, T5, T6, T7,
T8, T11, T12 and T13 have all been exercised locally with the results above;
T3, T9, T10, T14, T15 and T16 need the public endpoint.


---

## 2026-08-04 — first deployment, production verification

Deployed to Replit Autoscale as **`render-fetch-mcp.replit.app`** (note: not
`render-fetch.replit.app`, which is a different, unpublished app — probing the
wrong host returns Replit's "This app isn't live yet" page and looks like a
broken deployment).

### Verified against the live deployment

| Check | Result |
|---|---|
| `GET /` | 200, `{"service":"render-fetch",...}` |
| `GET /.well-known/http-message-signatures-directory` (T13) | 200, valid Ed25519 JWK |
| `POST /mcp/<bad token>` (T8) | 404 |
| `GET /healthz` (T12) | **404 — intercepted upstream, see below** |
| `version` field (D5) | **`"unknown"` — see below** |

### Production defects found and fixed

**1. `/healthz` never reaches the app on published Autoscale.**

Replit's fronting infrastructure intercepts that exact path and answers with
its own branded Google 404. The two 404s are distinguishable, but only if you
already know to look:

- ours: `content-type: text/plain`, `content-length: 9`, body `Not Found`,
  with `server: Google Frontend`, `x-cloud-trace-context` and a `GAESA` cookie
- theirs: Google's HTML error page, **none** of those headers

This makes D5 and T12 unsatisfiable in production as originally built — and
D5's whole purpose is that a missing browser is "a one-line log discovery, not
a mystery timeout on the first tier-3 fetch weeks later". A health endpoint
that 404s in the environment it is meant to be checked in does not serve that.

Fixed by registering the handler at **`/status`** (canonical in production),
plus `/health` and `/healthz`. `/` now advertises the working path as
`{"health": "/status"}`. The other names stay registered because they work
everywhere else, and a health check that changes name between dev and prod is
its own kind of trap.

**2. `version` reported `"unknown"` in production.**

D5 specifies `"version": "<git sha>"`. The deployed image has no `.git`
directory, so the runtime `git rev-parse` returned nothing — precisely where
the answer matters most, since the version field is the only way to tell which
build is actually serving.

Fixed by stamping a `VERSION` file during the build, while the checkout is
still intact (`scripts/verify_build.py`), and consulting it before falling back
to git. `VERSION` is gitignored; it is a build artefact, not source.

### Adopted from the Replit working copy

- **Private key is no longer logged.** The original implementation logged a
  generated Ed25519 key in PEM form so the operator could copy it into the
  Secret. Convenient, but logs are not a private-key store. The warning now
  explains how to generate one locally instead. Adopted as-is; `WORK-ORDER.md`
  updated to match, since the old text told the operator to go and read the
  key out of the logs.
- `.replit` nix packages for Pillow's native dependencies, and the workflow
  block. Adopted.
- `replit.md` and `.agents/memory/` notes. Kept; the `/healthz` note was
  updated to record that the fix has landed rather than only the workaround.

Removed `attached_assets/` (a 295 KB screenshot of the publish log) — build
detritus, not part of the deliverable. Now gitignored. It was checked for
credential leakage first; it contains only build log output.

### Still outstanding

The **functional** acceptance tests against the deployed connector (T1–T6,
T9–T11, T14, T15, T16) have not been run. The `Render Fetch` connector is
installed and connected at org level but was toggled off mid-session
(`enabledInChat: false`), and the MCP path token is a Secret held by the
operator, so neither the tool surface nor the endpoint could be driven.

The two fixes above also require a redeploy before `/status` and a real
`version` are live. Re-run §8 after that.
