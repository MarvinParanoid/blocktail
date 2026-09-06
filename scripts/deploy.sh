#!/usr/bin/env bash
# Ship this repository to the VPS and rebuild.
#
# Two things this script exists to get right, both learned the hard way.
#
# It finds its own root through git rather than by counting `..` from its own
# path. An earlier version used `$(dirname "$0")/../..`, which is one level
# above the project, and it cheerfully shipped every sibling repository in the
# parent directory to the server.
#
# And it looks at what it is about to send. A deploy that silently ships the
# wrong tree is worse than one that refuses to run, so anything at the top level
# that is not part of this repository stops it.
#
# Configuration stays on the server: .env and wallets.yml are never sent. A
# deploy that reverts a key rotated on the server, or a wallet added through the
# UI, is a deploy that loses data.
set -euo pipefail

HOST="${BLOCKTAIL_HOST:-tw-vps}"
REMOTE="${BLOCKTAIL_REMOTE:-/root/blocktail-build}"

ROOT="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
cd "$ROOT"

# The marker: this is blocktail and not whatever else happens to be a git repo.
grep -q '^name = "blocktail"' pyproject.toml || {
  echo "refusing: $ROOT does not look like blocktail" >&2
  exit 1
}

EXCLUDES=(
  --exclude=.git --exclude=.venv --exclude=__pycache__ --exclude='*.pyc'
  --exclude=data --exclude=.pytest_cache --exclude=.ruff_cache
  --exclude=.env --exclude=wallets.yml --exclude='*.db' --exclude='*.db-wal'
  --exclude='*.db-shm'
)

# Everything this repository is allowed to consist of, at the top level.
ALLOWED='^\.(github|gitignore|dockerignore|env\.example)$|^(app|docs|scripts|tests)$|^(ARCHITECTURE\.md|Dockerfile|LICENSE|README\.md|docker-compose\.yml|pyproject\.toml|requirements(-dev)?\.txt|wallets\.example\.yml)$'
unexpected="$(git ls-files --cached --others --exclude-standard \
  | cut -d/ -f1 | sort -u | grep -Ev "$ALLOWED" || true)"
if [ -n "$unexpected" ]; then
  echo "refusing: unexpected entries at the top level of $ROOT:" >&2
  echo "$unexpected" | sed 's/^/  /' >&2
  exit 1
fi

echo "shipping $(git ls-files | wc -l) tracked files from $ROOT"
tar "${EXCLUDES[@]}" -czf - . | ssh "$HOST" "cd '$REMOTE' && tar -xzf - \
  && docker compose build 2>&1 | tail -1 \
  && docker compose up -d --force-recreate 2>&1 | tail -1"
