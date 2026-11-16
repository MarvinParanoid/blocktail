# The tag and its digest: the digest makes the build the same next month, the
# tag says what it is and is what Dependabot follows — a new digest of the same
# tag arrives as a pull request and runs the container smoke. The digest is the
# multi-platform index (amd64, arm64, …), so it pins the image on any host.
# The same image also fixes the system uid the user below gets, which the data
# volume depends on. See docs/production.md.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BLOCKTAIL_CONFIG=/config/wallets.yml \
    BLOCKTAIL_DB=/data/blocktail.db \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Created exactly as before, on purpose: the data volume on a running host is
# owned by the uid this gave the user, and the deploy's backup step runs as
# the image's user. A different uid would leave the next image unable to
# write its own database. The code stays root-owned: the app cannot change it.
RUN adduser --system --group --no-create-home blocktail \
    && mkdir -p /data /config \
    && chown -R blocktail:blocktail /data

USER blocktail
EXPOSE 8000

# Liveness only: the app stays healthy while the upstream provider is down, and
# reports the degraded sync state in the response body and in the UI.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

# One worker on purpose: the indexer runs inside the web process, so a second
# worker would run a second indexer against the same database and provider quota.
# `exec`: uvicorn is PID 1 and gets docker stop's SIGTERM itself; it finishes
# open requests for up to 20 s (compose waits 30), stops the indexer, closes
# the database, and exits as stopped by that signal (143).
# --no-proxy-headers: the app trusts no X-Forwarded-* from anyone, and uvicorn
# then does not either. --no-server-header: no version banner.
CMD ["sh", "-c", "exec uvicorn app.main:main --factory --host ${HOST} --port ${PORT} --workers 1 --no-proxy-headers --no-server-header --timeout-graceful-shutdown 20"]
