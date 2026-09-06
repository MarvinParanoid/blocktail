#!/usr/bin/env bash
# Ship code to the VPS and rebuild. Configuration lives on the server: .env and
# wallets.yml are excluded, because a deploy that quietly reverts a key rotated
# on the server, or a wallet added through the UI, is a deploy that loses data.
set -euo pipefail
cd "$(dirname "$0")/../.." 2>/dev/null || true
tar --exclude=.git --exclude=.venv --exclude=__pycache__ --exclude='*.pyc' \
    --exclude=data --exclude=.pytest_cache --exclude=.ruff_cache \
    --exclude=.env --exclude=wallets.yml --exclude='*.db' \
    -czf - . | ssh tw-vps 'cd /root/blocktail-build && tar -xzf - \
      && docker compose build 2>&1 | tail -1 \
      && docker compose up -d 2>&1 | tail -1'
