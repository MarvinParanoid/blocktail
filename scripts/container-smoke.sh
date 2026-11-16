#!/usr/bin/env bash
# The image and docker-compose.yml, for real: what tests/test_container.py can
# only read. CI runs it on every push (the `image` job); run it anywhere Docker
# and the compose plugin are installed:
#
#   scripts/container-smoke.sh [image tag]
#   scripts/container-smoke.sh --prebuilt <image tag>   an image already built
#
# --prebuilt runs every check on an image someone else built — the release
# image CI ships — instead of building one: nothing was planted in its build,
# so it looks for files that must never be in an image instead.
#
# Nothing leaves the machine: the provider URL points at a closed port on the
# container's own loopback, prices are off, and every secret is a canary made
# up here — the point is to show none of them ends up in the image or a log.
#
# It builds from a copy of the tracked files plus a planted .env, wallets.yml
# and database, as in a developer's checkout; starts the service with the
# repository's compose file (read-only root, no capabilities, the named
# volume); checks the user, the filesystem, the HTTP policy, SQLite's WAL,
# the stop, and a backup and restore the way scripts/remote-deploy.sh and
# docs/operations.md do them; and removes everything it made.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PREBUILT=""
if [ "${1:-}" = "--prebuilt" ]; then
  PREBUILT=1
  shift
  [ -n "${1:-}" ] || { echo "usage: container-smoke.sh --prebuilt <image tag>" >&2; exit 2; }
fi
TAG="${1:-blocktail:smoke}"
WORK="$(mktemp -d)"
PROJECT="blocktail-smoke-$$"
CONTAINER="blocktail"              # container_name in docker-compose.yml
PUBLIC="watch.example.test"
KEY="smoke-canary-key-$RANDOM$RANDOM$RANDOM"
PASSWORD="smoke-canary-password-$RANDOM$RANDOM"
TOKEN="$(printf 'smoke:%s' "$PASSWORD" | base64 -w0)"
PORT="$(python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
URL="http://127.0.0.1:$PORT"

say() { printf 'container-smoke: %s\n' "$*"; }
fail() { printf 'container-smoke: FAILED: %s\n' "$*" >&2; dump; exit 1; }
compose() { (cd "$WORK/run" && docker compose -p "$PROJECT" "$@"); }
dump() { compose logs --no-color 2>/dev/null | tail -n 60 >&2 || true; }
cleanup() {
  compose down -v --remove-orphans >/dev/null 2>&1 || true
  rm -rf "$WORK" 2>/dev/null || sudo rm -rf "$WORK" 2>/dev/null || true
}
trap cleanup EXIT

# status <expected> <curl args...>
status() {
  local want="$1" got
  shift
  got="$(curl -s -o /dev/null -w '%{http_code}' "$@")"
  [ "$got" = "$want" ] || fail "expected HTTP $want, got $got: curl $*"
}

wait_healthy() {
  local state=""
  for _ in $(seq 1 60); do
    state="$(docker inspect -f '{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || true)"
    [ "$state" = "healthy" ] && return 0
    sleep 2
  done
  fail "the container did not become healthy (last state: ${state:-none})"
}

# --------------------------------------------------------------- the image

if [ -n "$PREBUILT" ]; then
  docker image inspect "$TAG" >/dev/null 2>&1 || fail "no image $TAG to check"
  say "checking the prebuilt $TAG ($(docker image inspect -f '{{.Id}}' "$TAG"))"
else
  say "building $TAG from the tracked files plus planted local secrets"
  mkdir -p "$WORK/src"
  (cd "$ROOT" && git ls-files -z | xargs -0 cp --parents -t "$WORK/src")
  printf 'ALCHEMY_API_KEY=%s\nAUTH_PASSWORD=%s\n' "$KEY" "$PASSWORD" > "$WORK/src/.env"
  printf 'wallets: []\n# %s\n' "$KEY" > "$WORK/src/wallets.yml"
  mkdir -p "$WORK/src/data"
  printf '%s' "$KEY" > "$WORK/src/data/blocktail.db"
  docker build -q -t "$TAG" "$WORK/src" >/dev/null
fi

uid="$(docker run --rm --entrypoint id "$TAG" -u)"
gid="$(docker run --rm --entrypoint id "$TAG" -g)"
[ "$uid" != "0" ] || fail "the image runs as root"
say "ok: the image runs as uid $uid"

if [ -n "$PREBUILT" ]; then
  found="$(docker run --rm --entrypoint sh "$TAG" -c 'find / -xdev \( -name .env -o -name "wallets.yml" \
    -o -name "*.db" -o -name "*.db-wal" -o -name "*.db-shm" -o -name "*.pem" -o -name "id_*" \) \
    -not -path "/proc/*" 2>/dev/null || true')"
  [ -z "$found" ] || fail "files that must never ship are in the image: $found"
  docker image inspect -f '{{json .Config.Env}}' "$TAG" | grep -qE '(ALCHEMY_API_KEY|ALCHEMY_URL|AUTH_USER|AUTH_PASSWORD)=' \
    && fail "the image config carries a credential: $(docker image inspect -f '{{json .Config.Env}}' "$TAG")"
  say "ok: no .env, wallets.yml, database, key file or credential variable in the image"
else
  found="$(docker run --rm --entrypoint sh "$TAG" -c \
    'grep -rIl --exclude-dir=proc --exclude-dir=sys --exclude-dir=dev -- "$0" / 2>/dev/null || true' "$KEY")"
  [ -z "$found" ] || fail "a planted secret is in the image: $found"
  docker image inspect "$TAG" | grep -q -- "$KEY" && fail "a planted secret is in the image config"
  docker history --no-trunc "$TAG" | grep -q -- "$KEY" && fail "a planted secret is in the image history"
  say "ok: no planted secret in the image's files, config or history"
fi

# ----------------------------------------------------------------- compose

mkdir -p "$WORK/run"
cp "$ROOT/docker-compose.yml" "$WORK/run/"
printf 'wallets:\n  - name: Smoke\n    address: "0x%s"\n' "$(printf '1%.0s' $(seq 1 40))" \
  > "$WORK/run/wallets.yml"
cat > "$WORK/run/docker-compose.override.yml" <<EOF
services:
  blocktail:
    image: $TAG
EOF
cat > "$WORK/run/.env" <<EOF
ALCHEMY_URL=http://127.0.0.1:9/v2/$KEY
ALCHEMY_API_KEY=$KEY
PRICES=off
SYNC_INTERVAL_SECONDS=3600
AUTH_USER=smoke
AUTH_PASSWORD=$PASSWORD
HOST_PORT=$PORT
EOF
if compose config >/dev/null 2>&1; then
  fail "compose accepted a .env without ALLOWED_HOSTS"
fi
say "ok: compose refuses to run without ALLOWED_HOSTS"
echo "ALLOWED_HOSTS=$PUBLIC" >> "$WORK/run/.env"

compose up -d --no-build >/dev/null
wait_healthy
say "ok: healthy by its own healthcheck, with no credentials"

# ------------------------------------------------------- inside the container

[ "$(docker exec "$CONTAINER" id -u)" = "$uid" ] || fail "the container runs as another user"
[ "$(docker exec "$CONTAINER" id -u)" != "0" ] || fail "the container runs as root"
docker exec "$CONTAINER" sh -c 'touch /srv/x' 2>/dev/null && fail "the root filesystem is writable"
docker exec "$CONTAINER" sh -c 'touch /tmp/x && rm /tmp/x' || fail "/tmp is not writable"
caps="$(docker exec "$CONTAINER" sh -c "awk '/^CapEff/ {print \$2}' /proc/1/status")"
[ "$caps" = "0000000000000000" ] || fail "effective capabilities: $caps"
nnp="$(docker exec "$CONTAINER" sh -c "awk '/^NoNewPrivs/ {print \$2}' /proc/1/status")"
[ "$nnp" = "1" ] || fail "no-new-privileges is not set"
say "ok: uid $uid, read-only root, writable /tmp, no capabilities, no-new-privileges"

mode="$(docker exec "$CONTAINER" python -c \
  "import sqlite3; print(sqlite3.connect('/data/blocktail.db').execute('PRAGMA journal_mode').fetchone()[0])")"
[ "$mode" = "wal" ] || fail "journal_mode is $mode"
docker exec "$CONTAINER" test -f /data/blocktail.db-wal || fail "no -wal file in /data"
owner="$(docker exec "$CONTAINER" stat -c '%u' /data/blocktail.db)"
[ "$owner" = "$uid" ] || fail "/data/blocktail.db is owned by $owner"
say "ok: SQLite runs in WAL in the volume, owned by the app"

# ------------------------------------------------------------ over HTTP

pub=(-H "Host: $PUBLIC")
auth=(-H "Authorization: Basic $TOKEN")
status 200 "${pub[@]}" "$URL/healthz"
status 401 "${pub[@]}" "$URL/"
status 401 "${pub[@]}" "$URL/api/status"
status 200 "${pub[@]}" "${auth[@]}" "$URL/"
status 400 "${auth[@]}" "$URL/"                                   # Host: 127.0.0.1:<port>
status 400 -H "Host: localhost" "${auth[@]}" "$URL/"
status 400 -H "Host: 127.0.0.1" "$URL/healthz"                    # loopback from outside
status 400 -H "Host: evil.example" "${auth[@]}" "$URL/"
status 403 "${pub[@]}" "${auth[@]}" -X POST -H "Sec-Fetch-Site: cross-site" "$URL/history/older"
status 403 "${pub[@]}" "${auth[@]}" -X POST "$URL/history/older"
status 403 "${pub[@]}" "${auth[@]}" -X POST -H "HX-Request: true" \
  -H "Origin: https://evil.example" "$URL/history/older"
status 200 "${pub[@]}" "${auth[@]}" -X POST -H "HX-Request: true" \
  -H "Origin: https://$PUBLIC" "$URL/history/older"
headers="$(curl -s -D - -o /dev/null "${pub[@]}" "${auth[@]}" "$URL/" | tr -d '\r' | tr '[:upper:]' '[:lower:]')"
for want in "content-security-policy: default-src 'none'" "x-frame-options: deny" \
            "x-content-type-options: nosniff" "cache-control: no-store" \
            "referrer-policy: same-origin"; do
  grep -qF -- "$want" <<<"$headers" || fail "missing header: $want"
done
grep -q '^server:' <<<"$headers" && fail "the server announces itself"
grep -q '^strict-transport-security:' <<<"$headers" && fail "HSTS is the proxy's to send"
say "ok: auth, Host policy, origin checks and headers through the published port"

# --------------------------------------------------------------- stopping

started=$SECONDS
compose stop >/dev/null
took=$((SECONDS - started))
code="$(docker inspect -f '{{.State.ExitCode}}' "$CONTAINER")"
case "$code" in 0|143) ;; *) fail "stopped with exit code $code" ;; esac
[ "$took" -lt 30 ] || fail "stopping took ${took}s: it was killed, not stopped"
compose logs --no-color | grep -q "Application shutdown complete" || fail "no clean shutdown in the log"
say "ok: stopped by SIGTERM in ${took}s, exit $code, shutdown complete"

logs="$(compose logs --no-color)"
for secret in "$KEY" "$PASSWORD" "$TOKEN"; do
  grep -qF -- "$secret" <<<"$logs" && fail "a secret is in the container log"
done
say "ok: no secret in the container log"

# -------------------------------------------------- backup and restore

mkdir -p "$WORK/backups"
sudo chown "$uid:$gid" "$WORK/backups" 2>/dev/null || chown "$uid:$gid" "$WORK/backups"
made="$(docker run --rm --volumes-from "$CONTAINER" -v "$WORK/backups:/backups" \
  -v "$ROOT/scripts:/ops:ro" --entrypoint python "$TAG" /ops/sqlite_backup.py backup \
  --source /data/blocktail.db --dest-dir /backups --label smoke --keep 5)"
[ -n "$made" ] && [ "$made" != "none" ] || fail "the backup made nothing"
docker run --rm -v "$WORK/backups:/backups:ro" -v "$ROOT/scripts:/ops:ro" \
  --entrypoint python "$TAG" /ops/sqlite_backup.py verify --path "/backups/$made" >/dev/null \
  || fail "the backup does not verify"
docker run --rm --volumes-from "$CONTAINER" -v "$WORK/backups:/backups:ro" \
  -v "$ROOT/scripts:/ops:ro" --entrypoint python "$TAG" /ops/sqlite_backup.py restore \
  --backup "/backups/$made" --target /data/blocktail.db >/dev/null \
  || fail "the restore failed"
compose start >/dev/null
wait_healthy
status 200 "${pub[@]}" "${auth[@]}" "$URL/"
say "ok: backup, verify and restore as the deploy does them; healthy again after"

say "passed"
