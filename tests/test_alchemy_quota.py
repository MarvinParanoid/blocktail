"""What blocktail asks of Alchemy, counted per HTTP request (S10a audit).

These hold today's behaviour — they are measurements, not a wish list: when
S10b changes how the indexer polls, the numbers here change with it, on
purpose and in the same commit. See docs/audit/alchemy-quota.md.

All offline: FakeAlchemy (tests/quota_lab.py) answers Alchemy's JSON-RPC and
Prices API; the indexer runs on a virtual clock.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tests import quota_scenarios as s
from tests.conftest import FakeProvider, make_config, make_settings
from tests.quota_lab import CU, CountingProvider, Lab, by_method, cu, synthetic_wallets, warm

WALLETS = 7
TRANSFER_CALLS = 2 * WALLETS        # outgoing and incoming, per wallet, per cycle
IDLE_CYCLE = {"eth_blockNumber": 1, "alchemy_getAssetTransfers": TRANSFER_CALLS}


@pytest.fixture
def lab(tmp_path):
    lab = Lab(tmp_path)
    warm(lab)
    yield lab
    lab.close()


# ---------------------------------------- the background, with nothing to find


def test_a_cycle_with_no_new_blocks_still_asks_for_every_wallets_transfers(lab):
    """Hypotheses 1 and 9: nothing changed on chain, and the cycle still sends
    fourteen alchemy_getAssetTransfers — 1,680 of its 1,690 CU."""
    result, recs = lab.cycle(blocks=0)
    assert dict(by_method(recs)) == IDLE_CYCLE
    assert cu(recs) == 10 + TRANSFER_CALLS * 120 == 1690
    assert result.stored == 0 and result.fetched == 0


def test_new_blocks_without_transfers_cost_the_same_and_rescan_the_reorg_window(lab):
    head = lab.server.head
    result, recs = lab.cycle(blocks=7)
    assert dict(by_method(recs)) == IDLE_CYCLE
    assert result.head_block == head + 7
    assert result.head_block - result.from_block + 1 == 64 + 7, "re-reads REORG_DEPTH blocks"


def test_the_reorg_window_does_not_add_requests_only_blocks_per_request(tmp_path):
    """Hypothesis 2: depth changes the range of each request, not their number
    — and Alchemy charges per request (120 CU), not per block."""
    for depth in (8, 64, 512):
        lab = Lab(tmp_path / f"d{depth}", settings={"reorg_depth": depth})
        try:
            warm(lab)
            _, recs = lab.cycle()
            assert dict(by_method(recs)) == IDLE_CYCLE, depth
        finally:
            lab.close()


def test_balances_and_prices_run_on_their_own_clocks_whatever_happens(lab):
    """Hypothesis 3: a full balance sweep every 7th cycle (≥600 s at a 90 s
    tick) and prices every 4th (≥300 s), with no transfer at all."""
    sweeps = prices = 0
    for _ in range(28):
        _, recs = lab.cycle()
        methods = by_method(recs)
        if methods["eth_getBalance"]:
            assert methods["eth_getBalance"] == methods["alchemy_getTokenBalances"] == WALLETS
            sweeps += 1
        if methods["prices:/tokens/by-symbol"]:
            assert methods["prices:/tokens/by-address"] == 1     # 13 tokens, ≤25 per request
            prices += 1
    assert (sweeps, prices) == (4, 7)


def test_the_idle_model_at_production_settings():
    m = s.model()
    assert m["cycles_per_day"] == 960
    assert m["day_cu"] == 1_680_000
    assert m["day_cu_by_method"]["alchemy_getAssetTransfers"] == 1_612_800      # 96 %
    assert m["month_cu"] == 50_400_000 > 30_000_000, "past the free plan in 18 days"


# ------------------------------------------------------------------ events


def test_a_received_transfer_adds_one_balance_read_for_that_wallet_only():
    out = s.c_eth_received()
    assert out["extra"] == {"eth_getBalance": 1, "alchemy_getTokenBalances": 1}


def test_a_new_token_is_looked_up_once_even_when_the_transfer_carried_its_decimals():
    """A transfer's own decimals do not reach the metadata cache, so the
    balance read that follows looks the token up anyway: one call, 10 CU."""
    with_decimals = s.d_token_received(decimals_in_transfer=True)
    assert with_decimals["extra"]["alchemy_getTokenMetadata"] == 1
    without = s.d_token_received(decimals_in_transfer=False)
    assert without["extra"]["alchemy_getTokenMetadata"] == 1
    assert "alchemy_getTokenMetadata" not in without["next"]["methods"], "cached afterwards"


def test_wallets_scanned_over_different_ranges_cost_pages_not_duplicates():
    """Hypothesis 5: each wallet's two requests are its own (an address and
    a side), never repeated for another wallet; a wallet far behind costs one
    request per 1,000 transfers in its range."""
    out = s.e_overlapping_ranges()
    calls = out["transfer_calls_per_wallet"]
    assert calls.pop("W3 transfers-in") == 3          # 2,500 transfers, 1,000 per page
    assert set(calls.values()) == {1}
    assert out["cycle"]["methods"]["alchemy_getAssetTransfers"] == TRANSFER_CALLS + 2


# ------------------------------------------------------------------ errors


def test_a_transient_error_is_retried_once_and_charged():
    out = s.f_transient_error()
    assert out["ok"]
    assert out["cycle"]["methods"]["alchemy_getAssetTransfers"] == TRANSFER_CALLS + 1
    assert out["cycle"]["cu"] == 1690 + 120, "a 5xx is charged at the method's price"
    assert len(out["backoff_seconds"]) == 1 and 0.5 <= out["backoff_seconds"][0] <= 0.7


def test_throttling_is_free_but_a_failing_provider_costs_more_than_a_quiet_hour():
    """Hypotheses 7 and 10: 429 costs 0 CU and waits ≥2 s; a call that fails
    every attempt is tried 3 times (5 before S10b.1), the cycle fails without moving the
    watermark, and the next cycle repeats the whole scan — a bounded loop
    (one per interval) that spends more than an idle cycle for no progress."""
    out = s.g_throttled()
    throttled = out["throttled"]
    assert throttled["ok"] and throttled["cycle"]["cu"] == 1690, "two 429s cost nothing"
    assert all(wait >= 2.0 for wait in throttled["backoff_seconds"])
    failing = out["failing"]
    assert not failing["ok"] and not failing["watermark_moved"]
    # S10b.1: a 5xx is charged, so it is tried 3 times, not 5 (429/403: still 5).
    assert failing["attempts_on_failing_call"] == 3
    assert failing["cycle"]["cu"] > 1690
    assert out["next"]["methods"]["alchemy_getAssetTransfers"] == TRANSFER_CALLS


def test_a_history_request_reaches_back_in_one_cycle_with_the_same_requests():
    """Hypothesis 6: the history job is part of the regular cycle, not a
    second poller; it costs its pages and a forced balance sweep, once."""
    out = s.h_history_job()
    assert (out["status_before"], out["status_after"]) == ("queued", "completed")
    assert out["during"]["methods"]["alchemy_getAssetTransfers"] == TRANSFER_CALLS
    assert out["during"]["methods"]["eth_getBalance"] == WALLETS
    assert out["after"]["methods"] == IDLE_CYCLE


def test_a_restart_rereads_balances_metadata_and_prices_once(tmp_path):
    out = s.i_restart()
    first = out["first"]["methods"]
    assert first["alchemy_getTokenMetadata"] == 13, "the in-memory cache is gone"
    assert first["eth_getBalance"] == WALLETS and first["prices:/tokens/by-symbol"] == 1
    assert out["second"]["methods"] == IDLE_CYCLE
    assert out["head"] - out["from_block"] + 1 <= 64 + 7 + 1, "resumes from its watermark"


def test_nobody_opening_the_page_changes_nothing_and_opening_a_transaction_costs_40_cu():
    out = s.j_no_ui()
    assert out["hour"]["methods"]["alchemy_getAssetTransfers"] == 40 * TRANSFER_CALLS
    assert out["one_transaction_opened"]["methods"] == {
        "eth_getTransactionReceipt": 1, "eth_getTransactionByHash": 1}
    assert out["one_transaction_opened"]["cu"] == 40


# ------------------------------------------------------------ the counters


def test_the_report_names_no_address_and_no_key(lab):
    _, recs = lab.cycle()
    text = json.dumps(recs) + s.report()
    for wallet in synthetic_wallets():
        assert wallet not in text and wallet[2:].lower() not in text.lower()
    assert "/v2/LAB" not in text and "LAB" not in json.dumps(recs)


def test_every_method_the_application_sends_has_a_cost():
    from tests.quota_lab import FakeAlchemy  # noqa: F401 — documents where methods are named
    import app.chains.ethereum.alchemy as provider_source

    text = open(provider_source.__file__).read()
    for method in ("eth_blockNumber", "eth_getBalance", "eth_getTransactionReceipt",
                   "eth_getTransactionByHash", "alchemy_getAssetTransfers",
                   "alchemy_getTokenBalances", "alchemy_getTokenMetadata"):
        assert method in text and method in CU


def test_the_fake_provider_counter_sees_the_same_calls_at_method_level(tmp_path):
    """With the test suite's FakeProvider: one get_head_block and two
    get_transfers per wallet per cycle — the method-level view of the same
    fourteen alchemy_getAssetTransfers (FakeProvider has no pages)."""
    from app.main import build_context

    wallets = synthetic_wallets()
    labels = {w: f"W{i + 1}" for i, w in enumerate(wallets)}
    provider = CountingProvider(FakeProvider(head_block=21_000_000), labels)
    config = make_config(wallets=[{"name": labels[w], "address": w} for w in wallets])
    ctx = build_context(make_settings(tmp_path, balance_refresh=600), config, provider=provider)
    ctx.db.migrate()
    ctx.db.apply_watch_config(config)
    ctx.db.relink_accounts()
    try:
        asyncio.run(ctx.indexer.run_once())
        provider.calls.clear()
        provider.inner.head_block += 7
        asyncio.run(ctx.indexer.run_once())
        methods = {}
        for call in provider.calls:
            methods[call["method"]] = methods.get(call["method"], 0) + 1
        assert methods == {"get_head_block": 1, "get_transfers": TRANSFER_CALLS}
        assert {c["wallet"] for c in provider.calls if c["method"] == "get_transfers"} == set(labels.values())
        assert all(c["ok"] for c in provider.calls)
    finally:
        ctx.db.close()
