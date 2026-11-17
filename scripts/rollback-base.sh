#!/usr/bin/env bash
# Prints the commit a rollback of HEAD would return to: the release production
# runs. tests/test_rollback.py and scripts/rollback-smoke.sh check HEAD against
# it, in CI and by hand.
#
#   scripts/rollback-base.sh            the full SHA, on stdout
#
# Which release that is:
#
#   1. BLOCKTAIL_ROLLBACK_BASE, if set: an explicit choice (a tag, a SHA, a
#      branch), which must name a commit here;
#   2. otherwise the newest release tag that HEAD contains — v<digits>…, not a
#      pre-release (nothing with a "-", such as v0.3.0-rc1). A release is
#      tagged when it is deployed (docs/operations.md), so on main, on a
#      release branch and in a pull request alike this is what production
#      runs, however many commits came since and however they arrived.
#
# Not HEAD^1 and not the commit before a push: those are only the previous
# commit, which after a fast-forward of many commits is not a release at all.
# When HEAD is itself the newest release, it prints HEAD: there is no rollback
# to check, and the checks say so. Fails, saying why, when there is no release
# tag (or the history has no tags: actions/checkout needs fetch-depth: 0).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ref="${BLOCKTAIL_ROLLBACK_BASE:-}"
if [ -z "$ref" ]; then
  ref="$(git -C "$ROOT" describe --tags --abbrev=0 --match 'v[0-9]*' --exclude '*-*' HEAD 2>/dev/null)" || {
    echo "rollback-base: no release tag (v<digits>…) is an ancestor of HEAD — is the history" \
         "fetched with its tags (fetch-depth: 0)? Set BLOCKTAIL_ROLLBACK_BASE to name it." >&2
    exit 1
  }
fi
sha="$(git -C "$ROOT" rev-parse --verify --quiet "$ref^{commit}")" || {
  echo "rollback-base: $ref is not a commit in this repository" >&2
  exit 1
}
echo "rollback-base: $ref ($sha)" >&2
printf '%s\n' "$sha"
