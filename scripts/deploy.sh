#!/usr/bin/env bash
# Ship the committed HEAD of this repository to the VPS and start it there.
#
#   scripts/deploy.sh               check, ship, back up, start, verify
#   scripts/deploy.sh --check-only  everything up to shipping; no network
#
# What it ships is `git archive HEAD` and nothing else: no working-tree file,
# no ignored file, no .env, no database. That replaces two older guards — an
# allowlist of top-level names, and a root found through git rather than by
# counting `..` (one version shipped every sibling repository in the parent
# directory). Archiving a commit cannot do either.
#
# It refuses, with no switch to skip it:
#   - a working tree with any change, staged, unstaged or untracked;
#   - a HEAD that is not a commit;
#   - a commit whose tests or lint fail — run on the archive itself, so what
#     passed is exactly what ships;
#   - a commit that tracks .env, wallets.yml or a database file.
#
# On the server, scripts/remote-deploy.sh (from the shipped release) builds an
# image tagged with the commit, keeps the running one as blocktail:previous,
# backs up SQLite, starts the new image and waits for /healthz. See
# docs/operations.md for what to do when that fails.
#
# Configuration stays on the server: .env and wallets.yml are never sent. A
# deploy that reverts a key rotated on the server, or a wallet added through the
# UI, is a deploy that loses data.
set -euo pipefail

HOST="${BLOCKTAIL_HOST:-tw-vps}"
REMOTE="${BLOCKTAIL_REMOTE:-/root/blocktail-build}"
SSH="${BLOCKTAIL_SSH:-ssh}"   # the test suite substitutes a double; nothing else should

CHECK_ONLY=0
case "${1:-}" in
  "") ;;
  --check-only) CHECK_ONLY=1 ;;
  *) echo "usage: scripts/deploy.sh [--check-only]" >&2; exit 2 ;;
esac

refuse() { printf 'refusing: %s\n' "$*" >&2; exit 1; }
say() { printf 'deploy: %s\n' "$*" >&2; }

ROOT="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
cd "$ROOT"

# The marker: this is blocktail and not whatever else happens to be a git repo.
grep -q '^name = "blocktail"' pyproject.toml 2>/dev/null \
  || refuse "$ROOT does not look like blocktail"

git rev-parse --verify --quiet 'HEAD^{commit}' >/dev/null \
  || refuse "HEAD is not a commit (nothing committed yet?)"

dirty="$(git status --porcelain=v1 --untracked-files=all)"
if [ -n "$dirty" ]; then
  printf 'refusing: the working tree is not clean; commit or stash first:\n' >&2
  printf '%s\n' "$dirty" | sed 's/^/  /' >&2
  exit 1
fi

SHA="$(git rev-parse --short=12 HEAD)"
say "commit $SHA — $(git log -1 --format='%s (%an, %ad)' --date=short HEAD)"

# A secret or a database that was committed by mistake is in the archive too.
tracked_secrets="$(git ls-files | grep -E '(^|/)\.env$|(^|/)wallets\.yml$|\.db(-wal|-shm|-journal)?$' || true)"
[ -z "$tracked_secrets" ] || refuse "HEAD tracks files that must never ship: $tracked_secrets"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
ARCHIVE="$WORK/blocktail-$SHA.tar.gz"
git archive --format=tar.gz -o "$ARCHIVE" HEAD

# Second look, at the archive itself rather than at the index.
if tar -tzf "$ARCHIVE" | grep -E '(^|/)\.env$|(^|/)wallets\.yml$|\.db(-wal|-shm|-journal)?$|(^|/)data/' ; then
  refuse "the archive contains files that must never ship (listed above)"
fi

# Tests and lint run on the extracted archive, not on the working tree, so an
# ignored local file cannot make them pass. Same lint as CI.
PY="${BLOCKTAIL_PYTHON:-}"
if [ -z "$PY" ]; then
  if [ -x "$ROOT/.venv/bin/python" ]; then PY="$ROOT/.venv/bin/python"; else PY="python3"; fi
fi
mkdir "$WORK/tree"
tar -xzf "$ARCHIVE" -C "$WORK/tree"
say "running the test suite on $SHA"
(cd "$WORK/tree" && "$PY" -m pytest -p no:cacheprovider) >&2 \
  || refuse "tests failed on $SHA"
say "running ruff (F, E9) on $SHA"
(cd "$WORK/tree" && "$PY" -m ruff check --no-cache --select F,E9 app tests) >&2 \
  || refuse "ruff failed on $SHA"

say "archive: $(tar -tzf "$ARCHIVE" | grep -vc '/$') files"
if [ "$CHECK_ONLY" = 1 ]; then
  tar -tzf "$ARCHIVE"   # stdout: what would ship
  say "check only: $SHA would ship; nothing was sent"
  exit 0
fi

q() { printf '%q' "$1"; }
RELEASES="$REMOTE/releases"
say "shipping $SHA to $HOST:$RELEASES/$SHA"
"$SSH" "$HOST" "set -e; mkdir -p $(q "$RELEASES"); rm -rf $(q "$RELEASES/$SHA.incoming");
  mkdir $(q "$RELEASES/$SHA.incoming"); tar -xzf - -C $(q "$RELEASES/$SHA.incoming");
  rm -rf $(q "$RELEASES/$SHA"); mv $(q "$RELEASES/$SHA.incoming") $(q "$RELEASES/$SHA")" \
  < "$ARCHIVE"

"$SSH" "$HOST" "BLOCKTAIL_REMOTE=$(q "$REMOTE") bash $(q "$RELEASES/$SHA/scripts/remote-deploy.sh") deploy $(q "$SHA")"
say "deployed $SHA"
