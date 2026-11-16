"""One phase of tests/test_rollback.py, run in its own interpreter: the
previous release and this one are both the package `app`.

    python -I tests/rollback_phases.py <code root> <phase> <work dir>

It imports `app` and `tests.conftest` from <code root> only, so the previous
release runs with its own code and its own test helpers.

old1  the previous release: synthetic wallets and events, a wallet added and renamed from
      the page, a token whose decimals are unknown; snapshot every read.
new   RC: migrate the same file (007, 008), run a cycle, ask for older
      history, so the new columns hold what a running RC would leave.
old2  the previous release again on the migrated file: the same reads, compared with the
      snapshot; then writes (add, rename, remove a wallet; older history).
new2  RC once more on what the old code wrote: rolling forward again.
"""

import asyncio
import json
import logging
import re
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

root, phase, work = sys.argv[1], sys.argv[2], Path(sys.argv[3])
sys.path.insert(0, root)

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402
from app.models import NATIVE, AssetRef, TransferKind  # noqa: E402
from tests.conftest import (  # noqa: E402
    BINANCE, COLD, MAIN, PAYMENTS, STRANGER, USDC,
    FakePrices, FakeProvider, address, make_config, make_settings, transfer,
)

problems: list[str] = []


class Collect(logging.Handler):
    def emit(self, record):
        # The stand runs without auth on purpose; that warning is expected.
        if record.levelno >= logging.WARNING \
                and "AUTH_USER/AUTH_PASSWORD" not in record.getMessage():
            problems.append(f"log {record.levelname} {record.name}: {record.getMessage()}")


logging.getLogger().addHandler(Collect())
logging.getLogger().setLevel(logging.WARNING)

MYST = AssetRef("ethereum", address("mystery-token"), "MYST", 0)   # decimals unknown
TRADING = address("trading")
LATER = address("later")
SAME_ORIGIN = {"sec-fetch-site": "same-origin", "HX-Request": "true"}


def provider() -> FakeProvider:
    p = FakeProvider()
    p.head_block = 21_000_100
    p.transfers = [
        transfer("in-eth", sender=STRANGER, recipient=MAIN, amount=24 * 10**17, block=21_000_010),
        transfer("out-eth", sender=MAIN, recipient=STRANGER, amount=12 * 10**16, block=21_000_020),
        transfer("out-usdc", sender=COLD, recipient=BINANCE, amount=500_000_000, asset=USDC,
                 kind=TransferKind.TOKEN, log_index=3, block=21_000_030),
        transfer("internal-in", sender=STRANGER, recipient=PAYMENTS, amount=5 * 10**16,
                 kind=TransferKind.INTERNAL, trace="0_1", block=21_000_040),
        transfer("wallet-to-wallet", sender=MAIN, recipient=COLD, amount=5 * 10**18,
                 block=21_000_050),
        transfer("myst-in", sender=STRANGER, recipient=COLD, amount=777, asset=MYST,
                 kind=TransferKind.TOKEN, log_index=1, block=21_000_060),
        transfer("trading-in", sender=BINANCE, recipient=TRADING, amount=3 * 10**18,
                 block=21_000_070),
        transfer("later-in", sender=STRANGER, recipient=LATER, amount=10**17, block=21_000_080),
    ]
    p.native_balances = {MAIN: 3 * 10**18, COLD: 10**18, PAYMENTS: 5 * 10**16,
                         STRANGER: 900 * 10**18, TRADING: 3 * 10**18}
    return p


def app():
    config = make_config(wallets=[
        {"name": "Main", "address": MAIN},
        {"name": "Cold", "address": COLD},
        {"name": "Payments", "address": PAYMENTS},
        {"name": "Whale", "address": STRANGER, "owned": False},
    ])
    prices = FakePrices(quotes={("ethereum", NATIVE): Decimal("3000"),
                                ("ethereum", USDC.contract_address): Decimal("1")})
    return create_app(settings=make_settings(work), config=config, provider=provider(),
                      price_source=prices, run_indexer=False)


def ids(ctx) -> dict[str, int]:
    return {a.name: a.id for a in ctx.db.list_accounts()}


def check(response, what, ok=(200,)):
    if response.status_code not in ok:
        problems.append(f"{what}: HTTP {response.status_code} {response.text[:200]}")
    return response


VOLATILE = [
    (re.compile(r"\b1[0-9]{9}\b"), "<ts>"),                       # unix times
    (re.compile(r"\b\d+\s*(s|sec|min|m|h|d)\s+ago\b"), "<ago>"),
    (re.compile(r"just now|moments? ago"), "<ago>"),
    (re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:?\d{2})?"), "<dt>"),
    (re.compile(r"\b\d{1,2}:\d{2}(:\d{2})?\b"), "<hm>"),
]


def normal(text: str) -> str:
    for pattern, sub in VOLATILE:
        text = pattern.sub(sub, text)
    return text


def snapshot(client, ctx) -> dict[str, list]:
    wallets = ids(ctx)
    paths = ["/", "/activity", "/activity?q=Binance", "/activity?q=MYST",
             "/activity?direction=in", "/activity?direction=out",
             f"/activity?wallet={wallets['Cold']}", f"/activity?wallet={wallets['Trading desk']}",
             "/activity?scope=mine", "/summary", "/wallets", "/holdings", "/dust-note",
             "/status", "/api/activity", "/api/accounts", "/api/portfolio", "/api/status",
             f"/wallets/form?id={wallets['Trading desk']}"]
    activity_ids = [row[0] for row in ctx.db.connect().execute(
        "SELECT id FROM activities ORDER BY id")]
    paths += [f"/tx/{i}" for i in activity_ids]
    out = {}
    for path in paths:
        r = check(client.get(path), f"GET {path}")
        out[path] = [r.status_code, normal(r.text)]
    return out


def schema(db: Path):
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        versions = [v for (v,) in c.execute("SELECT version FROM schema_migrations ORDER BY 1")]
        integrity = c.execute("PRAGMA integrity_check").fetchone()[0]
        fk = c.execute("PRAGMA foreign_key_check").fetchall()
        myst = c.execute("SELECT * FROM assets WHERE symbol='MYST'").fetchall()
        cols = [d[1] for d in c.execute("PRAGMA table_info(assets)")]
        return {"versions": versions, "integrity": integrity, "fk_violations": len(fk),
                "myst": [dict(zip(cols, row)) for row in myst]}
    finally:
        c.close()


def cycle(ctx):
    asyncio.run(ctx.indexer.run_once())


report = {"phase": phase}
db = work / "blocktail.db"

with TestClient(app()) as client:
    ctx = client.app.state.ctx
    cycle(ctx)
    if phase == "old1":
        check(client.post("/wallets", data={"name": "Trading", "address": TRADING,
                                            "chain": "ethereum", "owned": "on"},
                          headers=SAME_ORIGIN), "add Trading")
        cycle(ctx)
        check(client.post(f"/wallets/{ids(ctx)['Trading']}", data={"name": "Trading desk"},
                          headers=SAME_ORIGIN), "rename Trading")
        (work / "snap6.json").write_text(json.dumps(snapshot(client, ctx)))
    elif phase in ("new", "new2"):
        check(client.post("/history/older", headers=SAME_ORIGIN), "RC: ask for older history")
        cycle(ctx)
        for path in ["/", "/activity", "/summary", "/wallets", "/api/status", "/status",
                     "/activity?q=MYST", "/holdings"]:
            check(client.get(path), f"RC GET {path}")
        report["accounts"] = sorted(ids(ctx))
        c = ctx.db.connect()
        cols = [d[1] for d in c.execute("PRAGMA table_info(sync_state)")]
        report["sync_state"] = [
            {k: v for k, v in zip(cols, row) if k.startswith(("history_", "balances_"))}
            for row in c.execute("SELECT * FROM sync_state")]
    elif phase == "old2":
        before = json.loads((work / "snap6.json").read_text())
        after = snapshot(client, ctx)
        report["reads_compared"] = len(before)
        report["reads_differing"] = [p for p in before if before[p] != after.get(p)]
        (work / "snap8.json").write_text(json.dumps(after))
        # Writes on the migrated file.
        check(client.post("/wallets", data={"name": "Later", "address": LATER,
                                            "chain": "ethereum", "owned": "on"},
                          headers=SAME_ORIGIN), "old: add Later")
        cycle(ctx)
        later = ids(ctx)["Later"]
        check(client.post(f"/wallets/{later}", data={"name": "Later renamed"},
                          headers=SAME_ORIGIN), "old: rename Later")
        check(client.post("/history/older", headers=SAME_ORIGIN), "old: ask for older history")
        cycle(ctx)
        feed = check(client.get(f"/activity?wallet={later}"), "old: Later's feed").text
        report["later_rows"] = feed.count("data-activity")
        check(client.post(f"/wallets/{ids(ctx)['Trading desk']}/remove", headers=SAME_ORIGIN),
              "old: remove Trading desk")
        cycle(ctx)
        report["accounts"] = sorted(ids(ctx))

report["schema"] = schema(db)
report["problems"] = problems
print(json.dumps(report, indent=1, default=str))
