"""A removed wallet is removed (audit findings D-02, D-07).

Removing a wallet stops watching it and takes its history out of every view —
the feed, the filters, the inspector, the sidebar's judgements — while every
indexed event stays in SQLite, so re-adding the address brings it all back
without fetching it again.

An event with one active end stays, read from that end. An event whose ends
are all inactive is not shown anywhere, and is not pruned either.
"""

from __future__ import annotations

import json
import pathlib

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
    address,
    make_config,
    make_settings,
    run,
    transfer,
    tx_hash,
)

TEMP = address("temp-wallet")
TEMP2 = address("temp-wallet-2")
TOKEN = AssetRef("ethereum", "0x4444444444444444444444444444444444444444", "ONLYTEMP", 18)
SAME_ORIGIN = {"sec-fetch-site": "same-origin"}

TRANSFERS = [
    # Only TEMP is monitored.
    transfer("temp-in", sender=STRANGER, recipient=TEMP, amount=7 * 10**18, block=21_000_010),
    # Active Main -> TEMP.
    transfer("main-to-temp", sender=MAIN, recipient=TEMP, amount=2 * 10**18, block=21_000_020),
    # TEMP -> active Cold.
    transfer("temp-to-cold", sender=TEMP, recipient=COLD, amount=3 * 10**18, block=21_000_030),
    # TEMP -> TEMP2, both added from the page.
    transfer("temp-to-temp2", sender=TEMP, recipient=TEMP2, amount=4 * 10**18, block=21_000_040),
    # A token only TEMP ever sent.
    transfer("temp-sends-token", sender=TEMP, recipient=STRANGER, amount=10**18, asset=TOKEN,
             kind=TransferKind.TOKEN, log_index=2, block=21_000_050),
    # Main -> Cold, both config wallets: a pair inside the scope.
    transfer("main-to-cold", sender=MAIN, recipient=COLD, amount=5 * 10**18, block=21_000_060),
    # Only Payments is monitored (a config wallet).
    transfer("payments-in", sender=STRANGER, recipient=PAYMENTS, amount=6 * 10**18,
             block=21_000_070),
]


class Instance:
    """One app over a database that outlives it, so restarts can be tested."""

    def __init__(self, tmp_path: pathlib.Path, provider: FakeProvider):
        self.tmp_path = tmp_path
        self.provider = provider
        self.client: TestClient | None = None

    def start(self, **config) -> TestClient:
        self.stop()
        app = create_app(
            settings=make_settings(self.tmp_path),
            config=make_config(**config),
            provider=self.provider,
            price_source=FakePrices(),
            run_indexer=False,
        )
        self.client = TestClient(app)
        self.client.__enter__()
        self.client.ctx = app.state.ctx
        return self.client

    def stop(self):
        if self.client is not None:
            self.client.__exit__(None, None, None)
            self.client = None

    def sync(self):
        self.provider.head_block += 1
        return run(self.client.ctx.indexer.run_once())


@pytest.fixture
def instance(tmp_path):
    provider = FakeProvider(transfers=list(TRANSFERS), head_block=21_000_100)
    inst = Instance(tmp_path, provider)
    yield inst
    inst.stop()


def add(client, name: str, addr: str, *, owned: bool = True) -> int:
    data = {"name": name, "address": addr, "chain": "ethereum"}
    if owned:
        data["owned"] = "on"
    assert client.post("/wallets", data=data, headers=SAME_ORIGIN).status_code == 200
    return next(a["id"] for a in client.get("/api/accounts").json()["accounts"]
                if a["address"].lower() == addr)


def remove(client, account_id: int):
    assert client.post(f"/wallets/{account_id}/remove", headers=SAME_ORIGIN).status_code == 200


def feed(client, **params) -> dict[str, dict]:
    """tx hash -> activity, everything the JSON feed shows (dust included)."""
    rows = client.get("/api/activity", params={"dust": "show", **params}).json()["activities"]
    return {row["tx_hash"]: row for row in rows}


def stored(client) -> int:
    return client.ctx.db.connect().execute("SELECT COUNT(*) FROM activities").fetchone()[0]


def activity_id(client, seed: str) -> int:
    return client.ctx.db.connect().execute(
        "SELECT id FROM activities WHERE tx_hash = ?", (tx_hash(seed),)
    ).fetchone()[0]


@pytest.fixture
def removed(instance):
    """TEMP and TEMP2 added from the page, indexed, then both removed."""
    client = instance.start()
    temp = add(client, "Temp", TEMP)
    temp2 = add(client, "Temp2", TEMP2, owned=False)
    instance.sync()
    assert tx_hash("temp-in") in feed(client), "precondition: indexed while watched"
    remove(client, temp)
    remove(client, temp2)
    return instance, temp, temp2


# ------------------------------------------------------------- hidden


def test_a_removed_wallets_history_is_hidden(removed):
    instance, temp, _ = removed
    client = instance.client
    shown = feed(client)
    assert tx_hash("temp-in") not in shown
    assert tx_hash("temp-sends-token") not in shown
    assert not any(row["account"] in ("Temp", "Temp2") for row in shown.values())
    assert "Temp" not in client.get("/activity", params={"dust": "show"}).text
    assert feed(client, wallet=temp) == {}, "its own id no longer selects anything"


def test_nothing_is_deleted(removed):
    instance, _, _ = removed
    assert stored(instance.client) == len(TRANSFERS), "every indexed event stays in SQLite"
    for seed in ("temp-in", "temp-to-temp2", "temp-sends-token"):
        activity_id(instance.client, seed)  # raises if gone


def test_an_event_between_two_removed_wallets_is_not_shown_anywhere(removed):
    instance, _, _ = removed
    client = instance.client
    assert tx_hash("temp-to-temp2") not in feed(client)
    assert client.get(f"/tx/{activity_id(client, 'temp-to-temp2')}").status_code == 404
    assert client.get(f"/tx/{activity_id(client, 'temp-in')}").status_code == 404


def test_a_removed_wallets_only_asset_leaves_the_filter(removed):
    instance, _, _ = removed
    symbols = [row["symbol"] for row in instance.client.ctx.db.feed_assets()]
    assert "ONLYTEMP" not in symbols


# ------------------------------------------------- one end still active


def test_active_to_removed_reads_from_the_active_end(removed):
    instance, _, _ = removed
    row = feed(instance.client)[tx_hash("main-to-temp")]
    assert (row["account"], row["direction"]) == ("Main", "out")
    assert row["to"]["monitored"] is False
    assert row["to"]["label"] is None, "the removed name is not shown as a label"


def test_removed_to_active_reads_from_the_active_end(removed):
    instance, _, _ = removed
    row = feed(instance.client)[tx_hash("temp-to-cold")]
    assert (row["account"], row["direction"]) == ("Cold", "in")
    assert row["from"]["monitored"] is False


def test_a_removed_wallet_does_not_count_in_in_or_out(removed):
    instance, _, _ = removed
    client = instance.client
    inbound = feed(client, direction="in")
    outbound = feed(client, direction="out")
    assert tx_hash("temp-in") not in inbound
    assert tx_hash("main-to-temp") not in inbound, "Temp is not one of mine any more"
    assert tx_hash("main-to-temp") in outbound
    assert tx_hash("temp-to-cold") in inbound
    assert tx_hash("temp-to-cold") not in outbound


def test_a_removed_wallet_vouches_for_nothing(removed):
    instance, _, _ = removed
    db = instance.client.ctx.db
    token_id = db.connect().execute(
        "SELECT id FROM assets WHERE contract_address = ?", (TOKEN.contract_address,)
    ).fetchone()[0]
    assert token_id not in db.sent_asset_ids()


def test_a_removed_watched_wallet_leaves_the_watching_scope(removed):
    instance, _, _ = removed
    assert feed(instance.client, scope="watched") == {}


def test_removing_one_end_of_a_pair_unfolds_it(instance):
    client = instance.start()
    instance.sync()
    pair = feed(client)[tx_hash("main-to-cold")]
    assert pair["direction"] == "between"

    instance.start(wallets=[{"name": "Main", "address": MAIN},
                            {"name": "Payments", "address": PAYMENTS}])
    row = feed(instance.client)[tx_hash("main-to-cold")]
    assert (row["account"], row["direction"]) == ("Main", "out")
    assert row["to"]["monitored"] is False


# ----------------------------------------------------------------- prune


def test_prune_keeps_a_removed_wallets_recent_history(instance):
    """D-07: the re-scan window only holds what active wallets reported, so an
    event of a removed wallet inside it used to be deleted as reorged out."""
    instance.provider.transfers.append(
        transfer("temp-recent", sender=STRANGER, recipient=TEMP, amount=10**18, block=21_000_099)
    )
    client = instance.start()
    temp = add(client, "Temp", TEMP)
    instance.sync()
    before = stored(client)
    remove(client, temp)
    result = instance.sync()
    assert result.pruned == 0
    assert stored(client) == before
    activity_id(client, "temp-recent")


def test_prune_still_removes_a_reorged_event_of_an_active_wallet(instance):
    instance.provider.transfers.append(
        transfer("main-orphan", sender=STRANGER, recipient=MAIN, amount=10**18, block=21_000_099)
    )
    instance.start()
    instance.sync()
    instance.provider.transfers = [t for t in instance.provider.transfers
                                   if t.tx_hash != tx_hash("main-orphan")]
    assert instance.sync().pruned == 1


# ------------------------------------------------------------- re-adding


def test_re_adding_restores_history_without_a_new_backfill(removed):
    instance, temp, _ = removed
    client = instance.client
    watermark = client.ctx.db.connect().execute(
        "SELECT synced_to_block FROM accounts WHERE id = ?", (temp,)
    ).fetchone()[0]

    again = add(client, "Temporary, renamed", TEMP)
    assert again == temp, "the same account comes back"
    row = feed(client)[tx_hash("temp-in")]
    assert row["account"] == "Temporary, renamed"
    assert feed(client)[tx_hash("main-to-temp")]["direction"] == "between"

    instance.provider.calls.clear()
    instance.sync()
    starts = [c[3] for c in instance.provider.calls if c[1] == TEMP]
    assert starts and min(starts) > watermark - 64, "resumes, does not re-read the window"


# ---------------------------------------------- config file and restarts


def test_removing_a_config_wallet_hides_its_history(instance):
    client = instance.start()
    instance.sync()
    assert tx_hash("payments-in") in feed(client)
    before = stored(client)

    client = instance.start(wallets=[{"name": "Main", "address": MAIN},
                                     {"name": "Cold", "address": COLD}])
    assert tx_hash("payments-in") not in feed(client)
    assert "Payments" not in client.get("/activity").text
    assert stored(client) == before


def test_restoring_a_config_wallet_restores_its_history(instance):
    instance.start()
    instance.sync()
    instance.start(wallets=[{"name": "Main", "address": MAIN}, {"name": "Cold", "address": COLD}])
    instance.sync()

    instance.provider.calls.clear()
    client = instance.start()  # Payments is back in the file
    row = feed(client)[tx_hash("payments-in")]
    assert row["account"] == "Payments"
    instance.sync()
    starts = [c[3] for c in instance.provider.calls if c[1] == PAYMENTS]
    assert starts and min(starts) > 21_000_000, "no second backfill"


def test_a_restart_does_not_bring_a_removed_wallet_back(removed):
    instance, _, _ = removed
    client = instance.start()
    shown = feed(client)
    assert tx_hash("temp-in") not in shown
    assert feed(client)[tx_hash("main-to-temp")]["direction"] == "out"
    assert not any(a["name"] == "Temp" for a in client.get("/api/accounts").json()["accounts"])


def test_the_hidden_count_ignores_a_removed_wallet(removed):
    """The "N noisy transfers hidden" note counts with the same rule."""
    instance, _, _ = removed
    client = instance.client
    # temp-in, temp-to-temp2 and temp-sends-token have no active end.
    assert client.ctx.db.count_activities() == len(TRANSFERS) - 3
    assert len(feed(client)) == len(TRANSFERS) - 3


def test_the_inspector_leaves_out_a_removed_wallets_transfers(instance):
    """One transaction, two transfers: Main pays out, and something reaches
    Temp. After Temp is removed the transaction is still Main's, and only
    Main's transfer is listed in it."""
    instance.provider.transfers += [
        transfer("swap", sender=MAIN, recipient=STRANGER, amount=10**18, block=21_000_080),
        transfer("swap", sender=STRANGER, recipient=TEMP, amount=10**21, asset=TOKEN,
                 kind=TransferKind.TOKEN, log_index=9, block=21_000_080),
    ]
    client = instance.start()
    temp = add(client, "Temp", TEMP)
    instance.sync()
    remove(client, temp)

    main_row = feed(client)[tx_hash("swap")]
    inspector = client.get(f"/tx/{main_row['id']}")
    assert inspector.status_code == 200
    assert "ONLYTEMP" not in inspector.text
    assert "Temp" not in inspector.text


def test_removing_tells_the_page_to_drop_the_wallet_from_the_feed(instance):
    client = instance.start()
    temp = add(client, "Temp", TEMP)
    response = client.post(f"/wallets/{temp}/remove", headers=SAME_ORIGIN)
    # One event, with what happened and to which wallet. The order of what
    # follows — clear the selection, then the feed, then the summary and the
    # sidebar — is decided by its one handler on the page (S7), not by the
    # order of keys in this header.
    trigger = json.loads(response.headers["HX-Trigger"])
    assert trigger == {"wallet-changed": {"action": "removed", "id": temp}}
