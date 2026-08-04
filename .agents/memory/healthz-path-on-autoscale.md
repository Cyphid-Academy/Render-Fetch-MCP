---
name: /healthz unreachable on Autoscale
description: Google's frontend intercepts GET /healthz on published Replit Autoscale apps
---

On the published Autoscale deployment (*.replit.app, served via Google Frontend / App Engine-style routing), `GET /healthz` returns a Google HTML 404 that never reaches the app, while `/`, `/.well-known/...`, and `/mcp/<token>` route through fine.

**Why:** the fronting infrastructure reserves/intercepts the `/healthz` path in production. Verified 2026-08-04: same binary serves `/healthz` fine on the dev URL and localhost.

**Fixed 2026-08-04:** the health handler is now registered at `/status`, `/health`
and `/healthz`. `/status` is the canonical production name and is advertised in
the `/` response as `{"health": "/status"}`. `/healthz` stays registered because
it works everywhere except the published app, and a health check that changes
name between dev and prod is its own kind of trap.

**How to apply:** probe `/status` in production. Don't diagnose a "broken
deployment" from a prod `/healthz` 404.
