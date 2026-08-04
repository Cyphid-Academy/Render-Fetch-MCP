---
name: Chromium tier-3 launch dependencies
description: patchright's downloaded Chrome needs Nix system libs; chromium_present=True does not mean it can launch
---

Patchright launches the full Chrome binary (`.browsers/chromium-*/chrome-linux64/chrome`), not the headless shell. On this NixOS environment it fails at exec with `error while loading shared libraries: libnspr4.so` unless the Chromium runtime libs are in the Nix package set.

**Why:** the build check (`chromium_present`) only verifies the download exists; launch happens lazily on the first tier-3 request, so the failure surfaces only in a tier-3 fetch. Verified 2026-08-04: adding Nix packages nspr, nss, alsa-lib, atk, at-spi2-atk, at-spi2-core, cups, dbus, expat, glib, gtk3, libdrm, mesa, pango, cairo, libxkbcommon, xorg.libX11/libXcomposite/libXdamage/libXext/libXfixes/libXrandr/libxcb, and systemd (for libudev.so.1) made tier-3 work.

**How to apply:** if tier-3 fetches return a launch error, `ldd` the chrome binary and add missing libs as Nix packages — never `patchright install-deps` (apt, won't work here). The deployment bakes the Nix env from `.replit`, so production needs a republish after changing packages. Restart the workflow after installs; a process started before the env change keeps failing.
