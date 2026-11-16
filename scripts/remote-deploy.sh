#!/usr/bin/env bash
# The server half of a deploy. scripts/deploy.sh ships a release made with
# `git archive` into $BLOCKTAIL_REMOTE/releases/<sha>/ and runs this from it.
#
#   remote-deploy.sh deploy <sha>      build, back up, start, verify
#   remote-deploy.sh rollback <tag>    start an image that is already here
#
# Layout under $BLOCKTAIL_REMOTE (the directory compose has always run in, so
# the project name — and with it the data volume — does not change):
#
#   .env, wallets.yml            yours; never shipped, never touched
#   docker-compose.yml           copied from the release being deployed
#   docker-compose.override.yml  written here: pins the image tag that runs
#   releases/<sha>/              what was shipped; the last five are kept
#   backups/                     SQLite backups; the last five are kept
#   deployments.log              one line per attempt
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
KEEP=5
IMAGE_REPO="blocktail"

say() { printf 'deploy: %s\n' "$*" >&2; }
die() { printf 'deploy: FAILED: %s\n' "$*" >&2; exit 1; }

container_exists() { "$DOCKER" inspect --type container "$CONTAINER" >/dev/null 2>&1; }

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

# Liveness of the container we just started, from the image we just built.
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
    sleep 2
  done
  return 1
}

report_failure() {
  local tag="$1" previous="$2" backup="$3"
  say "the new version did not become healthy within ${HEALTH_TIMEOUT}s"
  say "container state: $("$DOCKER" inspect -f '{{.State.Status}} (restarts: {{.RestartCount}}, image {{.Config.Image}})' "$CONTAINER" 2>/dev/null || echo 'not found')"
  say "last log lines:"
  "$DOCKER" logs --tail 80 "$CONTAINER" 2>&1 | sed 's/^/    /' >&2 || true
  cat >&2 <<EOF

deploy: Nothing was rolled back automatically. The database may already carry
deploy: migrations from $tag. To go back:
deploy:
deploy:   1. Start the previous image (the database is left as it is):
deploy:        BLOCKTAIL_REMOTE='$REMOTE' bash '$REMOTE/releases/$SHA/scripts/remote-deploy.sh' rollback $previous
deploy:   2. If the previous version cannot read the database, restore the backup
deploy:      taken before this deploy ($backup) — see docs/operations.md,
deploy:      "Restoring the database". Everything indexed since then is fetched again.
EOF
}

prune_releases() {
  # Newest first by when they were deployed (deploy() touches the release it
  # runs), never the one now running: a re-deploy of an old commit is new.
  local old
  # shellcheck disable=SC2012
  ls -1dt "$REMOTE"/releases/*/ 2>/dev/null | sed 's:/$::' | tail -n +$((KEEP + 1)) \
    | while read -r old; do
        [ "$old" = "$RELEASE" ] || rm -rf -- "$old"
      done
}

# ------------------------------------------------------------------ backup

backup_database() {
  local image="$1" label="$2" mounts=() db_path=""
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
      --source "$db_path" --dest-dir /backups --label "$label" --keep "$KEEP" --missing-ok)" \
    || return 1
  [ -n "$BACKUP_NAME" ] || return 1
}

# ------------------------------------------------------------------ deploy

deploy() {
  SHA="${1:?usage: remote-deploy.sh deploy <sha>}"
  RELEASE="$REMOTE/releases/$SHA"
  local tag="$IMAGE_REPO:$SHA" previous="(none)" new_id

  [ -f "$RELEASE/Dockerfile" ] && [ -f "$RELEASE/docker-compose.yml" ] \
    || die "$RELEASE is not a complete release"
  [ -f "$REMOTE/.env" ] || die "$REMOTE/.env is missing (it stays on the server and is never shipped)"
  [ -f "$REMOTE/wallets.yml" ] || die "$REMOTE/wallets.yml is missing or not a file"
  touch "$RELEASE"   # tar restored the commit's mtime; this is when it was deployed
  PROJECT="$(project_name)"
  say "release $SHA, compose project '$PROJECT'"

  say "building $tag"
  "$DOCKER" build --label "org.opencontainers.image.revision=$SHA" -t "$tag" "$RELEASE" >&2 \
    || die "image build failed; the running version is untouched"
  new_id="$("$DOCKER" image inspect -f '{{.Id}}' "$tag")"

  if container_exists; then
    # Whatever runs now stays reachable by name, however it was tagged.
    previous="$("$DOCKER" inspect -f '{{.Config.Image}}' "$CONTAINER")"
    "$DOCKER" tag "$("$DOCKER" inspect -f '{{.Image}}' "$CONTAINER")" "$IMAGE_REPO:previous"
    say "currently running: $previous (also tagged $IMAGE_REPO:previous)"
  fi

  BACKUP_NAME=""
  if ! backup_database "$tag" "before-$SHA"; then
    log_line "$SHA aborted: backup failed"
    die "database backup failed; the running version is untouched"
  fi
  say "backup: $BACKUP_NAME"

  cp "$RELEASE/docker-compose.yml" "$REMOTE/docker-compose.yml"
  pin_image "$tag"
  say "starting $tag"
  compose up -d --no-build --force-recreate >&2 || true

  if wait_healthy "$new_id"; then
    log_line "$SHA ok image=$tag previous=$previous backup=$BACKUP_NAME"
    prune_releases
    say "ok: $tag is running and answers /healthz: $(cat "$REMOTE/.last-health" 2>/dev/null)"
    say "/healthz is liveness only. Whether the provider is reachable and the index"
    say "current is on the page and in /api/status — look there before walking away."
    return 0
  fi
  log_line "$SHA failed health check image=$tag previous=$previous backup=$BACKUP_NAME"
  local backup_path="$REMOTE/backups/$BACKUP_NAME"
  [ "$BACKUP_NAME" != "none" ] || backup_path="none — there was no database before this deploy"
  report_failure "$tag" "$IMAGE_REPO:previous" "$backup_path"
  exit 1
}

# ---------------------------------------------------------------- rollback

rollback() {
  local tag="${1:?usage: remote-deploy.sh rollback <image tag>}" id
  id="$("$DOCKER" image inspect -f '{{.Id}}' "$tag" 2>/dev/null)" \
    || die "no image $tag on this host (docker image ls $IMAGE_REPO)"
  PROJECT="$(project_name)"
  say "rolling back to $tag; the database is NOT restored"
  pin_image "$tag"
  compose up -d --no-build --force-recreate >&2 || true
  if wait_healthy "$id"; then
    log_line "rollback ok image=$tag"
    say "ok: $tag is running. If it cannot read the database, see docs/operations.md."
    return 0
  fi
  log_line "rollback failed image=$tag"
  say "rollback to $tag did not become healthy; last log lines:"
  "$DOCKER" logs --tail 80 "$CONTAINER" 2>&1 | sed 's/^/    /' >&2 || true
  exit 1
}

case "${1:-}" in
  deploy) shift; deploy "$@" ;;
  rollback) shift; SHA="${SHA:-unknown}"; rollback "$@" ;;
  *) die "usage: remote-deploy.sh deploy <sha> | rollback <image tag>" ;;
esac
