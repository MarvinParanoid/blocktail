#!/usr/bin/env bash
# Deploys and rollbacks with real Docker and the real scripts/remote-deploy.sh,
# on a directory laid out as on the server. CI runs both modes (the `image`
# job) against the release production runs; run it anywhere Docker and the
# compose plugin are installed:
#
#   scripts/rollback-smoke.sh [<previous release ref>]                         A–J
#   scripts/rollback-smoke.sh --after-failed-attempt [<previous release ref>]  the server today
#
# Default ref: origin/main. The previous release is built by its own
# Dockerfile under the tag its own docker-compose.yml names — a rebuild from
# the same commit, not production's image itself.
#
# A–J, on a legacy server (that compose file, no releases/, last-good or
# states/), with data only the database holds (a wallet added from the page):
#
#   A  the first deploy of HEAD over it (below, step H, after the failures);
#   –  without ALLOWED_HOSTS a deploy is refused and changes nothing;
#   I  a state that cannot be saved stops a deploy before anything moves;
#   B  a release whose compose file this Docker cannot run (a seccomp profile
#      denying exec — what snap Docker's AppArmor did with no-new-privileges)
#      is refused by the runtime check while the old version still runs;
#   C  a release that starts, migrates, and fails its health check;
#   D  the rollback puts back the old image AND the old compose files;
#   E  … with the volume and every wallet;
#   F  … and the schema the failed release left (no migration runs backwards);
#   G  a second rollback changes nothing;
#   J  a changed, incomplete or retagged state is refused as a target;
#   H  then HEAD deploys, rolls back to the baseline, and deploys again.
#
# --after-failed-attempt: the server as RC2b.1 left it — the baseline recorded
# as good but no states/, the failed release's image, its releases/ entry and
# its compose files set aside by hand, a backup and a log line, ALLOWED_HOSTS
# set, the legacy compose file back in place — then HEAD deploys, rolls back
# to the baseline with that legacy file, and deploys again.
#
# The server half runs as root (sudo when not root), as on the server. Nothing
# leaves the machine: the provider URL is a closed port, prices are off, every
# secret is a canary made up here. Everything it made is removed.
set -euo pipefail

MODE="a-to-j"
if [ "${1:-}" = "--after-failed-attempt" ]; then
  MODE="after-failed-attempt"
  shift
fi
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
BAD_EXEC="badexec00001"            # releases made here from HEAD, broken on purpose
BAD_HEALTH="badhealth001"
FAILED="25de5c1a751d"              # the release that failed in production
MADE_IMAGES=()
# An image of HEAD built before this ran — the release image CI ships — is
# the one deployed here (remote-deploy.sh never rebuilds a tag), and is kept.
RELEASE_ID="$(docker image inspect -f '{{.Id}}' "blocktail:$SHA" 2>/dev/null || true)"

say() { printf 'rollback-smoke[%s]: %s\n' "$MODE" "$*"; }
fail() { printf 'rollback-smoke[%s]: FAILED: %s\n' "$MODE" "$*" >&2; dump; exit 1; }
compose() { (cd "$REMOTE" && docker compose -p "$PROJECT" "$@"); }
dump() {
  docker logs --tail 60 "$CONTAINER" >&2 2>&1 || true
  [ -f "$REMOTE/deployments.log" ] && cat "$REMOTE/deployments.log" >&2 || true
}
cleanup() {
  compose down -v --remove-orphans >/dev/null 2>&1 || true
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  docker volume rm "${PROJECT}_blocktail-data" >/dev/null 2>&1 || true
  local made='^blocktail:(baseline-|'"$BAD_EXEC|$BAD_HEALTH|$FAILED"')'
  for image in "${MADE_IMAGES[@]}" $(docker image ls blocktail --format '{{.Repository}}:{{.Tag}}' \
      | grep -E "$made" || true); do
    docker rmi -f "$image" >/dev/null 2>&1 || true
  done
  rm -rf "$WORK" 2>/dev/null || sudo rm -rf "$WORK" 2>/dev/null || true
}
trap cleanup EXIT

# get/code <url> [curl args]: as a browser on the page would, through the
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
started_at() { docker inspect -f '{{.State.StartedAt}}' "$CONTAINER"; }
wait_healthy() {
  local state=""
  for _ in $(seq 1 75); do
    state="$(docker inspect -f '{{.State.Health.Status}}' "$CONTAINER" 2>/dev/null || true)"
    [ "$state" = "healthy" ] && return 0
    sleep 2
  done
  fail "the container did not become healthy (last state: ${state:-none})"
}
# The server half, from the release as deploy.sh ships it, as root.
AS_ROOT=()
[ "$(id -u)" = "0" ] || AS_ROOT=(sudo --preserve-env=PATH)
remote_deploy() {  # <args>
  "${AS_ROOT[@]}" env BLOCKTAIL_REMOTE="$REMOTE" BLOCKTAIL_HEALTH_TIMEOUT=45 \
    bash "$WORK/scripts/remote-deploy.sh" "$@"
}
ship() {  # <sha>: unpack HEAD as deploy.sh would, into a private incoming dir; print it
  local dir="$REMOTE/releases/.incoming-$1-smoke-$RANDOM"
  mkdir -p "$dir"
  git -C "$ROOT" archive HEAD | tar -x -C "$dir"
  printf '%s' "$dir"
}
schema() {
  docker exec "$CONTAINER" python -c "import sqlite3
c = sqlite3.connect('file:/data/blocktail.db?mode=ro', uri=True)
print(c.execute('SELECT max(version) FROM schema_migrations').fetchone()[0])"
}
no_errors_in_log() {  # <what> [secrets]: SQLite and filesystem errors; the key too if asked
  local log
  log="$(docker logs "$CONTAINER" 2>&1)"
  if grep -E 'sqlite3\.|OperationalError|IntegrityError|DatabaseError|Read-only file system|PermissionError|Permission denied' <<<"$log"; then
    fail "$1: errors in the log"
  fi
  if [ "${2:-}" = "secrets" ]; then
    grep -q -- "$KEY" <<<"$log" && fail "$1: a secret is in the log"
  fi
  return 0
}
hardened() {  # <what>: HEAD's compose — everything but no-new-privileges (docs/production.md)
  local got
  got="$(docker inspect -f '{{.HostConfig.ReadonlyRootfs}} {{.HostConfig.CapDrop}} {{.HostConfig.SecurityOpt}} {{.HostConfig.Memory}} {{.HostConfig.PidsLimit}}' "$CONTAINER")"
  [ "$got" = "true [ALL] [] 536870912 256" ] || fail "$1: runs as $got"
  docker exec "$CONTAINER" sh -c 'touch /srv/x' 2>/dev/null && fail "$1: the root filesystem is writable"
  docker exec "$CONTAINER" sh -c 'touch /tmp/x && rm /tmp/x' || fail "$1: /tmp is not writable"
  [ "$(docker exec "$CONTAINER" sh -c "awk '/^CapEff/ {print \$2}' /proc/1/status")" = "0000000000000000" ] \
    || fail "$1: has capabilities"
}
legacy_files() {  # the legacy compose file in place and no override
  cmp -s "$REMOTE/docker-compose.yml" "$WORK/previous/docker-compose.yml" \
    && [ ! -e "$REMOTE/docker-compose.override.yml" ]
}
runs_legacy() {  # <what>
  [ "$(running_id)" = "$legacy_id" ] || fail "$1: $LEGACY is not what runs"
  legacy_files || fail "$1: the legacy compose file is not the one in place"
}
deploy_head() {  # <log name>: deploys HEAD, which must succeed and use the prebuilt image
  remote_deploy deploy "$SHA" "$(ship "$SHA")" 2> "$WORK/$1.log" \
    || { cat "$WORK/$1.log" >&2; fail "the deploy of $SHA failed ($1)"; }
  grep -q "already built" "$WORK/$1.log" || fail "the deploy of $SHA rebuilt its image ($1)"
  [ "$(running_id)" = "$head_id" ] || fail "$SHA is not what runs ($1)"
  [ "$(cat "$REMOTE/last-good")" = "blocktail:$SHA $head_id" ] || fail "last-good ($1): $(cat "$REMOTE/last-good")"
  [ -f "$REMOTE/states/$SHA/complete" ] || fail "no state saved for $SHA ($1)"
  hardened "$SHA ($1)"
  no_errors_in_log "$SHA ($1)" secrets
}
rollback_to_baseline() {  # <log name>
  remote_deploy rollback "$baseline" 2> "$WORK/$1.log" \
    || { cat "$WORK/$1.log" >&2; fail "the rollback to $baseline failed ($1)"; }
  runs_legacy "rollback ($1)"
  [ "$(cut -d' ' -f1 "$REMOTE/last-good")" = "$baseline" ] || fail "last-good after rollback ($1): $(cat "$REMOTE/last-good")"
  wait_healthy
  for path in / /activity "/activity?q=page" /summary /wallets /status /api/status /api/portfolio; do
    [ "$(code "$URL$path")" = "200" ] || fail "the baseline ($1): GET $path answered $(code "$URL$path")"
  done
  no_errors_in_log "the baseline ($1)"
}

# ------------------------------------------------ production as it was

say "building the previous release ($BASE) by its own Dockerfile"
mkdir -p "$WORK/previous" "$REMOTE"
git -C "$ROOT" archive "$BASE" | tar -x -C "$WORK/previous"
LEGACY="$(sed -n 's/^[[:space:]]*image:[[:space:]]*//p' "$WORK/previous/docker-compose.yml" | head -n 1)"
[ -n "$LEGACY" ] || fail "$BASE's docker-compose.yml names no image"
docker image inspect "$LEGACY" >/dev/null 2>&1 && fail "$LEGACY already exists here; refusing to replace it"
docker build -q -t "$LEGACY" "$WORK/previous" >/dev/null
MADE_IMAGES+=("$LEGACY")
legacy_id="$(docker image inspect -f '{{.Id}}' "$LEGACY")"
if [ -z "$RELEASE_ID" ]; then
  say "building blocktail:$SHA (no prebuilt release image here)"
  mkdir -p "$WORK/head"
  git -C "$ROOT" archive HEAD | tar -x -C "$WORK/head"
  docker build -q --label "org.opencontainers.image.revision=$(git -C "$ROOT" rev-parse HEAD)" \
    -t "blocktail:$SHA" "$WORK/head" >/dev/null
  MADE_IMAGES+=("blocktail:$SHA")
fi
head_id="$(docker image inspect -f '{{.Id}}' "blocktail:$SHA")"
mkdir -p "$WORK/scripts"
git -C "$ROOT" archive HEAD scripts | tar -x -C "$WORK"      # the server half, as shipped

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

if [ "$MODE" = "after-failed-attempt" ]; then
  # ------------------------------------- the server as RC2b.1 left it
  echo "ALLOWED_HOSTS=$PUBLIC" >> "$REMOTE/.env"
  baseline="blocktail:baseline-$(printf '%s' "${legacy_id#sha256:}" | cut -c1-12)"
  docker tag "$legacy_id" "$baseline"
  docker tag "blocktail:$SHA" "blocktail:$FAILED"
  printf '%s %s\n' "$baseline" "$legacy_id" > "$REMOTE/last-good"
  mkdir -p "$REMOTE/releases/$FAILED" "$REMOTE/backups"
  git -C "$ROOT" archive HEAD | tar -x -C "$REMOTE/releases/$FAILED"
  cp "$REMOTE/releases/$FAILED/docker-compose.yml" "$REMOTE/docker-compose.yml.$FAILED"
  printf 'services:\n  blocktail:\n    image: blocktail:%s\n' "$FAILED" > "$REMOTE/docker-compose.override.yml.$FAILED"
  printf 'not a database: a placeholder for the backup taken then\n' \
    > "$REMOTE/backups/blocktail-20261116T213222Z-before-$FAILED-from-baseline-${baseline##*-}.db"
  printf '2026-11-16T21:33:59Z %s failed health check image=blocktail:%s last_good=%s\n' \
    "$FAILED" "$FAILED" "$baseline" > "$REMOTE/deployments.log"
  printf '2026-11-16T21:36:20Z manual restore: previous docker-compose.yml and %s\n' "$LEGACY" \
    >> "$REMOTE/deployments.log"
  [ ! -e "$REMOTE/states" ] || fail "precondition: no states/ yet"
  say "ok: the server as the failed attempt left it (last good $baseline, no states/)"

  deploy_head "after-attempt-deploy"
  [ -f "$REMOTE/states/${baseline#blocktail:}/complete" ] || fail "the baseline's state was not saved"
  cmp -s "$REMOTE/states/${baseline#blocktail:}/docker-compose.yml" "$WORK/previous/docker-compose.yml" \
    || fail "the baseline's state is not the legacy compose file"
  [ -f "$REMOTE/docker-compose.yml.$FAILED" ] && [ -f "$REMOTE/docker-compose.override.yml.$FAILED" ] \
    || fail "the set-aside files of the failed attempt were touched"
  docker image inspect "blocktail:$FAILED" >/dev/null || fail "the failed release's image was removed"
  [ "$(wallets)" = "Config From the page" ] || fail "after the deploy: $(wallets)"
  say "ok: deployed $SHA over the failed attempt's leftovers; the baseline saved with its legacy compose"

  rollback_to_baseline "after-attempt-rollback"
  [ "$(wallets)" = "Config From the page" ] || fail "after the rollback: $(wallets)"
  say "ok: rolled back to $baseline with its legacy compose file"
  deploy_head "after-attempt-forward"
  say "ok: and forward again"
  say "passed"
  exit 0
fi

# ----------------------------------------- no ALLOWED_HOSTS, no deploy

if out="$(remote_deploy deploy "$SHA" "$(ship "$SHA")" 2>&1)"; then
  fail "a deploy without ALLOWED_HOSTS went through: $out"
fi
grep -q ALLOWED_HOSTS <<<"$out" || fail "the refusal does not name ALLOWED_HOSTS: $out"
runs_legacy "refused without ALLOWED_HOSTS"
[ ! -e "$REMOTE/last-good" ] && [ ! -e "$REMOTE/backups" ] && [ ! -e "$REMOTE/states" ] \
  || fail "the refused deploy left a record, a backup or a state"
say "ok: without ALLOWED_HOSTS the deploy is refused and nothing changes"
echo "ALLOWED_HOSTS=$PUBLIC" >> "$REMOTE/.env"

# ------------------------------- I: the way back cannot be saved

printf 'not a directory\n' > "$REMOTE/states"
before="$(started_at)"
if out="$(remote_deploy deploy "$SHA" "$(ship "$SHA")" 2>&1)"; then
  fail "a deploy without a way back went through: $out"
fi
grep -q "could not save the state" <<<"$out" || fail "I: the refusal does not say why: $out"
runs_legacy "I"
[ "$(started_at)" = "$before" ] || fail "I: the running container was restarted"
[ ! -e "$REMOTE/backups" ] || fail "I: a backup was taken before the refusal"
baseline="$(sed -n 's/.*recorded as \(blocktail:baseline-[0-9a-f]*\).*/\1/p' <<<"$out")"
[ -n "$baseline" ] || fail "I: the running image was not adopted as the baseline: $out"
[ "$(docker image inspect -f '{{.Id}}' "$baseline")" = "$legacy_id" ] || fail "$baseline is not $LEGACY"
rm "$REMOTE/states"
say "ok: I — no state, no deploy: refused before the build, the backup and the switch"

# ----------------- B: a compose file this Docker cannot run the image under

cat > "$WORK/deny-exec.json" <<'EOF'
{"defaultAction": "SCMP_ACT_ALLOW",
 "syscalls": [{"names": ["execve", "execveat"], "action": "SCMP_ACT_ERRNO"}]}
EOF
docker tag "blocktail:$SHA" "blocktail:$BAD_EXEC"
incoming="$(ship "$BAD_EXEC")"
python3 - "$incoming/docker-compose.yml" "$WORK/deny-exec.json" <<'EOF'
import sys
path, profile = sys.argv[1], sys.argv[2]
text = open(path).read()
anchor = "    cap_drop: [ALL]\n"
assert text.count(anchor) == 1, "the release's compose file changed shape"
open(path, "w").write(text.replace(
    anchor, anchor + f"    security_opt:\n      - seccomp={profile}\n"))
EOF
before="$(started_at)"
if out="$(remote_deploy deploy "$BAD_EXEC" "$incoming" 2>&1)"; then
  fail "B: a release that cannot exec here went through: $out"
fi
grep -q "does not start under its compose restrictions" <<<"$out" || fail "B: not stopped by the runtime check: $out"
runs_legacy "B"
[ "$(started_at)" = "$before" ] || fail "B: the running container was restarted"
[ ! -e "$REMOTE/backups" ] || fail "B: a backup was taken before the refusal"
[ "$(cut -d' ' -f1 "$REMOTE/last-good")" = "$baseline" ] || fail "B: last-good changed"
[ -f "$REMOTE/states/${baseline#blocktail:}/complete" ] || fail "B: the way back was not saved first"
say "ok: B — the runtime check refused it while $LEGACY kept running: $(grep -m1 -i 'not permitted' <<<"$out" | sed 's/^ *//')"

# --------------------- C: starts, migrates, never passes its health check

docker tag "blocktail:$SHA" "blocktail:$BAD_HEALTH"
incoming="$(ship "$BAD_HEALTH")"
python3 - "$incoming/docker-compose.yml" <<'EOF'
import sys
path = sys.argv[1]
text = open(path).read()
anchor = "    container_name: blocktail\n"
assert text.count(anchor) == 1, "the release's compose file changed shape"
open(path, "w").write(text.replace(anchor, anchor + (
    '    command: ["sh", "-c", "exec uvicorn app.main:main --factory'
    ' --host 0.0.0.0 --port 8001 --workers 1"]\n')))
EOF
if out="$(remote_deploy deploy "$BAD_HEALTH" "$incoming" 2>&1)"; then
  fail "C: a release that never answers /healthz was recorded as good: $out"
fi
grep -q "did not become healthy" <<<"$out" || fail "C: not a health-check failure: $out"
grep -q "rollback $baseline" <<<"$out" || fail "C: the failure does not name the way back: $out"
[ "$(cut -d' ' -f1 "$REMOTE/last-good")" = "$baseline" ] || fail "C: last-good changed"
[ ! -e "$REMOTE/states/$BAD_HEALTH" ] || fail "C: a state was saved for a failed release"
failed_schema="$(schema)"
[ "$failed_schema" -ge "$legacy_schema" ] || fail "C: schema $failed_schema"
say "ok: C — health check failed after the switch; last good still $baseline; schema now $failed_schema"

# ----------------------------------- D, E, F: the rollback, and what it keeps

rollback_to_baseline "D"
[ "$(wallets)" = "Config From the page" ] || fail "E: wallets after the rollback: $(wallets)"
[ "$(schema)" = "$failed_schema" ] || fail "F: the rollback changed the schema ($failed_schema -> $(schema))"
kept="$(find "$REMOTE/attempts" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
grep -q "port 8001" "$kept/docker-compose.yml" || fail "D: the failed release's compose file was not kept"
[ -s "$kept/container.log" ] || fail "D: the failed container's log was not kept"
add_wallet "During the rollback" 4
[ "$(wallets)" = "Config During the rollback From the page" ] || fail "E: the baseline did not write: $(wallets)"
say "ok: D/E/F — $LEGACY with its own compose file; every wallet; schema $failed_schema kept; the failed files in ${kept#"$REMOTE"/}"

# --------------------------------------------------- G: once more, nothing

before="$(started_at)"
remote_deploy rollback "$baseline" 2> "$WORK/G.log" || { cat "$WORK/G.log" >&2; fail "G: the second rollback failed"; }
grep -q "already runs" "$WORK/G.log" || fail "G: the second rollback did something: $(cat "$WORK/G.log")"
[ "$(started_at)" = "$before" ] || fail "G: the second rollback restarted the container"
runs_legacy "G"
say "ok: G — a second rollback changes nothing"

# ---------------------------------- J: a damaged state is not a target

state="$REMOTE/states/${baseline#blocktail:}"
cp -a "$state" "$WORK/state.saved"
for damage in edited incomplete; do
  if [ "$damage" = edited ]; then
    "${AS_ROOT[@]}" sh -c "printf '# changed by hand\n' >> '$state/docker-compose.yml'"
  else
    "${AS_ROOT[@]}" rm "$state/complete"
  fi
  before="$(started_at)"
  if out="$(remote_deploy rollback "$baseline" 2>&1)"; then
    fail "J ($damage): a damaged state was accepted: $out"
  fi
  grep -q "is not a rollback target" <<<"$out" || fail "J ($damage): $out"
  [ "$(started_at)" = "$before" ] || fail "J ($damage): the container was restarted"
  runs_legacy "J ($damage)"
  "${AS_ROOT[@]}" rm -rf "$state"
  "${AS_ROOT[@]}" cp -a "$WORK/state.saved" "$state"
  say "ok: J — a state that is $damage is refused and nothing changes"
done

# ------------------- H, A: HEAD deploys, rolls back, and deploys again

deploy_head "H"
[ "$(wallets)" = "Config During the rollback From the page" ] || fail "H: wallets: $(wallets)"
add_wallet "After the deploy" 3
say "ok: H — deployed $SHA over $baseline; wallets kept; read-only, no capabilities, no NNP"
rollback_to_baseline "H-back"
[ "$(wallets)" = "After the deploy Config During the rollback From the page" ] || fail "H: wallets after rollback: $(wallets)"
say "ok: rolled back from a good $SHA to $baseline with its legacy compose"
deploy_head "H-forward"
[ "$(wallets)" = "After the deploy Config During the rollback From the page" ] || fail "H: wallets: $(wallets)"
say "ok: deployed $SHA again; every wallet kept"
say "passed"
