# Production

How blocktail is meant to run in production, what the repository already
enforces, and what can only be checked on the server. The server itself — its
name, its firewall, its proxy — is the operator's, and nothing here assumes a
particular one: `watch.example.com` stands for whatever name you serve it as.

## Topology

```
 browser ──HTTPS:443──► reverse proxy on the host (Caddy)   ── TLS, HSTS, public port
                         │  keeps Host, adds X-Forwarded-*     (the app ignores them)
                         ▼
               127.0.0.1:${HOST_PORT:-8000}                  ── loopback only
                         │  docker port publishing
                         ▼
 container "blocktail"  uvicorn :8000, one worker, uid of `blocktail` (not root)
   read-only root, /tmp tmpfs 64m, no capabilities, no setuid file,
   512m memory, 256 pids, logs rotated (3 × 10 MB)
   ├── /data            named volume blocktail-data: SQLite + -wal/-shm
   ├── /config/wallets.yml   bind mount, read-only
   └── egress           HTTPS to the provider (Alchemy) and its Prices API
```

* **One way in.** Only the proxy listens publicly. The container publishes to
  `127.0.0.1` and never to `0.0.0.0`; there is no other port, no database
  port, no admin interface. SQLite is a file in a volume and is reachable by
  nothing but the container and the backup step's `docker run`.
* **Two locks.** Basic auth in the proxy, or the app's own
  (`AUTH_USER`/`AUTH_PASSWORD`), or both. `/healthz` stays open in the app so
  Docker's healthcheck can reach it; it says only `{"status":"ok"}` while auth
  is on.
* **One network.** Compose's default bridge network for this one service. It
  needs egress to the provider and nothing inbound but the published port; a
  second network would separate nothing.
* **Secrets.** `.env` and `wallets.yml` live in `$REMOTE` on the server only,
  are never shipped by the deploy, never copied into the image
  (`.dockerignore`, and the Dockerfile copies `requirements.txt` and `app/`
  only), and reach the container as environment and a read-only file.
  `chmod 600 .env`.

Over Tailscale instead of a public proxy: publish to the tailnet address
rather than loopback (`ports: - "100.x.y.z:8000:8000"` in an override file),
set `ALLOWED_HOSTS` to the tailnet name or address, and skip the proxy.

## What the container may do

| Setting | Why | What still writes |
| --- | --- | --- |
| non-root user `blocktail` | the app needs no privilege | `/data` (owned by it), `/tmp` |
| `read_only: true` | code and system cannot be changed at runtime | |
| `tmpfs: /tmp:size=64m` | SQLite's temporary sort files | |
| `cap_drop: [ALL]` | port 8000 needs no capability | |
| no setuid/setgid file in the image (Dockerfile) | nothing can raise its privileges — the job `no-new-privileges` would do, which this host cannot run (below) | |
| `mem_limit: 512m`, `pids_limit: 256` | a runaway is contained (peak RSS ≈ 160 MB on 200k events) | |
| `stop_grace_period: 30s` | uvicorn finishes requests for up to 20 s, then the indexer and database close | |
| `logging: json-file 3 × 10m` | the log cannot fill the disk | |
| `restart: unless-stopped` | back after a crash or reboot, not after `docker compose stop` | |

The user is created exactly as it always was (`adduser --system`): the uid
it gets is the owner of the existing volume, and the deploy's backup step
runs as the image's user. Changing it would leave the next image unable to
write its own database.

Backups, verification and restores (`scripts/sqlite_backup.py`, run by
`remote-deploy.sh` and by hand per [operations.md](operations.md)) use a
separate `docker run --volumes-from blocktail` container, which takes the
volume but none of the restrictions above, so they keep working unchanged.

## Docker from snap, and no-new-privileges

The production host runs Docker from the snap package (`snap list docker`;
its data under `/var/snap/docker/`). That Docker runs under the AppArmor
profile `snap.docker.dockerd`, which denies **every exec** in a container
started with `security_opt: no-new-privileges:true`. The container never
starts: the log says `exec /usr/bin/sh: operation not permitted`, the kernel
log `apparmor="DENIED" operation="exec" info="no new privs"
profile="snap.docker.dockerd"`. The first production deploy (RC2b.1)
crash-looped on exactly this; GitHub's runners use Docker from Docker's own
packages, where it works, so CI could not see it.

So `docker-compose.yml` does not set it. What it protected against — a
process that is not root becoming root through a setuid program — cannot
happen here anyway: the Dockerfile removes the setuid and setgid bits from
every file in the image (the base ships su, mount, passwd and eight more),
and `scripts/container-smoke.sh` fails if one is found. Everything else
stays: the image's own user, read-only root, `/tmp` on tmpfs, no
capabilities, memory and pid limits.

Moving the host to Docker's own packages would make the option usable again;
that is a change to the server, not to blocktail, and is not assumed.

### Runtime check

Because CI cannot reproduce the host's Docker, every deploy checks it on the
host itself, before anything stops: `remote-deploy.sh` reads the new compose
file's restrictions (`docker compose config`) and runs the new image once
with them — its own user, `--read-only`, the tmpfs, `--cap-drop`,
`--security-opt`, `--memory`, `--pids-limit` — with `--network none`, no
volume, no `.env` and no `wallets.yml`; it must exec a shell and import the
app, as the real start does. If it does not, the deploy stops there: the
running version, its files and the database are untouched.

The same check by hand, for a compose change you want to try first:

```bash
docker run --rm --network none --read-only --tmpfs /tmp:size=64m,mode=1777 \
  --cap-drop ALL --memory 512m --pids-limit 256 [--security-opt <each one>] \
  --entrypoint sh blocktail:<sha12> -c 'exec python -c "import app.main; print(\"preflight: ok\")"'
```

Pass: it prints `preflight: ok` and exits 0. Fail: anything else — in
particular `operation not permitted`; `journalctl -k | grep DENIED` shows
whether AppArmor refused it.

## ALLOWED_HOSTS

Required by `docker-compose.yml` (compose refuses to start without it) and
checked by `remote-deploy.sh` before a deploy or rollback does anything. The
app's own default — any Host — is for running it by hand and for tests.

```
ALLOWED_HOSTS=watch.example.com                 # one name
ALLOWED_HOSTS=watch.example.com,*.tail1234.ts.net
```

* Set it to the names in the address bar. A request for any other Host gets
  400 `Unknown host.`, with the security headers, and the Host is never
  echoed into a body, a header or a redirect.
* **Loopback is not a way round it.** `localhost`, `127.0.0.1` and `[::1]`
  are accepted for `/healthz` only, and only from a loopback client: the
  healthcheck inside the container. From the host, through the published
  port, the client is the Docker bridge — so `curl http://127.0.0.1:8000/`
  on the server answers 400. Use `curl -H 'Host: watch.example.com' …`.
* **The proxy must keep the Host header.** Caddy does by default. nginx sends
  its upstream address unless told otherwise — with ALLOWED_HOSTS set that is
  a 400 on every request, plainly, not a silent bypass.
* With the list set, a write's `Origin` must name a listed host too.

## Reverse proxy

### Caddy (recommended)

```caddyfile
watch.example.com {
    # Optional second lock; the app's own AUTH_USER/AUTH_PASSWORD also works.
    basic_auth {
        you $2a$14$...                      # caddy hash-password
    }

    # Keeps Host; adds X-Forwarded-For/-Proto, which the app ignores.
    reverse_proxy 127.0.0.1:8000

    # Add only after HTTPS has been seen working for this name (below).
    # header Strict-Transport-Security "max-age=31536000"
}
```

Caddy obtains and renews the certificate and redirects HTTP to HTTPS by
itself. The app sets every other security header; do not add a second
`Content-Security-Policy` in the proxy — two policies both apply.

### nginx

```nginx
server {
    listen 443 ssl;
    server_name watch.example.com;
    ssl_certificate     /etc/letsencrypt/live/watch.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/watch.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;        # required: ALLOWED_HOSTS checks it
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;                # htmx polls; nothing is streamed, but keep it simple
    }
    # add_header Strict-Transport-Security "max-age=31536000" always;   # after HTTPS is confirmed
}
server {
    listen 80;
    server_name watch.example.com;
    return 301 https://$host$request_uri;
}
# No default_server that proxies to the app: a request for an unknown name
# should never reach it (it would get 400 anyway).
```

### Forwarded headers

The app reads none of `X-Forwarded-For`, `X-Forwarded-Proto`,
`X-Forwarded-Host` or `Forwarded`, and uvicorn runs with
`--no-proxy-headers`. Nothing the app decides depends on the client address
or the scheme, so nothing can be forged with them; the cost is that uvicorn's
access log shows the proxy's address rather than the client's (the proxy's own
log has it).

### HSTS

Not sent by the app, on purpose: whether HTTPS is real for a name, and for
which subdomains, is the proxy's knowledge. Turn it on in the proxy only after
the site has been seen over HTTPS with a valid certificate, start with a short
`max-age` (an hour), then a year. Do not add `includeSubDomains` or `preload`
unless every subdomain of the domain is HTTPS — that commitment is not
blocktail's to make.

## Release-candidate checks on the server

What the repository cannot check — run these on the VPS before calling a
build a release candidate. Nothing here changes the server.

1. **Nothing but the proxy is public.**
   `ss -ltnp` — only the proxy on `:80`/`:443` on a public address; the app on
   `127.0.0.1:8000` only. From another machine:
   `curl -m 5 http://<server-ip>:8000/` must fail to connect.
2. **The firewall agrees.** `ufw status` / `nft list ruleset`. Docker writes
   its own iptables rules for published ports, which a host firewall does not
   see — the loopback binding is what keeps 8000 private; check it in
   `docker ps` (`127.0.0.1:8000->8000/tcp`).
3. **HTTPS is valid** for every name in `ALLOWED_HOSTS`:
   `curl -sI https://watch.example.com/healthz` → 200, certificate valid;
   `curl -sI http://watch.example.com/` → redirect to HTTPS.
4. **Host policy through the proxy:**
   `curl -s -o /dev/null -w '%{http_code}' -H 'Host: evil.example' https://<server-ip>/ -k`
   → not the app's page (no site for that name, or 400).
5. **Headers reach the browser unchanged:** `curl -sI -u you:… https://watch.example.com/`
   shows `content-security-policy`, `x-frame-options: DENY`,
   `cache-control: no-store`, and exactly one CSP.
6. **The container runs as configured:**
   `docker inspect blocktail -f '{{.Config.User}} {{.HostConfig.ReadonlyRootfs}} {{.HostConfig.CapDrop}} {{.HostConfig.SecurityOpt}} {{.HostConfig.Memory}}'`
   → `blocktail true [ALL] [] 536870912`;
   `docker exec blocktail id -u` → not 0.
7. **The volume is the app's:** `docker exec blocktail ls -ln /data` → owned by
   that uid, `blocktail.db-wal` present while running.
8. **Health:** `docker inspect -f '{{.State.Health.Status}}' blocktail` →
   `healthy`; `/api/status` (with auth) → `state` is `ok`.
9. **Backups:** the last deploy's line in `deployments.log` names a backup, and
   `sqlite_backup.py verify` on it passes (operations.md).
10. **Secrets:** `ls -l .env` → `-rw-------`; `docker history --no-trunc
    blocktail:<sha>` and `docker image inspect` show no key; `docker logs
    blocktail | grep -c <first 8 chars of the key>` → 0.
11. **Base image pinned.** The Dockerfile names `python:3.13-slim` with the
    digest of its multi-platform index (pinned in RC1.1: the index CI built and
    tested). On the server, `docker image inspect blocktail:<sha>` after the
    build, and `docker buildx imagetools inspect python:3.13-slim@sha256:…`
    lists the platform the server pulled. A newer digest arrives from
    Dependabot as a pull request; take it through CI, never by hand-editing
    the tag away.
12. **HSTS** added in the proxy once 3 has been seen working.

## Release checklist: right before a production deploy

Read-only, except where marked. Stop at the first item that does not hold.

1. **The release.** A clean checkout at the release commit; its CI run green
   in every job; `scripts/deploy.sh --check-only` passes.
2. **The image** (when shipped prebuilt, operations.md): `sha256sum -c
   SHA256SUMS` in the artifact; after `docker load` (*changes: adds an
   image*), the id on the server is `config` in `image.json`.
3. **Production is healthy:** `docker inspect blocktail` → running, healthy,
   no OOM, no restarts you cannot explain; `/healthz` and `/api/status` (with
   auth) through HTTPS.
4. **Room:** at least 300 MB of memory available and 1 GB of disk.
5. **The way back exists:** `cat last-good`; the image it names is on the
   host (`docker image inspect`); `ls states/` — the last good version has a
   state, or what runs now is that version (the deploy saves it first). A
   state that does not verify is reported and refused by the deploy.
6. **Configuration:** `ALLOWED_HOSTS` set once, to the served names;
   `HOST_PORT` set if the host's 8000 is taken by something else.
7. **Data:** a fresh backup, and a copy of it off the server, checksummed.
8. **Time:** not within the host's cron windows (backups); someone watches.
9. **The runtime check is part of the deploy** — it starts one throwaway
   container from the new image, without network, volume or secrets (*a
   temporary container; agree to it with the deploy*). A refusal there is the
   end of the attempt, not something to work around.
10. **Rollback is ready:** the command from operations.md, "Rolling back the
    application", with the last good tag filled in; and "When a rollback
    fails" read once.

What CI checks on every push instead (the `image` job,
`scripts/container-smoke.sh`): a non-root uid; no planted secret in the image
files, config or history; compose refusing a `.env` without `ALLOWED_HOSTS`;
healthy by its own healthcheck; read-only root and writable `/tmp`; no
effective capabilities and no setuid or setgid file; WAL in the volume owned by the
app; auth, the Host and origin policy and the headers through the published
port; a clean SIGTERM stop; no secret in the log; a backup, verify and restore
as the deploy does them, and healthy again after.
