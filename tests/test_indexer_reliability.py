"""Indexer reliability (audit findings D-05, D-08, D-09).

* One cycle at a time, whoever asks: the scheduler, a request from the page,
  or two callers at once.
* "Load N more days" is a request the background indexer carries out; the
  page gets an answer at once, and ten clicks are one request.
* A balance failure is not an indexing failure: the transfers that were read
  are kept and their watermark moves on.
* A reorg that happened while the app was down is caught when it comes back.
"""

from __future__ import annotations

import asyncio
import pathlib
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import AssetRef, TransferKind
from tests.conftest import (
    COLD,
    MAIN,
    PAYMENTS,
    STRANGER,
    FakePrices,
    FakeProvider,
    ProviderDown,
    address,
    make_config,
    make_settings,
    run,
    transfer,
    tx_hash,
)

SAME_ORIGIN = {"sec-fetch-site": "same-origin"}
TOKEN = AssetRef("ethereum", "0x5555555555555555555555555555555555555555", "TKN", 18)


class SlowProvider(FakeProvider):
    """A FakeProvider that can be slow, or fail, for one address."""

    def __init__(self, *args, delay: float = 0.0, **kwargs):
        super().__init__(*args, **kwargs)
        self.delay = delay
        self.transfer_delay = 0.0
        self.head_calls = 0
        self.fail_transfers_for: set[str] = set()
        self.fail_balances_for: set[str] = set()
        self.in_transfers = threading.Event()

    async def get_head_block(self) -> int:
        self.head_calls += 1
        await asyncio.sleep(self.delay)
        return await super().get_head_block()

    async def get_transfers(self, address, **kwargs):
        self.in_transfers.set()
        await asyncio.sleep(self.delay + self.transfer_delay)
        if address in self.fail_transfers_for:
            raise ProviderDown(f"transfers for {address[:8]} unavailable")
        return await super().get_transfers(address, **kwargs)

    async def get_native_balance(self, address):
        if address in self.fail_balances_for:
            raise ProviderDown(f"HTTP 503 for {address[:8]}")
        return await super().get_native_balance(address)


class CycleCounter:
    """Counts cycles in flight from the database's point of view: a cycle
    begins with record_attempt and ends with record_success or record_failure.
    It does not care how the indexer is written, so it reads the same against
    any version of it."""

    def __init__(self, db):
        self.active = 0
        self.peak = 0
        self.started = 0
        self._lock = threading.Lock()
        for name, delta in (("record_attempt", 1), ("record_success", -1), ("record_failure", -1)):
            original = getattr(db, name)
            setattr(db, name, self._wrap(original, delta))

    def _wrap(self, original, delta):
        def wrapped(*args, **kwargs):
            with self._lock:
                self.active += delta
                if delta > 0:
                    self.started += 1
                self.peak = max(self.peak, self.active)
            return original(*args, **kwargs)
        return wrapped


def wait_until(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached")


def make_app(tmp_path: pathlib.Path, provider, *, run_indexer=False, **settings):
    return create_app(
        settings=make_settings(tmp_path, **settings),
        config=make_config(),
        provider=provider,
        price_source=FakePrices(),
        run_indexer=run_indexer,
    )


@pytest.fixture
def provider():
    return SlowProvider(
        native_balances={MAIN: 4 * 10**18, COLD: 3 * 10**18, PAYMENTS: 10**18},
        transfers=[
            transfer("old-in", sender=STRANGER, recipient=MAIN, amount=10**18, block=21_000_010),
        ],
    )


# ------------------------------------------------------- 1-4: single flight


def test_two_simultaneous_requests_run_one_cycle(tmp_path, provider):
    provider.delay = 0.2
    app = make_app(tmp_path, provider)
    with TestClient(app):
        indexer = app.state.ctx.indexer
        counter = CycleCounter(app.state.ctx.db)

        async def both():
            return await asyncio.gather(indexer.run_once(), indexer.run_once())

        first, second = run(both())
    assert provider.head_calls == 1, "one cycle, not two"
    assert counter.started == 1 and counter.peak == 1
    assert first is second, "the second caller joined the first"


def test_the_scheduler_and_a_page_request_never_overlap(tmp_path, provider):
    provider.delay = 0.4
    app = make_app(tmp_path, provider, run_indexer=True)
    counter = CycleCounter(app.state.ctx.db)
    with TestClient(app) as client:
        wait_until(provider.in_transfers.is_set)  # the scheduler's first cycle is running
        started = time.monotonic()
        response = client.post("/history/older", headers=SAME_ORIGIN)
        elapsed = time.monotonic() - started
        assert response.status_code == 200
        assert elapsed < 0.3, f"the request waited for indexing ({elapsed:.2f}s)"
        wait_until(lambda: app.state.ctx.db.get_sync_status("ethereum").history_status
                   == "completed")
    assert counter.peak == 1, "never two cycles at once"
    assert counter.started == 2, "the scheduler's cycle, then one more for the request"


def test_an_exception_does_not_leave_the_indexer_locked(tmp_path, provider):
    app = make_app(tmp_path, provider)
    with TestClient(app):
        ctx = app.state.ctx
        real = ctx.db.list_accounts
        calls = {"n": 0}

        def broken(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("a bug in the middle of a cycle")
            return real(*args, **kwargs)

        ctx.db.list_accounts = broken
        with pytest.raises(RuntimeError):
            run(ctx.indexer.run_once())
        result = run(ctx.indexer.run_once())
        assert result.ok
        assert not ctx.indexer.busy


def test_shutdown_during_a_cycle_is_prompt_and_clean(tmp_path, provider):
    provider.transfer_delay = 30  # a provider that would hang the shutdown
    app = make_app(tmp_path, provider, run_indexer=True)
    client = TestClient(app)
    client.__enter__()
    wait_until(provider.in_transfers.is_set)
    started = time.monotonic()
    client.__exit__(None, None, None)
    assert time.monotonic() - started < 5, "shutdown waited for the provider"
    assert app.state.ctx.indexer.busy is False


def test_many_wakes_during_a_cycle_queue_one_more_not_many(tmp_path, provider):
    provider.delay = 0.3
    app = make_app(tmp_path, provider, run_indexer=True, sync_interval=3600)
    counter = CycleCounter(app.state.ctx.db)
    with TestClient(app):
        wait_until(provider.in_transfers.is_set)
        for _ in range(50):
            app.state.ctx.indexer.wake()
        time.sleep(1.5)
    assert counter.started == 2, "the running cycle, and one for all fifty wakes"


# --------------------------------------------- 5-7: older history requests


def test_ten_quick_clicks_are_one_request(tmp_path, provider):
    app = make_app(tmp_path, provider)
    with TestClient(app) as client:
        run(app.state.ctx.indexer.run_once())
        floor = app.state.ctx.db.history_floor("ethereum")
        timings, targets = [], set()
        for _ in range(10):
            started = time.monotonic()
            response = client.post("/history/older", headers=SAME_ORIGIN)
            timings.append(time.monotonic() - started)
            assert response.status_code == 200
            assert "reaching back to block" in response.text
            targets.add(app.state.ctx.db.get_sync_status("ethereum").requested_from_block)
        status = app.state.ctx.db.get_sync_status("ethereum")
        assert targets == {floor - app.state.ctx.settings.backfill_blocks}
        assert status.history_status == "queued"
        assert max(timings) < 0.5


def test_a_new_request_after_completion_goes_deeper(tmp_path, provider):
    app = make_app(tmp_path, provider)
    with TestClient(app) as client:
        db, window = app.state.ctx.db, app.state.ctx.settings.backfill_blocks
        run(app.state.ctx.indexer.run_once())
        client.post("/history/older", headers=SAME_ORIGIN)
        first = db.get_sync_status("ethereum").requested_from_block
        run(app.state.ctx.indexer.run_once())
        assert db.get_sync_status("ethereum").history_status == "completed"
        assert db.history_floor("ethereum") == first, "the indexer reached it"

        client.post("/history/older", headers=SAME_ORIGIN)
        status = db.get_sync_status("ethereum")
        assert status.requested_from_block == first - window
        assert status.history_status == "queued"


def test_a_failed_request_is_retried_for_the_same_block(tmp_path, provider):
    app = make_app(tmp_path, provider)
    with TestClient(app) as client:
        db = app.state.ctx.db
        run(app.state.ctx.indexer.run_once())
        client.post("/history/older", headers=SAME_ORIGIN)
        target = db.get_sync_status("ethereum").history_target
        provider.fail_transfers_for = {MAIN}
        run(app.state.ctx.indexer.run_once())
        status = db.get_sync_status("ethereum")
        assert status.history_status == "failed" and "unavailable" in status.history_error
        row = client.get("/history/older").text
        assert "Could not reach block" in row and "Try again now" in row

        client.post("/history/older", headers=SAME_ORIGIN)
        assert db.get_sync_status("ethereum").history_target == target, "not deeper"


def test_the_page_polls_until_done_then_shows_the_rows(tmp_path, provider):
    provider.transfers.append(
        transfer("ancient", sender=STRANGER, recipient=MAIN, amount=7 * 10**18,
                 block=21_000_100 - 300_000)
    )
    app = make_app(tmp_path, provider, backfill_days=30)
    with TestClient(app) as client:
        run(app.state.ctx.indexer.run_once())
        assert tx_hash("ancient") not in client.get("/api/activity").text

        queued = client.post("/history/older", headers=SAME_ORIGIN).text
        assert 'hx-trigger="every 3s"' in queued and "Queued" in queued
        assert 'hx-trigger="every 3s"' in client.get("/history/older").text, "still polling"
        # The browser may be closed now; the indexer does the work regardless.
        run(app.state.ctx.indexer.run_once())
        done = client.get("/history/older").text
        assert "every 3s" not in done
        assert "Load 30 more days" in done
        assert tx_hash("ancient") in client.get("/api/activity").text


def test_a_restart_during_a_history_request_resumes_it(tmp_path, provider):
    app = make_app(tmp_path, provider)
    with TestClient(app) as client:
        run(app.state.ctx.indexer.run_once())
        client.post("/history/older", headers=SAME_ORIGIN)
        app.state.ctx.db.history_started("ethereum")  # the process dies mid-way
        target = app.state.ctx.db.get_sync_status("ethereum").history_target

    restarted = make_app(tmp_path, provider)
    with TestClient(restarted) as client:
        status = restarted.state.ctx.db.get_sync_status("ethereum")
        assert (status.history_status, status.history_target) == ("queued", target)
        assert "Queued" in client.get("/activity").text, "the page shows it as pending"
        run(restarted.state.ctx.indexer.run_once())
        assert restarted.state.ctx.db.get_sync_status("ethereum").history_status == "completed"
        assert restarted.state.ctx.db.history_floor("ethereum") == target


# ----------------------------------------- 8-9, 12: balances and watermarks


def test_a_balance_failure_keeps_the_indexed_transfers_and_moves_on(tmp_path, provider):
    app = make_app(tmp_path, provider)
    with TestClient(app) as client:
        ctx = app.state.ctx
        run(ctx.indexer.run_once())
        known = {r["account_id"]: r["amount_raw"]
                 for rows in ctx.db.balances_by_account().values() for r in rows}

        provider.head_block += 50
        provider.transfers.append(
            transfer("new-in", sender=STRANGER, recipient=MAIN, amount=2 * 10**18,
                     block=provider.head_block - 1)
        )
        provider.native_balances[MAIN] = 6 * 10**18
        provider.fail_balances_for = {MAIN}
        result = run(ctx.indexer.run_once())

        status = ctx.db.get_sync_status("ethereum")
        assert result.ok and result.balance_error
        assert status.last_synced_block == provider.head_block, "the watermark moved"
        assert status.last_error is None
        assert "HTTP 503" in status.balances_error
        assert tx_hash("new-in") in client.get("/api/activity").text
        main_id = next(a.id for a in ctx.db.list_accounts() if a.address == MAIN)
        still = {r["account_id"]: r["amount_raw"]
                 for rows in ctx.db.balances_by_account().values() for r in rows}
        assert still[main_id] == known[main_id], "the last known balance, not zero"
        body = client.get("/status").text
        assert "balances not refreshed" in body
        api = client.get("/api/status").json()
        assert api["state"] != "error" and api["balances"]["error"]


def test_balances_are_retried_without_reading_history_again(tmp_path, provider):
    app = make_app(tmp_path, provider, balance_refresh=3600)
    with TestClient(app):
        ctx = app.state.ctx
        run(ctx.indexer.run_once())  # first cycle: a full sweep
        provider.head_block += 50
        provider.transfers.append(
            transfer("moves-main", sender=STRANGER, recipient=MAIN, amount=10**18,
                     block=provider.head_block - 1)
        )
        provider.fail_balances_for = {MAIN}
        provider.native_balances[MAIN] = 9 * 10**18
        run(ctx.indexer.run_once())
        failed_head = provider.head_block

        provider.fail_balances_for = set()
        provider.head_block += 10           # Main does not move again
        provider.calls.clear()
        provider.balance_calls.clear()
        result = run(ctx.indexer.run_once())

        assert result.ok and result.balance_error is None
        assert MAIN in provider.balance_calls, "the failed wallet is read again"
        assert min(c[3] for c in provider.calls) == failed_head - ctx.settings.reorg_depth + 1
        main_id = next(a.id for a in ctx.db.list_accounts() if a.address == MAIN)
        native = next(r for r in ctx.db.balances_by_account()[main_id]
                      if r["contract_address"] == "")
        assert int(native["amount_raw"]) == 9 * 10**18
        assert ctx.db.get_sync_status("ethereum").balances_error is None


def test_one_wallets_balance_failure_does_not_touch_the_others(tmp_path, provider):
    app = make_app(tmp_path, provider)
    with TestClient(app):
        ctx = app.state.ctx
        run(ctx.indexer.run_once())
        provider.native_balances[COLD] = 8 * 10**18
        provider.fail_balances_for = {MAIN}
        ctx.indexer._balances_at = None  # force a sweep
        result = run(ctx.indexer.run_once())
        assert "1 of 3 wallet(s)" in result.balance_error
        cold_id = next(a.id for a in ctx.db.list_accounts() if a.address == COLD)
        native = next(r for r in ctx.db.balances_by_account()[cold_id]
                      if r["contract_address"] == "")
        assert int(native["amount_raw"]) == 8 * 10**18, "Cold was updated"


def test_one_wallets_transfer_failure_corrupts_nothing(tmp_path, provider):
    app = make_app(tmp_path, provider)
    with TestClient(app) as client:
        ctx = app.state.ctx
        run(ctx.indexer.run_once())
        before = client.get("/api/activity").json()["activities"]
        watermark = ctx.db.get_sync_status("ethereum").last_synced_block

        provider.head_block += 20
        provider.transfers = [t for t in provider.transfers]  # unchanged on chain
        provider.fail_transfers_for = {COLD}
        result = run(ctx.indexer.run_once())

        assert not result.ok and "unavailable" in result.error
        assert client.get("/api/activity").json()["activities"] == before, "nothing pruned"
        assert ctx.db.get_sync_status("ethereum").last_synced_block == watermark
        assert {a.synced_to_block for a in ctx.db.list_accounts()} == {watermark}


# ------------------------------------------------------------- 10-11: reorgs


def reorg_app(tmp_path, provider):
    app = make_app(tmp_path, provider, reorg_depth=8)
    return app


def test_a_reorg_during_downtime_is_caught(tmp_path, provider):
    """D-09: the old prune window was the last REORG_DEPTH blocks before the
    new head, which after a downtime no longer contains what was re-scanned."""
    provider.head_block = 21_000_100
    orphans = [
        transfer("orphan-native", sender=STRANGER, recipient=MAIN, amount=10**18,
                 block=21_000_099),
        transfer("orphan-internal", sender=STRANGER, recipient=MAIN, amount=10**17,
                 kind=TransferKind.INTERNAL, trace="0_1", block=21_000_098),
        transfer("orphan-token", sender=STRANGER, recipient=COLD, amount=10**18, asset=TOKEN,
                 kind=TransferKind.TOKEN, log_index=4, block=21_000_097),
    ]
    survivor = transfer("still-there", sender=STRANGER, recipient=MAIN, amount=10**18,
                        block=21_000_096)
    provider.transfers = [*orphans, survivor]
    app = reorg_app(tmp_path, provider)
    with TestClient(app) as client:
        run(app.state.ctx.indexer.run_once())
        everything = {"dust": "show"}  # TKN is unverified, hidden by default
        assert len(client.get("/api/activity", params=everything).json()["activities"]) == 4

        provider.transfers = [survivor]  # reorged out while the app was down
        provider.head_block += 500
        result = run(app.state.ctx.indexer.run_once())

        rows = client.get("/api/activity", params=everything).json()["activities"]
        assert {a["tx_hash"] for a in rows} == {tx_hash("still-there")}
        assert result.pruned == 3


def test_the_prune_window_ends_where_the_rescan_began(tmp_path, provider):
    """At the boundary: an event in the first re-scanned block is pruned if it
    is gone; one just below it was not re-read and is kept."""
    provider.head_block = 21_000_100
    app = reorg_app(tmp_path, provider)
    depth = 8
    first_rescanned = provider.head_block - depth + 1
    edge = transfer("edge", sender=STRANGER, recipient=MAIN, amount=10**18, block=first_rescanned)
    below = transfer("below", sender=STRANGER, recipient=MAIN, amount=10**18,
                     block=first_rescanned - 1)
    provider.transfers = [edge, below]
    with TestClient(app) as client:
        run(app.state.ctx.indexer.run_once())
        provider.transfers = []          # both gone from what the provider reports
        provider.head_block += 1
        run(app.state.ctx.indexer.run_once())
        shown = {a["tx_hash"] for a in client.get("/api/activity").json()["activities"]}
        assert tx_hash("edge") not in shown
        assert tx_hash("below") in shown, "settled: never pruned"


def test_a_downtime_reorg_spares_a_removed_wallet(tmp_path, provider):
    """The S2 invariant holds across the wider window: a removed wallet is
    not scanned, so its events are not judged by the re-scan."""
    temp = address("temp-for-reorg")
    provider.head_block = 21_000_100
    provider.transfers = [
        transfer("temp-only", sender=STRANGER, recipient=temp, amount=10**18, block=21_000_099),
        transfer("main-to-temp", sender=MAIN, recipient=temp, amount=10**18, block=21_000_098),
    ]
    app = reorg_app(tmp_path, provider)
    with TestClient(app) as client:
        client.post("/wallets", headers=SAME_ORIGIN,
                    data={"name": "Temp", "address": temp, "chain": "ethereum", "owned": "on"})
        run(app.state.ctx.indexer.run_once())
        temp_id = next(a.id for a in app.state.ctx.db.list_accounts() if a.name == "Temp")
        client.post(f"/wallets/{temp_id}/remove", headers=SAME_ORIGIN)

        provider.transfers = []          # both reorged out, during a downtime
        provider.head_block += 500
        run(app.state.ctx.indexer.run_once())

        db = app.state.ctx.db.connect()
        kept = {row[0] for row in db.execute("SELECT tx_hash FROM activities")}
        assert tx_hash("temp-only") in kept, "the removed wallet's history is kept"
        assert tx_hash("main-to-temp") not in kept, "Main's side was re-scanned: pruned"


# ------------------------------------------------------------- migration


def test_migration_008_upgrades_an_existing_database(tmp_path):
    """A database at schema 7, with a sync state and a pending deeper request
    written by the previous version, upgrades in place."""
    import sqlite3

    from app.db import MIGRATIONS_DIR, Database

    path = tmp_path / "old.db"
    old = sqlite3.connect(path, isolation_level=None)
    old.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at INTEGER)")
    for sql in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = int(sql.name.split("_")[0])
        if version <= 7:
            old.executescript(sql.read_text())
            old.execute("INSERT INTO schema_migrations VALUES (?, 0)", (version,))
    old.execute(
        "INSERT INTO sync_state (chain_id, last_synced_block, head_block, last_error,"
        " backfill_done, requested_from_block) VALUES ('ethereum', 100, 100, 'boom', 1, 42)"
    )
    old.close()

    db = Database(path)
    assert db.migrate() == [8]
    status = db.get_sync_status("ethereum")
    assert (status.last_synced_block, status.last_error, status.requested_from_block) == (
        100, "boom", 42
    )
    assert status.history_status is None and status.balances_error is None
    # The new state is writable straight away.
    db.record_balances("ethereum", "HTTP 503")
    assert db.get_sync_status("ethereum").balances_error == "HTTP 503"
    assert db.migrate() == [], "applied once"
    db.close()
