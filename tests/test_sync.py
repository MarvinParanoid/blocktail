"""Indexer behaviour: normalization, identity, idempotency, reorgs, failure."""

from __future__ import annotations

import time
from dataclasses import replace

import pytest

from app.main import build_context
from app.models import TransferKind
from tests.conftest import (
    COLD,
    MAIN,
    STRANGER,
    USDC,
    ProviderDown,
    make_config,
    run,
    transfer,
)


def test_indexes_every_supported_transfer_kind(app_ctx, provider, sample_transfers):
    provider.transfers = sample_transfers
    result = run(app_ctx.indexer.run_once())

    assert result.ok, result.error
    assert result.stored == 5
    kinds = {
        row["kind"]
        for row in app_ctx.db.connect().execute("SELECT DISTINCT kind FROM activities")
    }
    assert kinds == {"native", "token", "internal"}


def test_wallet_to_wallet_transfer_is_a_single_activity(app_ctx, provider, sample_transfers):
    provider.transfers = sample_transfers
    run(app_ctx.indexer.run_once())

    rows = app_ctx.db.connect().execute(
        "SELECT from_account_id, to_account_id FROM activities WHERE amount_raw = ?",
        (str(5_000_000_000_000_000_000),),
    ).fetchall()

    assert len(rows) == 1, "a transfer between two monitored wallets must not be stored twice"
    assert rows[0]["from_account_id"] is not None
    assert rows[0]["to_account_id"] is not None

    from app.web.format import build_activity

    feed = app_ctx.db.query_activities(limit=50)
    view = next(v for v in (build_activity(r, app_ctx.chain) for r in feed) if v.direction == "between")
    assert view.source.display == "Main"
    assert view.target.display == "Cold"
    assert view.counterparty is None


def test_sync_is_idempotent(app_ctx, provider, sample_transfers):
    provider.transfers = sample_transfers

    first = run(app_ctx.indexer.run_once())
    second = run(app_ctx.indexer.run_once())
    third = run(app_ctx.indexer.run_once())

    assert first.stored == 5
    assert second.stored == 0
    assert third.stored == 0
    assert app_ctx.db.activity_count() == 5


def test_restart_does_not_duplicate_events(settings, watch_config, provider, sample_transfers):
    provider.transfers = sample_transfers

    first = build_context(settings, watch_config, provider=provider)
    first.db.migrate()
    first.db.apply_watch_config(watch_config)
    first.db.relink_accounts()
    run(first.indexer.run_once())
    count_before = first.db.activity_count()
    first.db.close()

    # A fresh process against the same file: migrate, re-apply config, re-index.
    second = build_context(settings, watch_config, provider=provider)
    second.db.migrate()
    second.db.apply_watch_config(watch_config)
    second.db.relink_accounts()
    result = run(second.indexer.run_once())

    assert result.stored == 0
    assert second.db.activity_count() == count_before == 5
    second.db.close()


def test_same_transaction_with_several_transfers_stays_distinct(app_ctx, provider):
    """One transaction, three ERC-20 transfers: three activities, not one."""
    provider.transfers = [
        transfer(
            "multi",
            sender=STRANGER,
            recipient=MAIN,
            amount=100_000_000,
            asset=USDC,
            kind=TransferKind.TOKEN,
            log_index=index,
            block=21_000_010,
        )
        for index in (1, 2, 3)
    ]
    result = run(app_ctx.indexer.run_once())

    assert result.stored == 3
    hashes = {row["tx_hash"] for row in app_ctx.db.connect().execute("SELECT tx_hash FROM activities")}
    assert len(hashes) == 1, "the fixture is meant to be one transaction"

    run(app_ctx.indexer.run_once())
    assert app_ctx.db.activity_count() == 3


def test_provider_failure_records_error_and_holds_the_watermark(app_ctx, provider, sample_transfers):
    provider.transfers = sample_transfers
    run(app_ctx.indexer.run_once())
    watermark = app_ctx.db.get_sync_status("ethereum").last_synced_block

    provider.fail_with = ProviderDown("upstream is down")
    result = run(app_ctx.indexer.run_once())

    assert not result.ok
    status = app_ctx.db.get_sync_status("ethereum")
    assert "upstream is down" in status.last_error
    assert status.last_synced_block == watermark, "a failed cycle must not claim progress"
    assert app_ctx.db.activity_count() == 5, "existing history must survive an outage"


def test_transfer_fetch_failure_does_not_prune(app_ctx, provider, sample_transfers):
    provider.transfers = sample_transfers
    run(app_ctx.indexer.run_once())

    provider.fail_transfers_with = ProviderDown("rate limited")
    result = run(app_ctx.indexer.run_once())

    assert not result.ok
    assert app_ctx.db.activity_count() == 5


def test_recovery_after_failure(app_ctx, provider, sample_transfers):
    provider.transfers = sample_transfers
    provider.fail_with = ProviderDown("down")
    assert not run(app_ctx.indexer.run_once()).ok

    provider.fail_with = None
    result = run(app_ctx.indexer.run_once())

    assert result.ok
    assert app_ctx.db.get_sync_status("ethereum").last_error is None
    assert app_ctx.db.activity_count() == 5


def test_reorged_out_event_is_pruned_inside_the_window(app_ctx, provider, sample_transfers):
    near_head = transfer(
        "reorg-victim", sender=STRANGER, recipient=MAIN, amount=10**18, block=21_000_095
    )
    provider.transfers = [*sample_transfers, near_head]
    run(app_ctx.indexer.run_once())
    assert app_ctx.db.activity_count() == 6

    # The chain no longer reports it: it was in a block that got reorged away.
    provider.transfers = sample_transfers
    result = run(app_ctx.indexer.run_once())

    assert result.pruned == 1
    assert app_ctx.db.activity_count() == 5


def test_events_below_the_reorg_window_are_never_pruned(app_ctx, provider, sample_transfers):
    provider.transfers = sample_transfers
    run(app_ctx.indexer.run_once())

    # Simulate a provider that stops reporting old history (pagination limits,
    # a different backend). Settled history must not be deleted.
    provider.transfers = []
    result = run(app_ctx.indexer.run_once())

    assert result.pruned == 0
    assert app_ctx.db.activity_count() == 5


def test_adding_a_wallet_later_attributes_existing_history(settings, provider, sample_transfers):
    provider.transfers = sample_transfers
    partial = make_config(wallets=[{"name": "Main", "address": MAIN}])

    ctx = build_context(settings, partial, provider=provider)
    ctx.db.migrate()
    ctx.db.apply_watch_config(partial)
    ctx.db.relink_accounts()
    run(ctx.indexer.run_once())

    # The MAIN -> COLD transfer is currently an outgoing transfer to a stranger.
    rows = ctx.db.query_activities(limit=50)
    assert all(row["to_account_name"] != "Cold" for row in rows)
    ctx.db.close()

    full = make_config()
    ctx = build_context(settings, full, provider=provider)
    ctx.db.apply_watch_config(full)
    ctx.db.relink_accounts()

    rows = ctx.db.query_activities(limit=50)
    linked = [row for row in rows if row["from_account_name"] == "Main" and row["to_account_name"] == "Cold"]
    assert len(linked) == 1, "adding a wallet must re-attribute history already indexed"
    ctx.db.close()


def test_removing_a_wallet_keeps_its_history(app_ctx, provider, sample_transfers):
    provider.transfers = sample_transfers
    run(app_ctx.indexer.run_once())

    reduced = make_config(wallets=[{"name": "Main", "address": MAIN}])
    app_ctx.db.apply_watch_config(reduced)

    assert app_ctx.db.activity_count() == 5
    assert [a.name for a in app_ctx.db.list_accounts()] == ["Main"]
    assert len(app_ctx.db.list_accounts(active_only=False)) == 3


def test_ignored_assets_are_skipped(settings, provider, sample_transfers):
    provider.transfers = sample_transfers
    config = make_config(ignore_assets=[USDC.contract_address])

    ctx = build_context(settings, config, provider=provider)
    ctx.db.migrate()
    ctx.db.apply_watch_config(config)
    ctx.db.relink_accounts()
    run(ctx.indexer.run_once())

    symbols = {row["symbol"] for row in ctx.db.query_activities(limit=50)}
    assert "USDC" not in symbols
    balances = ctx.db.balances_by_account()
    assert all(row["symbol"] != "USDC" for rows in balances.values() for row in rows)
    ctx.db.close()


def test_balances_are_refreshed_and_replaced(app_ctx, provider, sample_transfers):
    provider.transfers = sample_transfers
    run(app_ctx.indexer.run_once())

    balances = app_ctx.db.balances_by_account()
    accounts = {a.name: a.id for a in app_ctx.db.list_accounts()}
    main = balances[accounts["Main"]]
    assert main[0]["contract_address"] == "", "native balance sorts first"
    assert main[0]["amount_raw"] == str(4_821_000_000_000_000_000)

    provider.native_balances[MAIN] = 1_000_000_000_000_000_000
    provider.token_balances[MAIN] = []
    run(app_ctx.indexer.run_once())

    refreshed = app_ctx.db.balances_by_account()[accounts["Main"]]
    assert len(refreshed) == 1, "stale token balances must be replaced, not merged"
    assert refreshed[0]["amount_raw"] == str(1_000_000_000_000_000_000)


def test_balance_failure_is_surfaced(app_ctx, provider, sample_transfers):
    provider.transfers = sample_transfers
    provider.fail_balances_with = ProviderDown("balance endpoint down")
    result = run(app_ctx.indexer.run_once())

    assert not result.ok
    assert "balance" in app_ctx.db.get_sync_status("ethereum").last_error


def test_backfill_window_then_incremental(app_ctx, provider, sample_transfers, settings):
    provider.transfers = sample_transfers
    result = run(app_ctx.indexer.run_once())
    assert result.backfill is True
    assert result.from_block == provider.head_block - settings.backfill_blocks

    provider.head_block += 5
    second = run(app_ctx.indexer.run_once())
    assert second.backfill is False
    assert second.from_block == provider.head_block - 5 - settings.reorg_depth + 1


def test_no_wallets_configured_is_not_an_error(settings, provider):
    config = make_config(wallets=[{"name": "Main", "address": MAIN}])
    ctx = build_context(settings, config, provider=provider)
    ctx.db.migrate()
    ctx.db.apply_watch_config(config)
    ctx.db.connect().execute("UPDATE accounts SET active = 0")

    result = run(ctx.indexer.run_once())
    assert result.ok
    ctx.db.close()


# ------------------------------------------------------------------ prices


def _priced_ctx(settings, provider, prices, sample_transfers, config=None):
    from app.main import build_context

    config = config or make_config()
    provider.transfers = sample_transfers
    ctx = build_context(settings, config, provider=provider, price_source=prices)
    ctx.db.migrate()
    ctx.db.apply_watch_config(config)
    ctx.db.relink_accounts()
    return ctx


def test_prices_are_stored_for_held_assets(settings, provider, prices, sample_transfers):
    ctx = _priced_ctx(settings, provider, prices, sample_transfers)
    result = run(ctx.indexer.run_once())

    assert result.ok
    assert result.prices_updated == 2  # ETH and USDC; the spam token is unquoted
    stored = ctx.db.prices_by_asset()
    assert stored, "prices must be persisted, not held only in memory"
    assert any(row["price"] == "3120.55" for row in stored.values())
    assert all(row["currency"] == "USD" for row in stored.values())
    ctx.db.close()


def test_prices_refresh_on_their_own_slower_cadence(settings, provider, prices, sample_transfers):
    ctx = _priced_ctx(settings, provider, prices, sample_transfers)
    run(ctx.indexer.run_once())
    assert prices.calls == 1

    run(ctx.indexer.run_once())
    run(ctx.indexer.run_once())
    assert prices.calls == 1, "activity syncs must not drag a price call along each time"

    ctx.indexer._priced_at = None  # as if it had never run
    run(ctx.indexer.run_once())
    assert prices.calls == 2
    ctx.db.close()


def test_a_price_failure_never_fails_the_cycle(settings, provider, prices, sample_transfers):
    """Activity is the product; valuation is a convenience."""
    prices.fail_with = ProviderDown("prices unavailable")
    ctx = _priced_ctx(settings, provider, prices, sample_transfers)
    result = run(ctx.indexer.run_once())

    assert result.ok, "on-chain indexing must survive a pricing outage"
    assert result.stored == 5
    assert ctx.db.get_sync_status("ethereum").last_error is None
    assert ctx.db.prices_by_asset() == {}
    ctx.db.close()


def test_a_price_outage_keeps_the_last_known_prices(settings, provider, prices, sample_transfers):
    ctx = _priced_ctx(settings, provider, prices, sample_transfers)
    run(ctx.indexer.run_once())
    before = ctx.db.prices_by_asset()

    prices.fail_with = ProviderDown("down")
    ctx.indexer._priced_at = None
    run(ctx.indexer.run_once())

    after = ctx.db.prices_by_asset()
    assert {k: v["price"] for k, v in after.items()} == {k: v["price"] for k, v in before.items()}
    ctx.db.close()


def test_running_without_a_price_source_is_fine(app_ctx, provider, sample_transfers):
    provider.transfers = sample_transfers
    assert app_ctx.indexer.price_source is None
    result = run(app_ctx.indexer.run_once())

    assert result.ok and result.prices_updated == 0
    assert app_ctx.db.prices_by_asset() == {}


def test_only_held_assets_are_priced(settings, provider, prices, sample_transfers):
    ctx = _priced_ctx(settings, provider, prices, sample_transfers)
    run(ctx.indexer.run_once())

    held = {row["symbol"] for row in ctx.db.held_assets()}
    assert held == {"ETH", "USDC", "FREE-AIRDROP"}, "assets with a balance, not every asset seen"
    ctx.db.close()


# ------------------------------------------------- owned vs watched accounts


def test_a_wallet_added_later_gets_its_history_backfilled(settings, provider, sample_transfers):
    """The shared watermark only reaches back `reorg_depth` blocks. An account
    added after the first sync must not silently start from the current head."""
    from app.main import build_context

    provider.transfers = sample_transfers
    partial = make_config(wallets=[{"name": "Main", "address": MAIN}])

    ctx = build_context(settings, partial, provider=provider)
    ctx.db.migrate()
    ctx.db.apply_watch_config(partial)
    ctx.db.relink_accounts()
    run(ctx.indexer.run_once())
    provider.calls.clear()

    # Cold's own history is older than the reorg window.
    full = make_config()
    ctx.db.apply_watch_config(full)
    ctx.db.relink_accounts()
    result = run(ctx.indexer.run_once())

    starts = {address: from_block for _, address, _, from_block, _ in provider.calls}
    assert starts[COLD] < starts[MAIN], "the new account reaches further back"
    assert starts[MAIN] == provider.head_block - settings.reorg_depth + 1
    assert result.backfill is True

    third = run(ctx.indexer.run_once())
    assert third.backfill is False, "the backfill happens once"
    assert {c[3] for c in provider.calls[-2:]} == {provider.head_block - settings.reorg_depth + 1}
    ctx.db.close()


def test_watched_accounts_are_indexed_like_owned_ones(settings, provider, sample_transfers):
    from app.main import build_context

    provider.transfers = sample_transfers
    config = make_config(
        wallets=[
            {"name": "Main", "address": MAIN},
            {"name": "Whale", "address": COLD, "owned": False},
        ]
    )
    ctx = build_context(settings, config, provider=provider)
    ctx.db.migrate()
    ctx.db.apply_watch_config(config)
    ctx.db.relink_accounts()
    result = run(ctx.indexer.run_once())

    accounts = {a.name: a for a in ctx.db.list_accounts()}
    assert accounts["Whale"].is_owned is False
    assert accounts["Main"].is_owned is True
    assert result.ok

    # The USDC transfer is only reachable through the watched account, so its
    # presence proves a watched account is scanned exactly like an owned one.
    symbols = {row["symbol"] for row in ctx.db.query_activities(limit=50)}
    assert "USDC" in symbols
    watched = [
        row for row in ctx.db.query_activities(limit=50)
        if accounts["Whale"].id in (row["from_account_id"], row["to_account_id"])
    ]
    assert len(watched) == 2, "its own transfers and the one it shares with Main"
    ctx.db.close()


def test_ui_added_accounts_survive_a_config_reload(app_ctx, watch_config):
    added = app_ctx.db.add_account(
        chain_id="ethereum", address=STRANGER, name="Whale", is_owned=False
    )
    assert added.source == "ui" and added.synced_to_block is None

    app_ctx.db.apply_watch_config(watch_config)

    names = [a.name for a in app_ctx.db.list_accounts()]
    assert "Whale" in names, "the config file is not the source of truth for UI accounts"
    assert names[:3] == ["Main", "Cold", "Payments"], "owned accounts still lead"


def test_an_address_cannot_be_watched_twice(app_ctx):
    with pytest.raises(ValueError, match="already watched"):
        app_ctx.db.add_account(chain_id="ethereum", address=MAIN, name="Duplicate", is_owned=True)


def test_only_ui_added_accounts_can_be_removed(app_ctx):
    from_config = next(a for a in app_ctx.db.list_accounts() if a.name == "Main")
    assert app_ctx.db.remove_account(from_config.id) is None, "the file would bring it back"

    added = app_ctx.db.add_account(
        chain_id="ethereum", address=STRANGER, name="Whale", is_owned=False
    )
    assert app_ctx.db.remove_account(added.id) == "Whale"
    assert "Whale" not in [a.name for a in app_ctx.db.list_accounts()]
    assert "Whale" in [a.name for a in app_ctx.db.list_accounts(active_only=False)]


def test_widening_the_backfill_window_reaches_further_back(settings, provider, sample_transfers):
    """Raising BACKFILL_DAYS used to do nothing once an account had synced: the
    watermark only moved forward, so the indexer kept resuming at the head."""
    from dataclasses import replace

    from app.main import build_context

    provider.transfers = sample_transfers
    narrow = replace(settings, backfill_days=1)
    config = make_config(wallets=[{"name": "Main", "address": MAIN}])

    ctx = build_context(narrow, config, provider=provider)
    ctx.db.migrate()
    ctx.db.apply_watch_config(config)
    ctx.db.relink_accounts()
    run(ctx.indexer.run_once())
    floor_before = ctx.db.list_accounts()[0].indexed_from_block
    ctx.db.close()

    wide = replace(settings, backfill_days=90)
    ctx = build_context(wide, config, provider=provider)
    provider.calls.clear()
    result = run(ctx.indexer.run_once())

    start = next(from_block for _, address, _, from_block, _ in provider.calls if address == MAIN)
    assert start == provider.head_block - wide.backfill_blocks, "it reaches back to the new floor"
    assert result.backfill is True
    assert ctx.db.list_accounts()[0].indexed_from_block < floor_before
    ctx.db.close()


def test_narrowing_the_window_does_not_forget_what_was_read(settings, provider, sample_transfers):
    """The floor only moves down: ground once covered stays covered."""
    from dataclasses import replace

    from app.main import build_context

    provider.transfers = sample_transfers
    config = make_config(wallets=[{"name": "Main", "address": MAIN}])

    ctx = build_context(replace(settings, backfill_days=90), config, provider=provider)
    ctx.db.migrate()
    ctx.db.apply_watch_config(config)
    run(ctx.indexer.run_once())
    deep = ctx.db.list_accounts()[0].indexed_from_block
    ctx.db.close()

    ctx = build_context(replace(settings, backfill_days=1), config, provider=provider)
    provider.calls.clear()
    run(ctx.indexer.run_once())

    assert ctx.db.list_accounts()[0].indexed_from_block == deep
    starts = {from_block for _, address, _, from_block, _ in provider.calls if address == MAIN}
    assert min(starts) > deep, "and it goes back to incremental, not to the shallower floor"
    ctx.db.close()


def test_only_the_wallets_that_moved_are_re_read(app_ctx, provider, sample_transfers):
    """A balance cannot change without a transfer, and we have just read every
    transfer. Asking the provider about the wallets that did not move is a
    rate limit spent on an answer we already know."""
    app_ctx.indexer.settings = replace(app_ctx.indexer.settings, balance_refresh=3600)
    provider.transfers = sample_transfers
    run(app_ctx.indexer.run_once())

    provider.balance_calls.clear()
    provider.transfers = []
    run(app_ctx.indexer.run_once())

    assert provider.balance_calls == [], "a quiet cycle should ask the provider nothing"


def test_a_wallet_that_moved_is_re_read_at_once(app_ctx, provider, sample_transfers):
    app_ctx.indexer.settings = replace(app_ctx.indexer.settings, balance_refresh=3600)
    run(app_ctx.indexer.run_once())

    provider.balance_calls.clear()
    provider.head_block += 1
    provider.transfers = [
        transfer("late", sender=STRANGER, recipient=MAIN, amount=10**18,
                 block=provider.head_block),
    ]
    run(app_ctx.indexer.run_once())

    assert MAIN in provider.balance_calls
    assert COLD not in provider.balance_calls, "the wallet that stayed still is not re-read"


def test_the_sweep_still_catches_what_no_transfer_explains(app_ctx, provider, sample_transfers):
    """The argument above is sound but not airtight, so a slow full sweep
    remains the safety net."""
    app_ctx.indexer.settings = replace(app_ctx.indexer.settings, balance_refresh=0)
    provider.transfers = sample_transfers
    run(app_ctx.indexer.run_once())

    provider.native_balances[MAIN] = 7 * 10**18
    provider.transfers = []
    run(app_ctx.indexer.run_once())

    accounts = {a.name: a.id for a in app_ctx.db.list_accounts()}
    refreshed = app_ctx.db.balances_by_account()[accounts["Main"]]
    assert refreshed[0]["amount_raw"] == str(7 * 10**18)


def test_never_means_never_not_the_moment_the_machine_booted(settings, provider, prices,
                                                             sample_transfers, monkeypatch):
    """`time.monotonic()` has an arbitrary origin — on Linux it counts from
    boot. Zero as a stand-in for "never last refreshed" therefore means "boot
    time", so the first refresh happens on a machine that has been up for hours
    and is silently skipped on one that has just started.

    This passed on every machine it was tried on and failed on CI, which is the
    only fresh machine in the loop.
    """
    real = time.monotonic
    base = real()
    monkeypatch.setattr(time, "monotonic", lambda: real() - base + 40.0)

    fresh = replace(settings, price_refresh=300, balance_refresh=600)
    ctx = _priced_ctx(fresh, provider, prices, sample_transfers)
    result = run(ctx.indexer.run_once())

    assert result.prices_updated > 0, "a fresh process has never priced anything"
    assert result.balances_updated > 0, "nor read any balance"
    ctx.db.close()
