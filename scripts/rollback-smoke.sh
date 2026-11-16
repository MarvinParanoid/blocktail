#!/usr/bin/env bash
# The first deploy onto a legacy production, and the way back, with real
# Docker and the real scripts/remote-deploy.sh. CI runs it (the `rollback`
# job) against the release production runs; run it anywhere Docker and the
# compose plugin are installed:
#
#   scripts/rollback-smoke.sh [<previous release ref>]    default: origin/main
#
# It builds what production runs: the previous release, by its own
# Dockerfile, under the tag its own docker-compose.yml names. That is a
# rebuild from the same commit, not production's image itself — the base
# image is whatever its tag means today.
#
# Then, in a directory laid out as on the server (blocktail-build: the name
# the data volume is under), with only a .env, a wallets.yml and that
# compose file, no releases/ and no last-good:
#
#   1. the previous release runs under its own compose, and a wallet is added
#      from the page — data only the database holds;
#   2. a deploy of HEAD without ALLOWED_HOSTS is refused and changes nothing;
#   3. with it, the deploy adopts the running image as the baseline, builds,
#      backs up, migrates and records HEAD as good; a wallet is added again;
#   4. a rollback to the baseline runs the previous image under HEAD's
#      hardened compose (read-only root, no capabilities, the limits), keeps
#      both wallets, and reads and writes the migrated database;
#   5. a deploy of HEAD again reuses its image and keeps all three.
#
# It needs root for the server half (sudo when not root), as the server does.
# Nothing leaves the machine: the provider URL is a closed port, prices are
# off, every secret is a canary made up here. Everything it made is removed.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BASE="${1:-origin/main}"
WORK="$(mktemp -d)"
REMOTE="$WORK/blocktail-build"     # the project name, and so the volume's name
PROJECT="blocktail-build"
CONTAINER="blocktail"
PUBLIC="watch.example.test"
KEY="rollback-canary-key-$RANDOM$RANDOM$RANDOM"
PASSWORD="rollback-canary-password-$RANDOM$RANDOM"
PORT="$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
URL="http://127.0.0.1:$PORT"
SHA="$(git -C "$ROOT" rev-parse --short=12 HEAD)"
MADE_IMAGES=()
# An image of HEAD built before this ran — the release image CI ships — is
# the one deployed here (remote-deploy.sh never rebuilds a tag), and is kept.
RELEASE_ID="$(docker image inspect -f '{{.Id}}' "blocktail:$SHA" 2>/dev/null || true)"

say() { printf 'rollback-smoke: %s\n' "$*"; }
fail() { printf 'rollback-smoke: FAILED: %s\n' "$*" >&2; dump; exit 1; }
compose() { (cd "$REMOTE" && docker compose -p "$PROJECT" "$@"); }
dump() {
  docker logs --tail 60 "$CONTAINER" >&2 2>&1 || true
  [ -f "$REMOTE/deployments.log" ] && cat "$REMOTE/deployments.log" >&2 || true
}
cleanup() {
  compose down -v --remove-orphans >/dev/null 2>&1 || true
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  docker volume rm "${PROJECT}_blocktail-data" >/dev/null 2>&1 || true
  local made='^blocktail:(baseline-|'"$SHA"'$)'
  [ -z "$RELEASE_ID" ] || made='^blocktail:baseline-'
  for image in "${MADE_IMAGES[@]}" $(docker image ls blocktail --format '{{.Repository}}:{{.Tag}}' \
      | grep -E "$made" || true); do
    docker rmi -f "$image" >/dev/null 2>&1 || true
  done
  rm -rf "$WORK" 2>/dev/null || sudo rm -rf "$WORK" 2>/dev/null || true
}
trap cleanup EXIT

# get/post <path> [curl args]: as a browser on the page would, through the
# published port, with the name the instance is served as.
get() { curl -s -u "smoke:$PASSWORD" -H "Host: $PUBLIC" "$@"; }
code() { get -o /dev/null -w '%{http_code}' "$@"; }
add_wallet() {  # <name> <hex digit>
  local got
  got="$(code -X POST "$URL/wallets" -H 'HX-Request: true' -H 'Sec-Fetch-Site: same-origin' \
    -H "Origin: http://$PUBLIC" --data-urlencode "name=$1" \
    --data-urlencode "address=0x$(printf "$2%.0s" $(seq 1 40))" \
    -d chain=ethereum -d owned=on)"
  [ "$got" = "200" ] || fail "adding wallet $1 answered $got"
}
wallets() {  # the names the app lists, sorted, one line
  get "$URL/api/accounts" | python3 -c \
    'import json, sys; print(" ".join(sorted(a["name"] for a in json.load(sys.stdin)["accounts"])))'
}
running_id() { docker inspect -f '{{.Image}}' "$CONTAINER"; }
wait_healthy() {
  local state=""
  for _ in $(seq 1 75); do
    state="$(docker inspect -f '{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || true)"
    [ "$state" = "healthy" ] && return 0
    sleep 2
  done
  fail "the container did not become healthy (last state: ${state:-none})"
}
# The server half, from the release as deploy.sh ships it, and as root, the
# way it runs on the server: it hands backups/ to the app's uid with chown.
AS_ROOT=()
[ "$(id -u)" = "0" ] || AS_ROOT=(sudo --preserve-env=PATH)
remote_deploy() {  # <args>
  "${AS_ROOT[@]}" env BLOCKTAIL_REMOTE="$REMOTE" bash "$RELEASE_SCRIPTS/remote-deploy.sh" "$@"
}
schema() {
  docker exec "$CONTAINER" python -c "import sqlite3
c = sqlite3.connect('file:/data/blocktail.db?mode=ro', uri=True)
print(c.execute('SELECT max(version) FROM schema_migrations').fetchone()[0])"
}
no_errors_in_log() {  # <what> [secrets]: SQLite and filesystem errors; the key too if asked
  local log
  log="$(docker logs "$CONTAINER" 2>&1)"
  if printf '%s' "$log" | grep -E 'sqlite3\.|OperationalError|IntegrityError|DatabaseError|Read-only file system|PermissionError|Permission denied'; then
    fail "$1: errors in the log"
  fi
  if [ "${2:-}" = "secrets" ]; then
    grep -q -- "$KEY" <<<"$log" && fail "$1: a secret is in the log"
  else
    say "note: lines with the provider key in $1's log: $(printf '%s' "$log" | grep -c -- "$KEY" || true)"
  fi
  return 0
}
hardened() {  # <what>
  local got
  got="$(docker inspect -f '{{.HostConfig.ReadonlyRootfs}} {{.HostConfig.CapDrop}} {{.HostConfig.SecurityOpt}} {{.HostConfig.Memory}} {{.HostConfig.PidsLimit}}' "$CONTAINER")"
  [ "$got" = "true [ALL] [no-new-privileges:true] 536870912 256" ] || fail "$1: runs as $got"
  docker exec "$CONTAINER" sh -c 'touch /srv/x' 2>/dev/null && fail "$1: the root filesystem is writable"
  docker exec "$CONTAINER" sh -c 'touch /tmp/x && rm /tmp/x' || fail "$1: /tmp is not writable"
  [ "$(docker exec "$CONTAINER" sh -c "awk '/^CapEff/ {print \$2}' /proc/1/status")" = "0000000000000000" ] \
    || fail "$1: has capabilities"
}

# ------------------------------------------- 1. production as it is today

say "building the previous release ($BASE) by its own Dockerfile"
mkdir -p "$WORK/previous" "$REMOTE"
git -C "$ROOT" archive "$BASE" | tar -x -C "$WORK/previous"
LEGACY="$(sed -n 's/^[[:space:]]*image:[[:space:]]*//p' "$WORK/previous/docker-compose.yml" | head -n 1)"
[ -n "$LEGACY" ] || fail "$BASE's docker-compose.yml names no image"
docker image inspect "$LEGACY" >/dev/null 2>&1 && fail "$LEGACY already exists here; refusing to replace it"
docker build -q -t "$LEGACY" "$WORK/previous" >/dev/null
MADE_IMAGES+=("$LEGACY")
legacy_id="$(docker image inspect -f '{{.Id}}' "$LEGACY")"

cp "$WORK/previous/docker-compose.yml" "$REMOTE/"
printf 'wallets:\n  - name: Config\n    address: "0x%s"\n' "$(printf '1%.0s' $(seq 1 40))" \
  > "$REMOTE/wallets.yml"
cat > "$REMOTE/.env" <<EOF
ALCHEMY_URL=http://127.0.0.1:9/v2/$KEY
ALCHEMY_API_KEY=$KEY
PRICES=off
SYNC_INTERVAL_SECONDS=3600
AUTH_USER=smoke
AUTH_PASSWORD=$PASSWORD
HOST_PORT=$PORT
TZ=UTC
EOF
chmod 600 "$REMOTE/.env"
compose up -d --no-build >/dev/null
wait_healthy
add_wallet "From the page" 2
[ "$(wallets)" = "Config From the page" ] || fail "the previous release lists: $(wallets)"
legacy_uid="$(docker exec "$CONTAINER" id -u):$(docker exec "$CONTAINER" id -g)"
legacy_schema="$(schema)"
say "ok: $LEGACY runs as $legacy_uid at schema $legacy_schema, a wallet added from the page"

# ---------------------------------------------- 2. no ALLOWED_HOSTS, no deploy

INCOMING="$REMOTE/releases/.incoming-$SHA-smoke"
mkdir -p "$INCOMING"
git -C "$ROOT" archive HEAD | tar -x -C "$INCOMING"
RELEASE_SCRIPTS="$WORK/scripts"            # deploy moves $INCOMING; keep a copy to call
cp -r "$INCOMING/scripts" "$RELEASE_SCRIPTS"
if out="$(remote_deploy deploy "$SHA" "$INCOMING" 2>&1)"; then
  fail "a deploy without ALLOWED_HOSTS went through: $out"
fi
grep -q ALLOWED_HOSTS <<<"$out" || fail "the refusal does not name ALLOWED_HOSTS: $out"
[ "$(running_id)" = "$legacy_id" ] || fail "the refused deploy changed the running image"
[ ! -e "$REMOTE/last-good" ] && [ ! -e "$REMOTE/docker-compose.override.yml" ] \
  && [ ! -e "$REMOTE/backups" ] || fail "the refused deploy left a record, a pin or a backup"
cmp -s "$REMOTE/docker-compose.yml" "$WORK/previous/docker-compose.yml" \
  || fail "the refused deploy replaced docker-compose.yml"
say "ok: without ALLOWED_HOSTS the deploy is refused and nothing changes"

# ------------------------------------------------------------- 3. the deploy

echo "ALLOWED_HOSTS=$PUBLIC" >> "$REMOTE/.env"
INCOMING="$REMOTE/releases/.incoming-$SHA-smoke-2"     # shipped again, as a retry would be
mkdir -p "$INCOMING"
git -C "$ROOT" archive HEAD | tar -x -C "$INCOMING"
remote_deploy deploy "$SHA" "$INCOMING" 2> "$WORK/deploy.log" \
  || { cat "$WORK/deploy.log" >&2; fail "the deploy failed"; }
baseline="$(sed -n 's/.*recorded as \(blocktail:baseline-[0-9a-f]*\).*/\1/p' "$WORK/deploy.log")"
[ -n "$baseline" ] || { cat "$WORK/deploy.log" >&2; fail "the running image was not adopted as the baseline"; }
[ "$(docker image inspect -f '{{.Id}}' "$baseline")" = "$legacy_id" ] || fail "$baseline is not $LEGACY"
new_id="$(docker image inspect -f '{{.Id}}' "blocktail:$SHA")"
if [ -n "$RELEASE_ID" ]; then
  [ "$new_id" = "$RELEASE_ID" ] || fail "the deploy replaced the prebuilt blocktail:$SHA"
  grep -q "already built" "$WORK/deploy.log" || fail "the deploy rebuilt the prebuilt image"
  say "ok: the deploy used the prebuilt blocktail:$SHA ($RELEASE_ID)"
fi
[ "$(cat "$REMOTE/last-good")" = "blocktail:$SHA $new_id" ] || fail "last-good: $(cat "$REMOTE/last-good")"
backup=""
for path in "$REMOTE/backups"/*"-before-$SHA-from-baseline-"*.db; do
  [ -f "$path" ] && backup="${path##*/}"
done
[ -n "$backup" ] || fail "no backup named after the baseline in backups/: $(ls "$REMOTE/backups")"
grep -q "^.* $SHA ok image=blocktail:$SHA" "$REMOTE/deployments.log" || fail "deployments.log: $(cat "$REMOTE/deployments.log")"
[ "$(running_id)" = "$new_id" ] || fail "HEAD is not what runs"
new_schema="$(schema)"
[ "$new_schema" -ge "$legacy_schema" ] || fail "the schema went back ($legacy_schema -> $new_schema)"
[ "$new_schema" -gt "$legacy_schema" ] || say "note: this release has no migration"
[ "$(docker exec "$CONTAINER" id -u):$(docker exec "$CONTAINER" id -g)" = "$legacy_uid" ] \
  || fail "HEAD runs as another user than the volume's"
hardened "HEAD"
[ "$(wallets)" = "Config From the page" ] || fail "after the deploy: $(wallets)"
add_wallet "After the deploy" 3
no_errors_in_log "HEAD" secrets
say "ok: deployed $SHA over the baseline $baseline; backup $backup; schema $legacy_schema -> $new_schema"

# ----------------------------------------------------------- 4. the rollback

remote_deploy rollback "$baseline" 2> "$WORK/rollback.log" \
  || { cat "$WORK/rollback.log" >&2; fail "the rollback failed"; }
[ "$(running_id)" = "$legacy_id" ] || fail "the rollback does not run the baseline"
[ "$(cut -d' ' -f1 "$REMOTE/last-good")" = "$baseline" ] || fail "last-good after rollback: $(cat "$REMOTE/last-good")"
[ "$(schema)" = "$new_schema" ] || fail "the rollback changed the schema"
hardened "the previous release under HEAD's compose"
wait_healthy
for path in / /activity "/activity?q=page" /summary /wallets /status /api/status /api/portfolio; do
  [ "$(code "$URL$path")" = "200" ] || fail "the previous release: GET $path answered $(code "$URL$path")"
done
[ "$(wallets)" = "After the deploy Config From the page" ] || fail "after the rollback: $(wallets)"
add_wallet "During the rollback" 4
[ "$(wallets)" = "After the deploy Config During the rollback From the page" ] \
  || fail "the previous release did not write: $(wallets)"
no_errors_in_log "the previous release on the migrated database"
say "ok: rolled back to $baseline under the hardened compose; it reads and writes schema $new_schema, every wallet kept"

# ------------------------------------------------------ 5. and forward again

remote_deploy deploy "$SHA" 2> "$WORK/again.log" || { cat "$WORK/again.log" >&2; fail "the second deploy failed"; }
grep -q "already built" "$WORK/again.log" || fail "the second deploy rebuilt the image"
[ "$(running_id)" = "$new_id" ] || fail "HEAD is not what runs after the second deploy"
[ "$(wallets)" = "After the deploy Config During the rollback From the page" ] \
  || fail "after rolling forward: $(wallets)"
no_errors_in_log "HEAD again" secrets
say "ok: deployed $SHA again; the image was reused and every wallet kept"
