#!/usr/bin/env bash
# The server half of a deploy. scripts/deploy.sh unpacks a release made with
# `git archive` into a private directory under $BLOCKTAIL_REMOTE/releases/ and
# runs this from it.
#
#   remote-deploy.sh deploy <sha> [<unpacked dir>]   build, back up, start, verify
#   remote-deploy.sh rollback [<image tag>]          start an image already here;
#                                                    default: the last good one
#
# Layout under $BLOCKTAIL_REMOTE (the directory compose has always run in, so
# the project name — and with it the data volume — does not change):
#
#   .env, wallets.yml            yours; never shipped, never touched
#   docker-compose.yml           copied from the release being deployed
#   docker-compose.override.yml  written here: pins the image tag that runs
#   last-good                    "<tag> <image id>" of the last version that
#                                passed its health check; written on success only
#   releases/<sha>/              what was shipped; the last five are kept
#   backups/                     SQLite backups; five, plus the newest one taken
#                                from the last good version
#   deployments.log              one line per attempt
#   .deploy.lock                 held for the whole of a deploy or rollback
#
# Images are tagged blocktail:<sha> and a tag, once built, is never built
# again: the same commit is the same code, so a retry reuses the image, and a
# tag recorded as good keeps meaning the image that was good.
#
# What it will not do is restore the database on its own. A failed start
# after a migration may leave a schema the previous code does not understand,
# and only a person can decide whether to restore a backup and lose what was
# indexed since. It stops, says what happened, and prints the commands.
set -euo pipefail

DOCKER="${DOCKER:-docker}"   # the test suite substitutes a double; nothing else should
REMOTE="${BLOCKTAIL_REMOTE:?BLOCKTAIL_REMOTE is not set}"
CONTAINER="${BLOCKTAIL_CONTAINER:-blocktail}"
HEALTH_TIMEOUT="${BLOCKTAIL_HEALTH_TIMEOUT:-90}"
HEALTH_INTERVAL="${BLOCKTAIL_HEALTH_INTERVAL:-2}"
KEEP=5
IMAGE_REPO="blocktail"
LOCK_FILE="$REMOTE/.deploy.lock"
LAST_GOOD="$REMOTE/last-good"
SHA=""
RELEASE=""

say() { printf 'deploy: %s\n' "$*" >&2; }
die() { printf 'deploy: FAILED: %s\n' "$*" >&2; exit 1; }

# ------------------------------------------------------------------- lock
#
# One deploy or rollback at a time, for the whole of it: moving the release
# into place, build, backup, compose up, health check, and the record of what
# is good. A second one is refused at once and changes nothing — the only
# thing it removes is the directory its own session unpacked.
#
# flock ties the lock to an open file, so the kernel releases it when the
# process ends, however it ends. Without flock, a lock directory stands in,
# holding the owner's pid; it counts as held only while that pid is alive
# and is running this script, so a crashed run cannot block the next one.

lock_owner() { cat "$LOCK_FILE.owner" 2>/dev/null || echo "owner unknown"; }

refuse_busy() {  # <path this session unpacked, or empty>
  [ -n "$1" ] && rm -rf -- "$1"
  die "another deploy or rollback is running ($(lock_owner)); nothing was changed"
}

acquire_lock() {  # <what> [<path to remove if refused>]
  local what="$1" mine="${2:-}"
  mkdir -p "$REMOTE"
  if command -v flock >/dev/null 2>&1 && [ -z "${BLOCKTAIL_LOCK_FALLBACK:-}" ]; then
    exec 9>>"$LOCK_FILE"
    flock -n 9 || refuse_busy "$mine"
  else
    local dir="$LOCK_FILE.d" pid
    if ! mkdir "$dir" 2>/dev/null; then
      pid="$(cat "$dir/pid" 2>/dev/null || true)"
      if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null \
         && tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q 'remote-deploy.sh'; then
        refuse_busy "$mine"
      fi
      say "removing a stale lock left by pid ${pid:-?}, which is no longer running"
      rm -rf "$dir"
      mkdir "$dir" 2>/dev/null || refuse_busy "$mine"
    fi
    printf '%s\n' "$$" > "$dir/pid"
    trap 'rm -rf "$LOCK_FILE.d"' EXIT
  fi
  printf '%s pid %s: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$$" "$what" > "$LOCK_FILE.owner"
}

# ------------------------------------------------------------- preflight

# docker-compose.yml requires ALLOWED_HOSTS (S8b) and compose would refuse
# to start without it — after the build and the backup, with an error about
# interpolation. Asked here first, so the answer is plain and nothing moves.
require_allowed_hosts() {
  grep -Eq '^[[:space:]]*(export[[:space:]]+)?ALLOWED_HOSTS=[[:space:]]*[^[:space:]#]' "$REMOTE/.env" \
    || die "ALLOWED_HOSTS is not set in $REMOTE/.env — set it to the name(s) this instance is" \
           "served as (e.g. ALLOWED_HOSTS=watch.example.com; docs/production.md); nothing was changed"
}

# ------------------------------------------------------------------ state

container_exists() { "$DOCKER" inspect --type container "$CONTAINER" >/dev/null 2>&1; }
image_id() { "$DOCKER" image inspect -f '{{.Id}}' "$1" 2>/dev/null; }

# Compose names the data volume after the project, and the project after the
# directory unless told otherwise. Ask the running container rather than guess.
project_name() {
  local name=""
  if container_exists; then
    name="$("$DOCKER" inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$CONTAINER" 2>/dev/null || true)"
  fi
  [ -n "$name" ] && [ "$name" != "<no value>" ] || name="$(basename "$REMOTE")"
  printf '%s' "$name"
}

compose() { (cd "$REMOTE" && "$DOCKER" compose -p "$PROJECT" "$@"); }

last_good_tag() { if [ -f "$LAST_GOOD" ]; then cut -d' ' -f1 "$LAST_GOOD"; fi; }
last_good_id() { if [ -f "$LAST_GOOD" ]; then cut -d' ' -f2 "$LAST_GOOD"; fi; }

record_good() {  # <tag> <image id>; only ever after a passed health check
  printf '%s %s\n' "$1" "$2" > "$LAST_GOOD.new"
  mv "$LAST_GOOD.new" "$LAST_GOOD"
}

# A version is named in backup labels by its commit when it has one.
version_of() { printf '%s' "${1#"$IMAGE_REPO":}"; }

# Before the first deploy made by this script there is no record of what is
# good. What is running then is production, deployed by hand or by the old
# script: give its image an immutable name and treat it as the baseline.
adopt_running_as_good() {
  [ -f "$LAST_GOOD" ] && return 0
  container_exists || return 0
  local id ref short tag
  id="$("$DOCKER" inspect -f '{{.Image}}' "$CONTAINER")"
  ref="$("$DOCKER" inspect -f '{{.Config.Image}}' "$CONTAINER")"
  short="${id#sha256:}"
  tag="$IMAGE_REPO:baseline-${short:0:12}"
  "$DOCKER" tag "$id" "$tag"
  record_good "$tag" "$id"
  say "no record of a good version yet: the running $ref is recorded as $tag"
}

pin_image() {
  # The override is what makes a plain `docker compose up -d` on the server
  # run the deployed image, not whatever `build: .` would make of this folder.
  cat > "$REMOTE/docker-compose.override.yml.new" <<EOF
# Written by scripts/remote-deploy.sh. Pins the image that runs; do not edit.
services:
  blocktail:
    image: $1
EOF
  mv "$REMOTE/docker-compose.override.yml.new" "$REMOTE/docker-compose.override.yml"
}

log_line() {
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >> "$REMOTE/deployments.log"
}

# Liveness of the container we just started, from the image we expect.
# HTTP 200 here means the web process is up — not that the provider is
# reachable or the index current; /healthz keeps that distinction on purpose.
wait_healthy() {
  local expected_id="$1" deadline=$((SECONDS + HEALTH_TIMEOUT)) running_id state
  while [ "$SECONDS" -lt "$deadline" ]; do
    running_id="$("$DOCKER" inspect -f '{{.Image}}' "$CONTAINER" 2>/dev/null || true)"
    state="$("$DOCKER" inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || true)"
    if [ "$running_id" = "$expected_id" ] && [ "$state" = "running" ] \
       && "$DOCKER" exec "$CONTAINER" python -c 'import os, sys, urllib.request
r = urllib.request.urlopen("http://127.0.0.1:%s/healthz" % os.environ.get("PORT", "8000"), timeout=4)
sys.stdout.write(r.read().decode()); sys.exit(0 if r.status == 200 else 1)' > "$REMOTE/.last-health" 2>/dev/null; then
      return 0
    fi
    sleep "$HEALTH_INTERVAL"
  done
  return 1
}

show_logs() {
  say "container state: $("$DOCKER" inspect -f '{{.State.Status}} (restarts: {{.RestartCount}}, image {{.Config.Image}})' "$CONTAINER" 2>/dev/null || echo 'not found')"
  say "last log lines:"
  "$DOCKER" logs --tail 80 "$CONTAINER" 2>&1 | sed 's/^/    /' >&2 || true
}

report_failure() {  # <new tag> <last good tag or empty> <backup from it or empty>
  local tag="$1" good="$2" backup="${3:-none on this host}"
  say "the new version did not become healthy within ${HEALTH_TIMEOUT}s"
  show_logs
  if [ -z "$good" ]; then
    cat >&2 <<EOF

deploy: Nothing was rolled back automatically, and no earlier version is
deploy: recorded as good on this host. Fix forward, or see docs/operations.md,
deploy: "When a new version does not start".
EOF
    return
  fi
  cat >&2 <<EOF

deploy: Nothing was rolled back automatically. The database may already carry
deploy: migrations from $tag. To go back:
deploy:
deploy:   1. Start the last good version (the database is left as it is):
deploy:        BLOCKTAIL_REMOTE='$REMOTE' bash '$REMOTE/releases/$SHA/scripts/remote-deploy.sh' rollback $good
deploy:   2. If that version cannot read the database, restore the newest backup
deploy:      taken while it was running ($backup) — see docs/operations.md,
deploy:      "Restoring the database". Everything indexed since then is fetched again.
EOF
}

prune_releases() {
  # Newest first by when they were deployed (install_release touches the one
  # it installs), never the one now running: a re-deploy of an old commit is new.
  local old
  # shellcheck disable=SC2012
  ls -1dt "$REMOTE"/releases/*/ 2>/dev/null | sed 's:/$::' | tail -n +$((KEEP + 1)) \
    | while read -r old; do
        [ "$old" = "$RELEASE" ] || rm -rf -- "$old"
      done
  # Unpacked by a session that never got as far as the lock, an hour ago or more.
  find "$REMOTE/releases" -maxdepth 1 -name '.incoming-*' -mmin +60 -exec rm -rf {} + \
    2>/dev/null || true
}

# ------------------------------------------------------------------ backup

backup_database() {  # <image> <label> <protect substring or empty>
  local image="$1" label="$2" protect="$3" mounts=() db_path="" extra=()
  if container_exists; then
    mounts=(--volumes-from "$CONTAINER")
    db_path="$("$DOCKER" inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$CONTAINER" \
               | sed -n 's/^BLOCKTAIL_DB=//p' | tail -n 1)"
  elif "$DOCKER" volume inspect "${PROJECT}_blocktail-data" >/dev/null 2>&1; then
    mounts=(-v "${PROJECT}_blocktail-data:/data")
  else
    say "no container and no data volume: a first deployment, nothing to back up"
    BACKUP_NAME="none"
    return 0
  fi
  db_path="${db_path:-/data/blocktail.db}"
  # "=" form: the value starts with "-", which argparse would read as an option.
  [ -z "$protect" ] || extra=("--protect=$protect")

  # The backup runs as the app's own user, so nothing it touches in the data
  # volume ends up owned by root and unreadable to the app afterwards.
  local uid gid
  uid="$("$DOCKER" run --rm --entrypoint id "$image" -u)"
  gid="$("$DOCKER" run --rm --entrypoint id "$image" -g)"
  mkdir -p "$REMOTE/backups"
  chown "$uid:$gid" "$REMOTE/backups"

  say "backing up $db_path with SQLite's online backup API"
  BACKUP_NAME="$("$DOCKER" run --rm "${mounts[@]}" \
      -v "$REMOTE/backups:/backups" \
      -v "$RELEASE/scripts:/ops:ro" \
      --entrypoint python "$image" /ops/sqlite_backup.py backup \
      --source "$db_path" --dest-dir /backups --label "$label" --keep "$KEEP" \
      "${extra[@]}" --missing-ok)" \
    || return 1
  [ -n "$BACKUP_NAME" ] || return 1
}

newest_backup_from() {  # <version>
  # shellcheck disable=SC2012
  ls -1 "$REMOTE/backups" 2>/dev/null | grep -F -- "-from-$1.db" | sort | tail -n 1 || true
}

# ------------------------------------------------------------------ deploy

install_release() {  # under the lock: put the unpacked tree at releases/<sha>
  local incoming="$1"
  RELEASE="$REMOTE/releases/$SHA"
  if [ -n "$incoming" ] && [ "$incoming" != "$RELEASE" ]; then
    rm -rf -- "$RELEASE"
    mv -- "$incoming" "$RELEASE"
  fi
  touch "$RELEASE"   # tar restored the commit's mtime; this is when it was deployed
}

deploy() {
  SHA="${1:?usage: remote-deploy.sh deploy <sha> [<unpacked dir>]}"
  local incoming="${2:-}" tag good running="none" new_id protect="" from_good=""
  tag="$IMAGE_REPO:$SHA"

  acquire_lock "deploy $SHA" "$incoming"
  install_release "$incoming"

  [ -f "$RELEASE/Dockerfile" ] && [ -f "$RELEASE/docker-compose.yml" ] \
    || die "$RELEASE is not a complete release"
  [ -f "$REMOTE/.env" ] || die "$REMOTE/.env is missing (it stays on the server and is never shipped)"
  [ -f "$REMOTE/wallets.yml" ] || die "$REMOTE/wallets.yml is missing or not a file"
  require_allowed_hosts
  PROJECT="$(project_name)"
  say "release $SHA, compose project '$PROJECT'"

  adopt_running_as_good
  good="$(last_good_tag)"
  if container_exists; then
    running="$("$DOCKER" inspect -f '{{.Config.Image}}' "$CONTAINER")"
    # Name it the way the record does when it is the same image, so the
    # backup is recognised as one taken from the last good version.
    if [ -n "$good" ] && [ "$("$DOCKER" inspect -f '{{.Image}}' "$CONTAINER")" = "$(last_good_id)" ]; then
      running="$good"
    fi
  fi
  say "running: $running; last good: ${good:-none}"

  if new_id="$(image_id "$tag")"; then
    say "$tag is already built; not rebuilding — a commit's image is immutable here"
  else
    say "building $tag"
    "$DOCKER" build --label "org.opencontainers.image.revision=$SHA" -t "$tag" "$RELEASE" >&2 \
      || die "image build failed; the running version is untouched"
    new_id="$(image_id "$tag")"
  fi

  # Named after what is running, and the newest one taken from the last good
  # version is never rotated out: retries of a failed deploy back up the
  # failed version, and must not push the useful backup off the end.
  BACKUP_NAME=""
  [ -z "$good" ] || protect="-from-$(version_of "$good").db"
  if ! backup_database "$tag" "before-$SHA-from-$(version_of "$running")" "$protect"; then
    log_line "$SHA aborted: backup failed"
    die "database backup failed; the running version is untouched"
  fi
  say "backup: $BACKUP_NAME"

  cp "$RELEASE/docker-compose.yml" "$REMOTE/docker-compose.yml"
  pin_image "$tag"
  say "starting $tag"
  compose up -d --no-build --force-recreate >&2 || true

  if wait_healthy "$new_id"; then
    record_good "$tag" "$new_id"
    log_line "$SHA ok image=$tag id=$new_id previous_good=${good:-none} backup=$BACKUP_NAME"
    prune_releases
    say "ok: $tag is running and answers /healthz: $(cat "$REMOTE/.last-health" 2>/dev/null)"
    say "/healthz is liveness only. Whether the provider is reachable and the index"
    say "current is on the page and in /api/status — look there before walking away."
    return 0
  fi

  # The record of what is good is left exactly as it was.
  log_line "$SHA failed health check image=$tag last_good=${good:-none} backup=$BACKUP_NAME"
  if [ -n "$good" ]; then
    from_good="$(newest_backup_from "$(version_of "$good")")"
  fi
  report_failure "$tag" "$good" "${from_good:+$REMOTE/backups/$from_good}"
  exit 1
}

# ---------------------------------------------------------------- rollback

rollback() {  # [<image tag>]
  acquire_lock "rollback ${1:-to the last good version}"
  local tag id recorded
  tag="${1:-$(last_good_tag)}"
  [ -n "$tag" ] || die "no version is recorded as good on this host; name an image tag"
  id="$(image_id "$tag")" || die "no image $tag on this host (docker image ls $IMAGE_REPO)"
  [ -f "$REMOTE/.env" ] || die "$REMOTE/.env is missing"
  require_allowed_hosts
  recorded="$(last_good_id)"
  if [ "$tag" = "$(last_good_tag)" ] && [ "$id" != "$recorded" ]; then
    die "$tag now points at $id, not at $recorded which passed its health check" \
        "(rebuilt or retagged by hand); name the image you mean explicitly"
  fi
  PROJECT="$(project_name)"
  say "rolling back to $tag ($id); the database is NOT restored"
  pin_image "$tag"
  compose up -d --no-build --force-recreate >&2 || true
  if wait_healthy "$id"; then
    record_good "$tag" "$id"
    log_line "rollback ok image=$tag id=$id"
    say "ok: $tag is running. If it cannot read the database, see docs/operations.md."
    return 0
  fi
  log_line "rollback failed image=$tag"
  say "rollback to $tag did not become healthy"
  show_logs
  exit 1
}

case "${1:-}" in
  deploy) shift; deploy "$@" ;;
  rollback) shift; rollback "$@" ;;
  *) die "usage: remote-deploy.sh deploy <sha> [<unpacked dir>] | rollback [<image tag>]" ;;
esac
