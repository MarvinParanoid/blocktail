# Operations

How blocktail is deployed to a server, how its database is backed up and
restored, and what to do when a new version does not start.

Everything below assumes the layout `scripts/deploy.sh` creates. Commands are
run on the server unless they say otherwise; `$REMOTE` is the deploy directory,
`/root/blocktail-build` by default.

---

## Deploying

From a clean checkout, on your machine:

```bash
scripts/deploy.sh --check-only   # everything except sending: tests, lint, archive
scripts/deploy.sh                # check, ship, back up, start, verify
```

`BLOCKTAIL_HOST` (default `tw-vps`) and `BLOCKTAIL_REMOTE` (default
`/root/blocktail-build`) select the target.

```
your machine                                server ($REMOTE)
────────────                                ─────────────────
refuse: dirty tree (incl. untracked)
refuse: HEAD not a commit
refuse: .env / wallets.yml / *.db tracked
git archive HEAD ─► extract to a temp dir
  pytest + ruff F,E9 on that tree
  refuse on any failure
ssh: unpack archive ───────────────────────► releases/.incoming-<sha>-<time>-<pid>/
ssh: remote-deploy.sh deploy <sha> ────────► take the deploy lock
                                              (held → refuse, remove own
                                               unpacked tree, change nothing)
                                            move the tree to releases/<sha>/
                                            refuse: .env or wallets.yml missing
                                            blocktail:<sha> already built? reuse it
                                              else docker build -t blocktail:<sha>
                                              (build fails → stop, nothing changed)
                                            SQLite online backup → backups/
                                              integrity_check, keep last 5 + the
                                              newest taken from the last good one
                                              (backup fails → stop, nothing changed)
                                            pin blocktail:<sha> in
                                              docker-compose.override.yml
                                            docker compose up -d --no-build
                                            wait ≤ 90 s: container runs the new
                                              image id and /healthz answers 200
                                              ok   → record as last good, log,
                                                     keep last 5 releases
                                              fail → logs + rollback to the last
                                                     good SHA, exit 1, nothing
                                                     undone, record untouched
```

There is no switch to skip a check. A deploy ships `git archive HEAD` and
nothing else, so a working-tree file, an ignored file or a local `.env` can
never reach the server, and the tests that passed are the tests of exactly
what ships.

### On the server

```
$REMOTE/
  .env, wallets.yml            yours; never shipped, never touched
  docker-compose.yml           copied from the release being deployed
  docker-compose.override.yml  pins the running image: blocktail:<sha>
  last-good                    "<tag> <image id>" of the last version that
                               passed its health check
  releases/<sha>/              the last five releases shipped
  backups/                     SQLite backups (see below)
  deployments.log              one line per attempt, with image and backup
  .deploy.lock, .deploy.lock.owner
                               the deploy lock, and who last took it
```

### One at a time

A deploy or a rollback holds a lock on `$REMOTE/.deploy.lock` from the moment
it moves the shipped tree into place until it has recorded the outcome:
build, backup, start, health check and the record of what is good are never
interleaved with another run. A second run is refused at once — "another
deploy or rollback is running (… pid …: deploy 3ab1290c5d1e)" — and changes
nothing; it only removes the tree its own session unpacked.

The lock is `flock` on an open file, so the kernel releases it when the
holding process ends, however it ends — `kill -9` included. On a host without
`flock` a lock directory stands in, holding the owner's pid; it counts as held
only while that process is alive and running the deploy script, so a crashed
run never blocks the next one. A `docker build` started by a deploy that is
then killed keeps the lock until the build process itself exits.

### Images, and what "last good" means

Images are tagged `blocktail:<sha>`, and a tag, once built on the host, is not
built again: retrying a commit reuses its image, and a tag means the same image
for as long as it exists. Images are never deleted by the scripts.

`$REMOTE/last-good` holds the tag and image id of the last version that
passed its health check — written after a successful deploy or rollback, and
never by a failure. However many attempts at a new version fail in a row, the
rollback target stays the version that last worked, and the failure message
names it by its SHA tag. The backup taken while that version was running is
kept out of rotation for the same reason.

Compose keeps running under the project name of the container that was
already there, so the data volume (`<project>_blocktail-data`) stays the same
one. A plain `docker compose up -d` or `docker compose restart` in `$REMOTE`
runs the deployed image.

**Never run `docker compose up --build` on the server.** It builds whatever
files happen to be in `$REMOTE` — on a server deployed by the old script, a
stale copy of the code — and tags the result as the deployed version.

### Before the first deploy after S8b

`docker-compose.yml` requires `ALLOWED_HOSTS` in the server's `.env`, and
`remote-deploy.sh` checks it before it builds or backs anything up: a deploy
or a rollback without it stops at once and changes nothing. Add the name the
proxy serves the app as (`ALLOWED_HOSTS=watch.example.com`) once. The
container now also runs with a read-only root, no capabilities and a memory
limit; backups and restores run in their own `docker run --volumes-from`
container and are not affected. See [production.md](production.md).

### The first deploy with this script

A server set up by the earlier `deploy.sh` has the code unpacked directly in
`$REMOTE` and no `last-good` record. The first run of this one detects the
running container's compose project, tags the image it runs
`blocktail:baseline-<image id>` and records that as the last good version,
backs up its database, and takes over from there. The old code files left in `$REMOTE` are no longer used; they
can be removed once a deploy has succeeded, keeping `.env`, `wallets.yml`,
`docker-compose*.yml`, `releases/`, `backups/` and `deployments.log`.

---

## What "healthy" means here

The deploy waits for `/healthz` to return 200 from the new container. That
proves the web process started, ran its migrations and serves requests. It
does **not** prove the provider is reachable or the index is current —
`/healthz` stays 200 through a provider outage on purpose, so that Docker does
not restart a container that is serving fine. After a deploy, look at the page
(the dot and the block number in the top bar) or at `GET /api/status`.

---

## Backups

Every deploy takes one before starting the new version. They use SQLite's
online backup API from inside the app image, as the app's own user, while the
app keeps running; copying `blocktail.db` with `cp` would miss whatever is
still in `blocktail.db-wal`. Each backup is checked with `PRAGMA
integrity_check` and saved as one self-contained file:

```
$REMOTE/backups/blocktail-20261116T081500Z-before-3ab1290c5d1e-from-e83ad579990a.db
```

A backup is named after the version it was taken from:
`before-<new sha>-from-<running sha>`. Older ones are removed only after a new
one has been verified, keeping five — plus, whatever its age, the newest one
taken while the last good version was running, so a string of failed attempts
(each of which backs up the failed version) cannot rotate it out.

To take one by hand at any time:

```bash
cd "$REMOTE"
IMAGE="$(docker inspect -f '{{.Config.Image}}' blocktail)"
OPS="$(ls -1dt releases/*/ | head -n 1)scripts"
docker run --rm --volumes-from blocktail \
  -v "$PWD/backups:/backups" -v "$PWD/$OPS:/ops:ro" \
  --entrypoint python "$IMAGE" /ops/sqlite_backup.py backup \
  --source /data/blocktail.db --dest-dir /backups --label manual --keep 5
```

To check one:

```bash
docker run --rm -v "$PWD/backups:/backups:ro" -v "$PWD/$OPS:/ops:ro" \
  --entrypoint python "$IMAGE" /ops/sqlite_backup.py verify \
  --path /backups/blocktail-….db
# ok: schema 7, 41230 activities
```

On a machine with the `sqlite3` CLI, `sqlite3 FILE 'PRAGMA integrity_check'`
printing `ok` says the same.

Copy them off the server now and then. Five backups on the same disk protect
against a bad deploy, not against losing the disk.

---

## Restoring the database

Restoring replaces the whole database with the backup. Everything indexed
after the backup was taken is lost from the file — and fetched again on the
next sync cycles, at the cost of provider quota.

```bash
cd "$REMOTE"
docker compose stop blocktail            # the app must not be running

ls -1 backups/                           # choose a file
IMAGE="$(docker inspect -f '{{.Config.Image}}' blocktail)"
OPS="$(ls -1dt releases/*/ | head -n 1)scripts"
BACKUP=blocktail-….db

docker run --rm --volumes-from blocktail \
  -v "$PWD/backups:/backups:ro" -v "$PWD/$OPS:/ops:ro" \
  --entrypoint python "$IMAGE" /ops/sqlite_backup.py restore \
  --backup "/backups/$BACKUP" --target /data/blocktail.db
```

`restore` checks the backup's integrity before touching anything, refuses
while `blocktail.db-wal` still holds unwritten changes (a sign the app is
still running), and keeps the file it replaces as
`blocktail.db.before-restore-<time>` in the volume. Then start the version
that matches the backup — see the next section — and check:

```bash
docker run --rm --volumes-from blocktail -v "$PWD/$OPS:/ops:ro" \
  --entrypoint python "$IMAGE" /ops/sqlite_backup.py verify --path /data/blocktail.db
```

Remove the `before-restore` file once you are sure you will not need it.

---

## Rolling back the application

```bash
cat "$REMOTE/last-good"                      # blocktail:<sha> sha256:…
SCRIPT="$(ls -1dt "$REMOTE"/releases/*/ | head -n 1)scripts/remote-deploy.sh"

BLOCKTAIL_REMOTE="$REMOTE" bash "$SCRIPT" rollback                    # the last good version
BLOCKTAIL_REMOTE="$REMOTE" bash "$SCRIPT" rollback blocktail:<sha>    # or one you name
```

Any release's copy of the script can roll back to any image on the host
(`docker image ls blocktail`). Rollback takes the deploy lock, pins the image,
starts it and waits for `/healthz`; if it comes up healthy it becomes the
last good version. It refuses when the last good tag no longer points at the
image id recorded for it — rebuilt or retagged by hand — and asks you to name
the image instead. **It does not touch the database.**

### Rollback after a migration

Migrations only move forward, and there is no automatic way back. Whether the
previous version can run on a database the new one has migrated depends on the
migration:

* A version that does not know a migration ignores it and starts: it applies
  the ones it knows and does not look at the rest.
* An added column is usually harmless to older code — but not always. After
  migration 007, for example, older code would store a failed token lookup as
  `decimals = 0`, and the new column would mark that zero as *known*; going
  forward again would then trust it.
* A changed or removed table may stop the older version from starting at all.

CI checks the first and the last of these for every change against main, the
release production runs (the `rollback` job): `tests/test_rollback.py` runs
main's code on a synthetic database this change has migrated — every read
compared with the same read before the migration, and wallets added, renamed
and removed — and `scripts/rollback-smoke.sh` makes a legacy server out of
main's image and compose, deploys this change onto it with this script, rolls
back to the baseline under the new compose, and deploys again. The second
point is not checked: it shows only on a forward deploy after a rollback in
which the older code met a token whose lookup failed.

So after a failed deploy that included a migration:

1. Roll back the application (above) and look at the page and the logs.
2. If the last good version misbehaves on the migrated database, stop it and
   restore the newest backup taken while it was running — named
   `…-from-<its sha>.db`, and the one the failure message prints — then start
   it again.
3. Fix forward when you can: a corrected new version usually costs less than
   a restore, which re-fetches everything since the backup.

`deployments.log` names the backup taken before each deploy.

---

## When a new version does not start

The deploy stops with the last 80 log lines and the rollback commands. Nothing
has been undone at that point.

1. Read the log lines. A configuration error (`ALCHEMY_API_KEY is not set`, an
   invalid `wallets.yml`) is fixed in `$REMOTE` and needs no rollback:
   `docker compose up -d` again.
2. A migration error, or a crash in the new code: roll back the application.
3. If the last good version cannot open the database: restore the newest
   `…-from-<its sha>.db` backup, then start it again.
4. `docker compose ps` and `docker compose logs --tail 200` show the current
   state at any point; `deployments.log` shows what was attempted.

`scripts/deploy.sh` is not the place to retry until it works: every attempt
takes another backup and rotates out an older one.
