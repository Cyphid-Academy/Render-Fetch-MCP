# Manual work order

Everything in this build that needs a human at a console, batched into one
pass. Organised tool → screen → field, with the fully qualified path to every
value. Nothing here was done for you; nothing here needs a code change.

Work top to bottom — later steps consume values produced by earlier ones.

---

## 0. Values to generate first

Run these locally and keep the output to hand. Both are pasted into Replit
Secrets in step 2.

**Path token** (32+ URL-safe random characters, for `MCP_PATH_TOKEN`):

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

**Ed25519 signing key** (for `SIGNING_KEY_PEM`):

```bash
python -c "
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
print(ed25519.Ed25519PrivateKey.generate().private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption()).decode())"
```

Paste the key **including** the `-----BEGIN PRIVATE KEY-----` and
`-----END PRIVATE KEY-----` lines.

> If you skip the key, the server generates an ephemeral one at boot and warns
> that it did. It will work, but the key changes on every cold start, which
> makes the published Web Bot Auth directory meaningless. The key material is
> deliberately never written to the logs, so a skipped key cannot be recovered
> after the fact — generate it here and store it in the Secret.

---

## 1. GitHub — repository

**Tool:** github.com

| Screen | Field | Value |
|---|---|---|
| Repositories → New repository | Owner | `Cyphid-Academy` |
| | Repository name | `render-fetch-mcp` |
| | Visibility | Private (public is fine; nothing secret is committed) |
| | Initialize with README / .gitignore / license | **all unchecked** — the branch already has them |

The code is already pushed to the branch `claude/mcp-server-bot-unfriendly-aa0ph3`
with a draft pull request open. Merge that PR to `main` before deploying, or
point Replit at the branch directly in step 2.

---

## 2. Replit — app, secrets, deployment

**Tool:** replit.com

### 2a. Create the app

| Screen | Field | Value |
|---|---|---|
| Home → Create App → Import from GitHub | Repository | `Cyphid-Academy/render-fetch-mcp` |
| | Branch | `main` (or `claude/mcp-server-bot-unfriendly-aa0ph3`) |
| | App name | `render-fetch` |

### 2b. Secrets

**Screen:** left sidebar → **Tools** → **Secrets** → *New secret* for each row.

| Key | Value | Required |
|---|---|---|
| `MCP_PATH_TOKEN` | the token from step 0 | **yes** |
| `SIGNING_KEY_PEM` | the PEM block from step 0 | strongly recommended |
| `PUBLIC_ORIGIN` | `https://render-fetch-mcp.replit.app` | recommended — enables own-origin SSRF rejection and absolute `Signature-Agent` URLs |
| `IDENTITY_MODE` | `stealth` | no (this is the default) |
| `MAX_CONTENT_CHARS` | `40000` | no |
| `POLL_BUDGET_MS` | `8000` | no |
| `TOTAL_BUDGET_MS` | `55000` | no |
| `BROWSER_IDLE_TIMEOUT_S` | `120` | no |
| `MAX_TILES` | `4` | no |
| `LOG_LEVEL` | `INFO` | no |

Only `MCP_PATH_TOKEN` has no default. Add the optional rows only if you intend
to change them from the values already in `app/config.py`.

`PUBLIC_ORIGIN` is a chicken-and-egg: the URL is not known until the first
publish. Deploy once, read the URL off the deployment screen, then add the
Secret and republish.

### 2c. Deployment

**Screen:** top right → **Deploy** → **Publish**.

| Field | Value |
|---|---|
| Deployment type | **Autoscale** |
| Machine power | **2 vCPU / 4 GiB** |
| Max machines | **1** |
| Build command | `pip install -r requirements.txt && patchright install chromium && python scripts/verify_build.py` |
| Run command | `python main.py` |
| App name / subdomain | `render-fetch` |

The build command is already in `.replit` under `[deployment]`; confirm the
publish screen shows it rather than a default.

**The build takes several minutes** — it downloads ~300 MB of Chromium. That
is expected and happens once per deployment, not per request. The last build
step prints the resolved Chromium path and its size, and fails the build if
the binary is missing. If you do not see `OK: chromium is installed and
executable.` in the build log, do not proceed.

### 2d. Verify before connecting

Replace `<TOKEN>` with your `MCP_PATH_TOKEN`.

```bash
curl -s https://render-fetch-mcp.replit.app/status
# expect: {"ok":true,"chromium_present":true,"identity_mode":"stealth","version":"<sha>",...}
# NOTE: use /status, not /healthz. Replit's frontend intercepts /healthz on
# published Autoscale apps and returns its own 404 without reaching the app.

curl -s https://render-fetch-mcp.replit.app/.well-known/http-message-signatures-directory
# expect: {"keys":[{"kty":"OKP","crv":"Ed25519",...}]}

curl -s -o /dev/null -w '%{http_code}\n' -X POST https://render-fetch-mcp.replit.app/mcp/wrong
# expect: 404
```

`chromium_present: false` means the build did not bake the browser in. Fix
that before going further — every tier-3 fetch and every `capture_page` call
will fail without it.

---

## 3. claude.ai — custom connector

**Tool:** claude.ai

| Screen | Field | Value |
|---|---|---|
| Settings → Connectors → **Add custom connector** | Name | `Render Fetch` |
| | URL | `https://render-fetch-mcp.replit.app/mcp/<MCP_PATH_TOKEN>` |
| | Authentication | None — the token is the path segment |

**Assemble the URL** by substituting your step-0 token:

```
https://render-fetch-mcp.replit.app/mcp/<paste MCP_PATH_TOKEN here>
```

Treat this URL as a credential. Anyone holding it can drive the server.

After adding, confirm both `fetch_url_as_markdown` and `capture_page` appear
in the connector's tool list.

---

## 4. Acceptance suite

Run the §8 tests from the build spec against the **deployed** URL and append
the results to `DECISIONS.md`. T9, T11 and T15 are the decisive ones:

- **T9** — a page behind an aggressive bot wall must return `content_ok: false`
  with a hint naming the Wayback connector, not a hang and not a wall of
  challenge HTML.
- **T11** — `capture_page` tiles must be legible at native resolution, count
  ≤ `MAX_TILES`.
- **T15** — cold start after 15+ minutes idle, then a tier-3 fetch. Record the
  total latency including container cold start. This determines whether
  Autoscale is viable for this workload.

If a long tier-3 fetch returns a platform 5xx rather than this server's own
envelope, Replit's request limit is below 55 s: set `TOTAL_BUDGET_MS` to that
limit minus 5 s and record the number in `DECISIONS.md`. See the D9 entry
there — the limit is not published and could not be verified at build time.

---

## 5. Deliberately not done

- **Cloudflare bot-directory registration.** The signing plumbing and the
  well-known endpoint are built, so this is a form submission later rather
  than a rebuild. Resolve the robots.txt tension recorded in `DECISIONS.md`
  first — Cloudflare's verified-bot bar includes obeying crawl directives.
- **`replit.nix` fallback.** Only needed if Chromium will not launch after a
  plain install (missing system libraries, GLIBC mismatch). Not written,
  because it is not yet known to be needed. Procedure is in build spec §6.
