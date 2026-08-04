#!/bin/bash
# Post-merge setup: keep the dev environment in sync after a task merge.
set -e

# Install/refresh Python dependencies (idempotent, non-interactive)
pip install -r requirements.txt --quiet

# Ensure Chromium is present for tier-3 rendering (no-op if already installed)
python -m patchright install chromium
