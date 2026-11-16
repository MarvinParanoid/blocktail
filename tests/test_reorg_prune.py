"""Which stored events a cycle may prune as reorged out (S3.1).

An event may be deleted only when a scan that should have reported it did
not. That is decided per wallet: each active wallet is authoritative for the
blocks it re-scanned this cycle *within its own reorg window* — from
REORG_DEPTH below where it was last synced, to the head. A wallet's window does
not shrink because another wallet started elsewhere, and a wallet reading
blocks for the first time (new, or reaching back for older history) is reading
settled history and is authoritative for nothing.
"""

from __future__ import annotations

import pathlib

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import AssetRef, TransferKind
from tests.conftest import (
    COLD,
    MAIN,
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
TOKEN = AssetRef("ethereum", "0x6666666666666666666666666666666666666666", "TKN", 18)
DEPTH = 8
HEAD = 21_000_100
NEW = address("new-wallet")
OLD_FRIEND = address("re-added-wallet")


class Stand:
    def __init__(self, tmp_path: pathlib.Path, *, backfill_days: int = 90):
        self.provider = FakeProvider(head_block=HEAD)
        self.app = create_app(
            settings=make_settings(tmp_path, reorg_depth=DEPTH, backfill_days=backfill_days),
            config=make_config(wallets=[{"name": "Main", "address": MAIN},
                                        {"name": "Cold", "address": COLD}]),
            provider=self.provider,
            price_source=FakePrices(),
            run_indexer=False,
        )
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.db = self.app.state.ctx.db
        self.window = self.app.state.ctx.settings.backfill_blocks

    def close(self):
        self.client.__exit__(None, None, None)

    def cycle(self):
        result = run(self.app.state.ctx.indexer.run_once())
        assert result.ok, result.error
        return result

    def stored(self) -> set[str]:
        return {row[0] for row in self.db.connect().execute("SELECT tx_hash FROM activities")}

    def add(self, name: str, addr: str) -> int:
        self.client.post("/wallets", headers=SAME_ORIGIN,
                         data={"name": name, "address": addr, "chain": "ethereum", "owned": "on"})
        return next(a.id for a in self.db.list_accounts() if a.address == addr)

    def remove(self, account_id: int):
        self.client.post(f"/wallets/{account_id}/remove", headers=SAME_ORIGIN)


@pytest.fixture
def stand(tmp_path):
    s = Stand(tmp_path)
    yield s
    s.close()


def t(seed, sender, recipient, block, **kw):
    return transfer(seed, sender=sender, recipient=recipient, amount=10**18, block=block, **kw)


# ------------------------------------------------- the reported defect


def test_a_new_wallet_does_not_narrow_an_existing_wallets_reorg_check(stand):
    """Main has been indexed for a long time. The app is down for longer than
    the backfill window; meanwhile a reorg drops one of Main's recent
    transfers. A wallet is added, whose first scan starts at the backfill
    window — far above where Main's re-scan starts. Main's orphan must go."""
    orphan = t("main-orphan", STRANGER, MAIN, HEAD - 1)
    kept = t("main-kept", STRANGER, MAIN, HEAD - 2)
    stand.provider.transfers = [orphan, kept]
    stand.cycle()
    temp = stand.add("Temp", address("temp-s3a"))
    stand.provider.transfers.append(t("temp-only", STRANGER, address("temp-s3a"), HEAD - 3))
    stand.cycle()
    stand.remove(temp)

    stand.provider.head_block = HEAD + 2 * stand.window   # a long downtime
    new_tx = t("new-wallet-in", STRANGER, NEW, stand.provider.head_block - 5)
    stand.provider.transfers = [kept, new_tx]             # the orphan was reorged out
    stand.add("New", NEW)
    result = stand.cycle()

    stored = stand.stored()
    assert tx_hash("main-orphan") not in stored, "Main's re-scan saw it gone"
    assert tx_hash("main-kept") in stored
    assert tx_hash("new-wallet-in") in stored, "the new wallet's history is indexed"
    assert tx_hash("temp-only") in stored, "a removed wallet's history is kept"
    assert result.pruned == 1


# ---------------------------------------------------- windows per wallet


def test_each_wallet_is_judged_over_its_own_window(stand):
    """A re-added wallet reaches back to its old watermark; Main does not. An
    event of Main's below Main's window is out of reach even though the scan
    of the re-added wallet started lower."""
    friend_old = t("friend-old", STRANGER, OLD_FRIEND, HEAD - 1)
    stand.provider.transfers = [friend_old]
    friend = stand.add("Friend", OLD_FRIEND)
    stand.cycle()
    stand.remove(friend)

    stand.provider.head_block = HEAD + 200
    main_settled = t("main-settled", STRANGER, MAIN, HEAD + 100)   # below Main's next window
    stand.provider.transfers = [friend_old, main_settled]
    stand.cycle()
    stand.provider.head_block = HEAD + 300
    stand.provider.transfers = [main_settled]   # friend-old reorged; main-settled "vanishes" next
    stand.add("Friend", OLD_FRIEND)             # back, watermark at HEAD
    stand.provider.transfers = []               # neither is reported now
    stand.cycle()

    stored = stand.stored()
    assert tx_hash("friend-old") not in stored, "inside the re-added wallet's window"
    assert tx_hash("main-settled") in stored, "below Main's window: never re-checked, kept"


def test_a_new_wallet_is_authoritative_for_nothing(stand):
    """A shared event below Main's window, inside the new wallet's first scan,
    that the provider does not return: settled history read for the first
    time, not a reorg. Kept."""
    shared_old = t("shared-old", MAIN, NEW, HEAD - 50)
    stand.provider.transfers = [shared_old]
    stand.cycle()
    stand.provider.head_block = HEAD + 100
    stand.provider.transfers = []   # a provider gap, not a reorg
    stand.add("New", NEW)
    stand.cycle()
    assert tx_hash("shared-old") in stand.stored()


# ------------------------------------------------------------ shared events


def test_a_shared_event_is_kept_while_any_scan_reports_it(stand):
    shared = t("main-to-cold", MAIN, COLD, HEAD - 1)
    stand.provider.transfers = [shared]
    stand.cycle()
    stand.provider.head_block += 1
    stand.cycle()
    assert tx_hash("main-to-cold") in stand.stored()


def test_a_shared_event_gone_from_the_chain_is_pruned(stand):
    stand.provider.transfers = [t("main-to-cold", MAIN, COLD, HEAD - 1)]
    stand.cycle()
    stand.provider.transfers = []
    stand.provider.head_block += 1
    assert stand.cycle().pruned == 1
    assert tx_hash("main-to-cold") not in stand.stored()


def test_a_shared_event_with_a_removed_wallet_is_judged_by_the_active_end(stand):
    temp = stand.add("Temp", NEW)
    stand.provider.transfers = [t("main-to-temp", MAIN, NEW, HEAD - 1),
                                t("temp-only", STRANGER, NEW, HEAD - 2)]
    stand.cycle()
    stand.remove(temp)
    stand.provider.transfers = []
    stand.provider.head_block += 400
    stand.cycle()
    stored = stand.stored()
    assert tx_hash("main-to-temp") not in stored, "Main re-scanned it"
    assert tx_hash("temp-only") in stored, "nobody active is responsible for it"


# ------------------------------------------------------ kinds and repeats


def test_internal_and_token_orphans_are_pruned_and_survivors_kept(stand):
    stand.provider.transfers = [
        t("int-orphan", STRANGER, MAIN, HEAD - 1, kind=TransferKind.INTERNAL, trace="0_2"),
        t("tok-orphan", STRANGER, COLD, HEAD - 1, kind=TransferKind.TOKEN, asset=TOKEN,
          log_index=3),
        t("int-kept", STRANGER, MAIN, HEAD - 2, kind=TransferKind.INTERNAL, trace="0_1"),
        t("tok-kept", STRANGER, COLD, HEAD - 2, kind=TransferKind.TOKEN, asset=TOKEN,
          log_index=1),
    ]
    stand.cycle()
    stand.provider.transfers = [x for x in stand.provider.transfers
                                if x.tx_hash in (tx_hash("int-kept"), tx_hash("tok-kept"))]
    stand.provider.head_block += 1
    assert stand.cycle().pruned == 2
    stored = stand.stored()
    assert {tx_hash("int-kept"), tx_hash("tok-kept")} <= stored
    assert not {tx_hash("int-orphan"), tx_hash("tok-orphan")} & stored


def test_a_cycle_with_no_new_blocks_changes_nothing(stand):
    stand.provider.transfers = [t("a", STRANGER, MAIN, HEAD - 1), t("b", MAIN, COLD, HEAD - 3)]
    stand.cycle()
    before = stand.stored()
    for _ in range(3):
        assert stand.cycle().pruned == 0
    assert stand.stored() == before


def test_a_failed_fetch_prunes_nothing(stand):
    stand.provider.transfers = [t("a", STRANGER, MAIN, HEAD - 1)]
    stand.cycle()
    stand.provider.transfers = []
    stand.provider.fail_transfers_with = ProviderDown("one request failed")
    stand.provider.head_block += 1
    result = run(stand.app.state.ctx.indexer.run_once())
    assert not result.ok and result.pruned == 0
    assert tx_hash("a") in stand.stored()


def test_reaching_back_for_older_history_prunes_nothing_down_there(tmp_path):
    """A deep history request re-reads settled blocks; a provider that has
    stopped serving them must not erase what was indexed."""
    s = Stand(tmp_path, backfill_days=1)
    try:
        deep = HEAD - 3 * s.window
        s.provider.transfers = [t("deep-old", STRANGER, MAIN, deep + 10)]
        s.db.request_history("ethereum", 4 * s.window)
        s.cycle()                      # reaches back and stores it
        assert tx_hash("deep-old") in s.stored()
        s.provider.transfers = []      # the provider no longer serves it
        s.db.request_history("ethereum", s.window)
        s.provider.head_block += 1
        s.cycle()
        assert tx_hash("deep-old") in s.stored()
    finally:
        s.close()
