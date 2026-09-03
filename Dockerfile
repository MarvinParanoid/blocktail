FROM python:3.13-slim

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
CMD ["sh", "-c", "exec uvicorn app.main:main --factory --host ${HOST} --port ${PORT} --workers 1"]
