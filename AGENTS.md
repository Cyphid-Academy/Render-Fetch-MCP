# Upstream update policy

Two pinned dependencies drift and both matter: the vendored
`sidney/web-to-markdown-mcp` source, and patchright/Chromium.

## When to check
At the start of any session that touches this repository, if the last
recorded check in DECISIONS.md is more than 14 days old.

## How to check
Fetch the upstream commit log since the pinned SHA. Read the actual
diff, not just the commit messages.

## How to decide — use judgement, not a rule
Adopt automatically:
  - security fixes
  - bug fixes in the fetch or extraction path
  - new tier-1/tier-2 capability (e.g. new content-negotiation headers)
  - dependency bumps that don't change behaviour

Adopt only if it doesn't conflict with a decision in BUILD-SPEC §2:
  - new parameters or tool surface
  - changes to default timeouts or polling behaviour

Do NOT adopt; record and leave pinned:
  - anything that restructures the module boundary our wrapper imports
    across
  - anything reintroducing stdio-only assumptions
  - anything adding a runtime browser download

## Before merging any new pin
Run the full acceptance suite in BUILD-SPEC §8. A green suite is the
gate, not a code read. If T4, T9 or T11 regress, revert the pin.

## After deciding
Append to DECISIONS.md: date, old SHA, new SHA, what changed, what you
adopted or skipped and why, acceptance-suite result.

## Stealth decay
patchright's patches go stale after Chromium releases; anti-bot vendors
adapt continuously. If tier-3 fetches start returning content_ok=false
on sites that previously worked, that is the signal — check for a
patchright release before debugging our own code.

---

# Working notes for this repository

These are not part of the update policy above, but they are the things
most likely to be got wrong by someone editing this code.

## Install vs launch
Installing Chromium (~300 MB) is a **build** step. Launching it (~2 s) is a
**request** step. Never move the install into the request path — on a
scale-to-zero container that means a 300 MB download on every cold start,
onto ephemeral disk, inside a request timeout. It fails intermittently,
which is worse than failing outright.

## content_ok is the contract
The whole point of the envelope is that a calling model can tell "this page
is empty" from "a bot wall served me a challenge". If you add a tier or
change extraction, make sure `envelope.classify` still recognises the
failure, and that the hint still names the Wayback connector for walls.
A tier that returns a challenge page as if it were content is worse than a
tier that fails.

## Escalation is gated on content_ok, not on emptiness
`fetch._poll_tier3` and the ladder in `fetch.fetch_markdown` both refuse to
settle on content that classifies as unusable. This is deliberate: a stable
"Loading..." placeholder extracts cleanly and identically on consecutive
polls, so a pure stabilisation rule returns it in 250 ms and never renders
the page. Do not "simplify" this back to a stabilisation-only check.

## Accept-Encoding must match what we can decode
`identity.ACCEPT_ENCODING` is derived from importable codecs. Advertising
`br` without brotli installed returns undecoded bytes; extraction yields
nothing and every page silently falls through to the expensive tiers. This
was a real bug, caught by tiers 1 and 2 mysteriously never firing.

## Tiles must not be resized
`capture.tile_geometry` computes tile size from the model's published token
and pixel limits so that no downscale happens. If a tile is resized, body
text becomes unreadable and the image costs a full image's tokens to say
nothing. Do not hard-code a tile size.

## Blank-tile detection needs both signals
`is_blank` requires low entropy AND low ink ratio. Black text on white has a
strongly bimodal histogram and therefore low entropy — entropy alone drops
perfectly readable tiles. There is a test for this.
