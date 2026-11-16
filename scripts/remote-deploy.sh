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
#   states/<version>/            how that version ran: its docker-compose.yml,
#                                its override if it had one, "<tag> <image id>",
#                                SHA256SUMS, and "complete", written last. What
#                                a rollback puts back — never the image alone
#   attempts/<time>-<what>/      the files a rollback replaced, and the failed
#                                container's state and log, kept for diagnosis
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

# ------------------------------------------------------------ the way back
#
# An image is not a version: blocktail:0.1.0 under the compose file that
# replaced its own does not start on a host whose Docker refuses that file's
# settings (RC2b.1). So a version is kept as the pair it ran as — the image
# and the compose files — in states/<version>/, and a rollback restores the
# pair. A state is written before anything moves (for what is good now) and
# after a health check passes (for the new version); never by a failed one.

STATES="$REMOTE/states"
state_dir() { printf '%s/%s' "$STATES" "$(version_of "$1")"; }

# save_state <tag> <image id>: the compose files in $REMOTE now, as that version's.
save_state() {
  local tag="$1" id="$2" dir tmp files
  dir="$(state_dir "$tag")"
  tmp="$STATES/.new-$(version_of "$tag")-$$"
  mkdir -p "$STATES" && rm -rf -- "$tmp" && mkdir "$tmp" || return 1
  cp -p "$REMOTE/docker-compose.yml" "$tmp/docker-compose.yml" || return 1
  files="docker-compose.yml image"
  if [ -f "$REMOTE/docker-compose.override.yml" ]; then
    cp -p "$REMOTE/docker-compose.override.yml" "$tmp/docker-compose.override.yml" || return 1
    files="$files docker-compose.override.yml"
  fi
  printf '%s %s\n' "$tag" "$id" > "$tmp/image" || return 1
  # shellcheck disable=SC2086  # the names are fixed, without spaces
  (cd "$tmp" && sha256sum $files > SHA256SUMS) || return 1
  printf 'complete\n' > "$tmp/complete" || return 1
  # Swapped in whole: a reader sees the old state or the new one, never half.
  if [ -e "$dir" ]; then
    rm -rf -- "$dir.replaced" && mv -- "$dir" "$dir.replaced" || return 1
  fi
  mv -- "$tmp" "$dir" || return 1
  rm -rf -- "$dir.replaced"
}

# check_state <tag>: a state is a rollback target only if it is complete,
# intact, names this tag, and the tag still points at the image it names.
# Sets STATE_PROBLEM when it is not.
STATE_PROBLEM=""
check_state() {
  local tag="$1" dir recorded_tag recorded_id listed
  dir="$(state_dir "$tag")"
  STATE_PROBLEM=""
  [ -d "$dir" ] || { STATE_PROBLEM="there is no saved state for $tag ($dir)"; return 1; }
  [ -f "$dir/complete" ] || { STATE_PROBLEM="the state in $dir is incomplete (no 'complete' marker)"; return 1; }
  [ -f "$dir/SHA256SUMS" ] && [ -f "$dir/docker-compose.yml" ] && [ -f "$dir/image" ] \
    || { STATE_PROBLEM="the state in $dir is missing files"; return 1; }
  listed="$(awk '{print $2}' "$dir/SHA256SUMS" | sort | tr '\n' ' ')"
  if [ -f "$dir/docker-compose.override.yml" ]; then
    [ "$listed" = "docker-compose.override.yml docker-compose.yml image " ] \
      || { STATE_PROBLEM="the state in $dir lists other files than it holds"; return 1; }
  else
    [ "$listed" = "docker-compose.yml image " ] \
      || { STATE_PROBLEM="the state in $dir lists other files than it holds"; return 1; }
  fi
  (cd "$dir" && sha256sum --quiet --strict -c SHA256SUMS >/dev/null 2>&1) \
    || { STATE_PROBLEM="the state in $dir does not match its checksums (changed or damaged)"; return 1; }
  read -r recorded_tag recorded_id < "$dir/image"
  [ "$recorded_tag" = "$tag" ] \
    || { STATE_PROBLEM="the state in $dir is for $recorded_tag, not $tag"; return 1; }
  [ "$(image_id "$tag" || true)" = "$recorded_id" ] \
    || { STATE_PROBLEM="$tag no longer points at $recorded_id, the image that state ran"; return 1; }
}

# The last good version must have a state before anything moves. Kept if it
# has a valid one; taken from what runs now if that is the last good image;
# otherwise there is no reliable way back, and the deploy does not start.
secure_the_way_back() {  # <last good tag>
  local good="$1" running_id
  [ -n "$good" ] || return 0
  if check_state "$good"; then
    say "the way back: $good, as saved in $(state_dir "$good")"
    return 0
  fi
  running_id="$("$DOCKER" inspect -f '{{.Image}}' "$CONTAINER" 2>/dev/null || true)"
  if [ "$running_id" != "$(last_good_id)" ]; then
    die "$STATE_PROBLEM, and what runs now is not $good, so its compose files cannot" \
        "be saved in its place; there would be no reliable way back. Nothing was changed."
  fi
  save_state "$good" "$running_id" \
    || die "could not save the state of $good in $(state_dir "$good"); without it there is" \
           "no way back, so nothing was changed"
  check_state "$good" || die "the state just saved for $good does not verify: $STATE_PROBLEM; nothing was changed"
  say "the way back: $good, saved now from what runs (docker-compose.yml$( \
      [ -f "$REMOTE/docker-compose.override.yml" ] && printf ' and its override'))"
}

# Before a rollback replaces the compose files, keep them and the failed
# container's state and log for diagnosis. Nothing here is deleted later by
# this script.
keep_attempt() {  # <what>: prints the directory; fails if the files could not be kept
  local dir
  dir="$REMOTE/attempts/$(date -u +%Y%m%dT%H%M%SZ)-$1"
  mkdir -p "$dir" || return 1
  if [ -f "$REMOTE/docker-compose.yml" ]; then
    cp -p "$REMOTE/docker-compose.yml" "$dir/" || return 1
  fi
  if [ -f "$REMOTE/docker-compose.override.yml" ]; then
    cp -p "$REMOTE/docker-compose.override.yml" "$dir/" || return 1
  fi
  # The container may be gone or never have started: then these say so.
  "$DOCKER" inspect -f '{{.Config.Image}} {{.Image}} {{.State.Status}} restarts={{.RestartCount}} started={{.State.StartedAt}}' \
    "$CONTAINER" > "$dir/container.txt" 2>&1 || echo "(no container to inspect)" >> "$dir/container.txt"
  "$DOCKER" logs --tail 200 "$CONTAINER" > "$dir/container.log" 2>&1 || echo "(no log)" >> "$dir/container.log"
  printf '%s' "$dir"
}

install_state_files() {  # <state dir>: its compose files become $REMOTE's
  local dir="$1"
  cp -p "$dir/docker-compose.yml" "$REMOTE/docker-compose.yml.restore" \
    && mv "$REMOTE/docker-compose.yml.restore" "$REMOTE/docker-compose.yml" || return 1
  if [ -f "$dir/docker-compose.override.yml" ]; then
    cp -p "$dir/docker-compose.override.yml" "$REMOTE/docker-compose.override.yml.restore" \
      && mv "$REMOTE/docker-compose.override.yml.restore" "$REMOTE/docker-compose.override.yml" \
      || return 1
  else
    rm -f "$REMOTE/docker-compose.override.yml" || return 1
  fi
}

same_files_as_state() {  # <state dir>: does $REMOTE run from exactly these files?
  local dir="$1"
  cmp -s "$dir/docker-compose.yml" "$REMOTE/docker-compose.yml" || return 1
  if [ -f "$dir/docker-compose.override.yml" ]; then
    cmp -s "$dir/docker-compose.override.yml" "$REMOTE/docker-compose.override.yml"
  else
    [ ! -e "$REMOTE/docker-compose.override.yml" ]
  fi
}

# ------------------------------------------------- runtime preflight
#
# CI runs the image on a stock Docker. The server's may refuse what the
# compose file asks (snap Docker's AppArmor and no-new-privileges, RC2b.1).
# So before anything stops, the new image runs once here, under every
# restriction the new compose file sets — the image's own user, read-only
# root, tmpfs, capabilities, security options, memory and pids — and must
# exec a shell and import the app, as the real start does. No network, no
# volume, no .env, no wallets.yml: nothing of production is in it.

compose_restrictions() {  # <compose file>: docker run flags, one per line
  "$DOCKER" compose --project-directory "$REMOTE" -p "$PROJECT" -f "$1" config --format json \
    2>/dev/null | python3 -c '
import json, sys
s = json.load(sys.stdin)["services"]["blocktail"]
out = []
if s.get("read_only"): out.append("--read-only")
tmpfs = s.get("tmpfs") or []
for t in ([tmpfs] if isinstance(tmpfs, str) else tmpfs): out += ["--tmpfs", t]
for c in s.get("cap_drop") or []: out += ["--cap-drop", c]
for c in s.get("cap_add") or []: out += ["--cap-add", c]
for o in s.get("security_opt") or []: out += ["--security-opt", o]
if s.get("mem_limit"): out += ["--memory", str(s["mem_limit"])]
if s.get("pids_limit"): out += ["--pids-limit", str(s["pids_limit"])]
if s.get("user"): out += ["--user", str(s["user"])]
if s.get("privileged"): out.append("--privileged")
print("\n".join(out))'
}

runtime_preflight() {  # <image tag> <compose file>
  local flags out
  local -a args=()
  command -v python3 >/dev/null 2>&1 || die "python3 is needed on the server for the runtime check; nothing was changed"
  flags="$(compose_restrictions "$2")" \
    || die "could not read the restrictions from $2 (docker compose config); nothing was changed"
  [ -z "$flags" ] || mapfile -t args <<< "$flags"
  say "runtime check: $1 under ${args[*]:-no restrictions}"
  if ! out="$("$DOCKER" run --rm --network none --name "blocktail-preflight-$$" "${args[@]}" \
        --entrypoint sh "$1" -c 'exec python -c "import app.main; print(\"preflight: ok\")"' 2>&1)" \
     || ! grep -q '^preflight: ok$' <<<"$out"; then
    printf '%s\n' "$out" | sed 's/^/    /' >&2
    die "$1 does not start under its compose restrictions on this host's Docker (above);" \
        "the running version is untouched. See docs/production.md, \"Runtime check\"."
  fi
  say "runtime check: ok"
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
deploy:   1. Return to the last good version — its image and the compose files it
deploy:      ran with, from $(state_dir "$good") (the database is left as it is):
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
  # Names start with a UTC timestamp, so the last match in glob order is the
  # newest. A backup still being written is a dotfile (.partial-…): not matched.
  local path newest=""
  for path in "$REMOTE/backups"/*"-from-$1.db"; do
    [ -f "$path" ] && newest="${path##*/}"
  done
  [ -z "$newest" ] || printf '%s\n' "$newest"
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

  # Before anything is built, stopped or replaced: the way back to the last
  # good version — its image and the compose files it ran with — is saved
  # and verified, or nothing happens.
  secure_the_way_back "$good"

  if new_id="$(image_id "$tag")"; then
    say "$tag is already built; not rebuilding — a commit's image is immutable here"
  else
    say "building $tag"
    "$DOCKER" build --label "org.opencontainers.image.revision=$SHA" -t "$tag" "$RELEASE" >&2 \
      || die "image build failed; the running version is untouched"
    new_id="$(image_id "$tag")"
  fi

  # The new image, under the new compose file's restrictions, on this host's
  # Docker — while the running version still runs.
  runtime_preflight "$tag" "$RELEASE/docker-compose.yml"

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
  # compose's own status is not the verdict: the health check below is.
  compose up -d --no-build --force-recreate >&2 \
    || say "docker compose up reported an error; the health check decides"

  if wait_healthy "$new_id"; then
    # The new version's own state, then the record: a version is recorded as
    # good only with a way back to it.
    if ! save_state "$tag" "$new_id" || ! check_state "$tag"; then
      log_line "$SHA healthy but its state could not be saved image=$tag last_good=${good:-none}"
      die "$tag is running and healthy, but its state could not be saved in" \
          "$(state_dir "$tag"), so it is not recorded as good; last good is still ${good:-none}"
    fi
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
  local tag id recorded dir kept running_id
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
  # A version is its image and the compose files it ran with; without a
  # verified record of both there is nothing safe to go back to.
  check_state "$tag" || die "$STATE_PROBLEM — $tag is not a rollback target. Nothing was changed."
  dir="$(state_dir "$tag")"
  PROJECT="$(project_name)"

  # Asked twice, the second time changes nothing.
  running_id="$("$DOCKER" inspect -f '{{.Image}}' "$CONTAINER" 2>/dev/null || true)"
  if [ "$running_id" = "$id" ] && same_files_as_state "$dir" && wait_healthy "$id"; then
    record_good "$tag" "$id"
    log_line "rollback to $tag: already running, nothing changed"
    say "ok: $tag already runs from its saved compose files and is healthy; nothing was changed"
    return 0
  fi

  kept="$(keep_attempt "before-rollback-to-$(version_of "$tag")")" \
    || die "could not keep the current compose files before replacing them; nothing was changed"
  say "kept what ran until now in $kept"
  say "rolling back to $tag ($id) with the compose files it ran with; the database is NOT restored"
  install_state_files "$dir" \
    || die "could not put $tag's compose files back from $dir; the files that ran until now are in $kept"
  compose up -d --no-build --force-recreate >&2 \
    || say "docker compose up reported an error; the health check decides"
  if wait_healthy "$id"; then
    record_good "$tag" "$id"
    log_line "rollback ok image=$tag id=$id kept=$kept"
    say "ok: $tag is running with its own compose files. The database was not touched;"
    say "if this version cannot read it, see docs/operations.md, \"Restoring the database\"."
    return 0
  fi
  log_line "rollback failed image=$tag kept=$kept"
  say "rollback to $tag did not become healthy within ${HEALTH_TIMEOUT}s"
  show_logs
  cat >&2 <<EOF

deploy: The compose files of $tag are in place (from $dir) and its image is
deploy: what they name; the record of what is good was not changed. What ran
deploy: before is kept in $kept. The database was not touched. Stop here:
deploy: read the log above and docs/operations.md, "When a rollback fails".
EOF
  exit 1
}

case "${1:-}" in
  deploy) shift; deploy "$@" ;;
  rollback) shift; rollback "$@" ;;
  *) die "usage: remote-deploy.sh deploy <sha> [<unpacked dir>] | rollback [<image tag>]" ;;
esac
