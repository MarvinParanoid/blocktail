"""S10b.1: two-level sync — fewer transfer queries, no transfer lost.

Every scenario runs the real indexer, AlchemyProvider and AlchemyPrices on
tests/quota_lab.py (FakeAlchemy over httpx.MockTransport, a virtual clock) at
production's settings with FULL_SCAN_INTERVAL_SECONDS=900. Where it matters,
the same synthetic chain is run through the original algorithm
(FULL_SCAN_INTERVAL_SECONDS=0, exactly S10a's baseline) and the stored
transfers are compared. See docs/audit/alchemy-quota-s10b1.md.
"""

from __future__ import annotations

import logging
import random
from collections import Counter

import pytest

from tests.conftest import address
from tests.quota_lab import (
    PRODUCTION,
    TWO_LEVEL,
    Holding,
    Lab,
    by_method,
    cu,
    synthetic_wallets,
    warm,
)

WALLETS = 7
PROBE_CYCLE = {"eth_blockNumber": 1, "eth_getBalance": WALLETS, "alchemy_getTokenBalances": WALLETS}
PROBE_CU = 10 + WALLETS * 40                     # 290
FULL_TRANSFERS = 2 * WALLETS


def lab_with(tmp_path, name: str = "two", **settings) -> Lab:
    return Lab(tmp_path / name, settings={**TWO_LEVEL, **settings})


def stored_keys(lab: Lab) -> set[str]:
    rows = lab.ctx.db.connect().execute("SELECT event_key FROM activities").fetchall()
    return {row[0] for row in rows}


def activity_rows(lab: Lab) -> int:
    return lab.ctx.db.connect().execute("SELECT COUNT(*) FROM activities").fetchone()[0]


def watermarks(lab: Lab) -> dict[str, int]:
    return {a.name: a.synced_to_block for a in lab.ctx.db.list_accounts()}


def until_full(lab: Lab, limit: int = 12) -> list[tuple[object, list[dict]]]:
    """Cycles up to and including the next full scan."""
    out = []
    for _ in range(limit):
        result, recs = lab.cycle()
        out.append((result, recs))
        if result.scan == "full":
            return out
    raise AssertionError("no full scan within the limit")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Nothing here may reach a network: FakeAlchemy answers through
    httpx.MockTransport, which opens no socket. Any connect fails the test."""
    import socket

    def refuse(self, address):
        raise AssertionError(f"a test tried to connect to {address}")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


def _ready(lab: Lab) -> Lab:
    warm(lab)
    until_full(lab, limit=40)    # start right after a full scan: the clock is known
    lab.server.take()
    return lab


@pytest.fixture
def lab(tmp_path):
    lab = _ready(lab_with(tmp_path))
    yield lab
    lab.close()


@pytest.fixture(params=[8, 64], ids=["depth-8", "depth-64"])
def deep_lab(tmp_path, request):
    """The same, at REORG_DEPTH 8 and at production's 64."""
    lab = _ready(lab_with(tmp_path, f"d{request.param}", reorg_depth=request.param))
    yield lab
    lab.close()


# ------------------------------------------------- 1, 2, 13: the background


def test_1_and_13_with_nothing_happening_a_cycle_reads_balances_and_scans_nothing(lab):
    result, recs = lab.cycle(blocks=0)
    assert result.scan == "none" and result.scanned == 0
    assert dict(by_method(recs)) == PROBE_CYCLE
    assert cu(recs) == PROBE_CU


def test_2_new_blocks_without_transfers_scan_nothing_and_keep_the_watermarks(lab):
    before = watermarks(lab)
    result, recs = lab.cycle(blocks=7)
    assert result.scan == "none" and dict(by_method(recs)) == PROBE_CYCLE
    assert watermarks(lab) == before, "not scanned: not moved"


def test_a_full_scan_comes_every_nine_cycles_and_never_more_than_900_s_apart(lab):
    fulls = []
    for i in range(45):
        result, recs = lab.cycle()
        if result.scan == "full":
            fulls.append(lab.clock.now)
            assert by_method(recs)["alchemy_getAssetTransfers"] == FULL_TRANSFERS
    gaps = [b - a for a, b in zip(fulls, fulls[1:])]
    assert len(fulls) == 5 and all(gap <= 900 for gap in gaps), gaps


# --------------------------------------------- 3, 4, 12: balances that move


def test_3_incoming_eth_is_scanned_and_stored_in_the_next_cycle(lab):
    w = lab.wallets[0]
    raw = lab.server.add_transfer(sender=address("stranger"), recipient=w, block=lab.server.head + 2)
    result, recs = lab.cycle()
    assert result.scan == "changed" and result.scanned == 1
    assert Counter(r["wallet"] for r in recs if r["method"] == "alchemy_getAssetTransfers") == {"W1": 2}
    assert raw["uniqueId"] in stored_keys(lab)
    assert watermarks(lab)["W1"] == result.head_block


def test_4_an_erc20_transfer_is_scanned_and_stored_in_the_next_cycle(lab):
    w = lab.wallets[1]
    token = lab.server.holdings[w][0]
    raw = lab.server.add_transfer(sender=address("stranger"), recipient=w,
                                  block=lab.server.head + 2, token=token, amount=5 * 10**18)
    result, _ = lab.cycle()
    assert result.scan == "changed" and result.scanned == 1
    assert raw["uniqueId"] in stored_keys(lab)


def test_12_several_wallets_moving_are_scanned_together_and_only_they(lab):
    for w in lab.wallets[:3]:
        lab.server.add_transfer(sender=address("stranger"), recipient=w, block=lab.server.head + 1)
    result, recs = lab.cycle()
    scanned = Counter(r["wallet"] for r in recs if r["method"] == "alchemy_getAssetTransfers")
    assert scanned == {"W1": 2, "W2": 2, "W3": 2}
    assert result.changed == 3 and result.scanned == 3


def test_detection_latency_of_a_balance_change_is_at_most_one_tick(lab):
    w = lab.wallets[2]
    raw = lab.server.add_transfer(sender=address("stranger"), recipient=w, block=lab.server.head + 1)
    happened = lab.clock.now
    for _ in range(3):
        lab.cycle()
        if raw["uniqueId"] in stored_keys(lab):
            break
    assert lab.clock.now - happened <= 180


# ----------------------------- 5, 6: what a balance cannot show — the full scan


def test_5_in_and_out_that_net_to_zero_are_found_by_the_full_scan(lab):
    w = lab.wallets[3]
    token = lab.server.holdings[w][0]
    got = lab.server.add_transfer(sender=address("payer"), recipient=w, block=lab.server.head + 1,
                                  token=token, amount=10**18)
    sent = lab.server.add_transfer(sender=w, recipient=address("payee"), block=lab.server.head + 2,
                                   token=token, amount=10**18)
    happened = lab.clock.now
    result, _ = lab.cycle()
    assert result.scan == "none", "the balances did not move"
    assert not {got["uniqueId"], sent["uniqueId"]} & stored_keys(lab)
    cycles = until_full(lab)
    assert {got["uniqueId"], sent["uniqueId"]} <= stored_keys(lab)
    assert lab.clock.now - happened <= 900 + 90, "by the full scan, within its interval"
    assert cycles[-1][0].scan == "full"


def test_6_a_transfer_the_balances_do_not_show_is_found_by_the_full_scan(tmp_path):
    """W1 holds more tokens than MAX_TOKEN_LOOKUPS (200): the balances read
    stops at 200, and a transfer of the 201st token moves nothing it sees."""
    lab = lab_with(tmp_path)
    try:
        w = lab.wallets[0]
        lab.server.holdings[w] = [Holding(address(f"many-{i}"), f"M{i}", 18) for i in range(205)]
        warm(lab)
        until_full(lab)
        hidden = lab.server.holdings[w][-1]
        raw = lab.server.add_transfer(sender=address("stranger"), recipient=w,
                                      block=lab.server.head + 1, token=hidden, amount=10**18)
        result, _ = lab.cycle()
        assert result.scan == "none" and raw["uniqueId"] not in stored_keys(lab)
        until_full(lab)
        assert raw["uniqueId"] in stored_keys(lab)
    finally:
        lab.close()


# ----------------------------------------------------------------- 7: reorg


def test_7_a_transfer_reorged_out_is_pruned_when_the_balance_reverts(deep_lab):
    lab = deep_lab
    w = lab.wallets[0]
    raw = lab.server.add_transfer(sender=address("stranger"), recipient=w, block=lab.server.head + 1)
    lab.cycle()
    assert raw["uniqueId"] in stored_keys(lab)
    lab.server.remove_transfer(raw)          # within REORG_DEPTH: the block is replaced
    result, _ = lab.cycle()
    assert result.scan == "changed" and result.pruned == 1
    assert raw["uniqueId"] not in stored_keys(lab)


def test_7_a_transfer_appearing_in_an_already_scanned_block_is_found(deep_lab):
    """A reorg puts a transfer into a block the wallet was already scanned
    past: the next scan re-reads REORG_DEPTH blocks below its own watermark."""
    lab = deep_lab
    w = lab.wallets[1]
    scanned_to = watermarks(lab)["W2"]
    depth = lab.ctx.settings.reorg_depth
    raw = lab.server.add_transfer(sender=address("stranger"), recipient=w,
                                  block=scanned_to - min(10, depth - 1))
    result, _ = lab.cycle()
    assert result.scan == "changed"
    assert raw["uniqueId"] in stored_keys(lab)


def test_7_a_reorg_the_balances_cannot_see_is_found_by_the_full_scan(deep_lab):
    """A transfer replaced by one of the same amount (a reorg reordering a
    block) leaves the balances as they were; the full scan, from each wallet's
    own watermark less REORG_DEPTH, corrects both."""
    lab = deep_lab
    w = lab.wallets[2]
    old = lab.server.add_transfer(sender=address("stranger"), recipient=w, block=lab.server.head + 1)
    lab.cycle()
    assert old["uniqueId"] in stored_keys(lab)
    lab.server.remove_transfer(old, move_balances=False)
    new = lab.server.add_transfer(sender=address("stranger"), recipient=w,
                                  block=lab.server.head - 3, seed="replacement", move_balances=False)
    result, _ = lab.cycle()
    assert result.scan == "none"
    until_full(lab)
    keys = stored_keys(lab)
    assert new["uniqueId"] in keys and old["uniqueId"] not in keys


# ------------------------------------------------------- 8: restart/downtime


def test_8_after_six_hours_down_the_first_cycle_scans_every_wallet_from_its_watermark(lab):
    before = watermarks(lab)
    lab.start()                                            # a new process
    lab.clock.now += 6 * 3600
    lab.server.head += 6 * 3600 // 12
    missed = lab.server.add_transfer(sender=address("stranger"), recipient=lab.wallets[4],
                                     block=lab.server.head - 900)
    netted = [lab.server.add_transfer(sender=address("x"), recipient=lab.wallets[5],
                                      block=lab.server.head - 1000, token=lab.server.holdings[lab.wallets[5]][0]),
              lab.server.add_transfer(sender=lab.wallets[5], recipient=address("y"),
                                      block=lab.server.head - 999, token=lab.server.holdings[lab.wallets[5]][0])]
    lab.next_wait = 0
    result, recs = lab.cycle(blocks=0)
    assert result.scan == "full", "a restart trusts nothing it did not see"
    assert by_method(recs)["alchemy_getAssetTransfers"] == FULL_TRANSFERS
    assert result.from_block == min(before.values()) - 63
    keys = stored_keys(lab)
    assert missed["uniqueId"] in keys and {t["uniqueId"] for t in netted} <= keys
    assert set(watermarks(lab).values()) == {result.head_block}


# ------------------------------------------------------- 9, 10: provider errors


def test_9_a_5xx_on_one_scan_is_retried_and_nothing_is_lost(lab):
    w = lab.wallets[0]
    raw = lab.server.add_transfer(sender=address("stranger"), recipient=w, block=lab.server.head + 1)
    lab.server.failures.append(("alchemy_getAssetTransfers", 503))
    result, recs = lab.cycle()
    assert result.ok and raw["uniqueId"] in stored_keys(lab)
    assert any(r["attempt"] == 2 for r in recs)


def test_9_a_scan_that_fails_moves_no_watermark_and_is_scanned_again(lab):
    w = lab.wallets[0]
    before = watermarks(lab)
    raw = lab.server.add_transfer(sender=address("stranger"), recipient=w, block=lab.server.head + 1)
    lab.server.failures += [("alchemy_getAssetTransfers", 503, "W1 transfers-in")] * 3
    result, _ = lab.cycle()
    assert not result.ok and watermarks(lab) == before
    assert raw["uniqueId"] not in stored_keys(lab)
    result, recs = lab.cycle()                 # balances now look unchanged: pending, still scanned
    assert result.ok and result.scan == "changed"
    assert raw["uniqueId"] in stored_keys(lab)


def test_10_a_429_costs_nothing_waits_and_succeeds(lab):
    w = lab.wallets[0]
    raw = lab.server.add_transfer(sender=address("stranger"), recipient=w, block=lab.server.head + 1)
    lab.server.failures += [("alchemy_getAssetTransfers", 429)] * 2
    slept = len(lab.clock.slept)
    result, recs = lab.cycle()
    assert result.ok and raw["uniqueId"] in stored_keys(lab)
    throttled = [r for r in recs if r["status"] == 429]
    assert len(throttled) == 2 and cu(throttled) == 0
    assert all(wait >= 2.0 for wait in lab.clock.slept[slept:])


@pytest.mark.parametrize("broken", [("alchemy_getAssetTransfers",),
                                    ("eth_getBalance", "alchemy_getTokenBalances"),
                                    ("eth_blockNumber",)])
def test_a_provider_failing_for_an_hour_costs_at_most_10000_cu(lab, broken):
    for method in broken:
        lab.server.broken[method] = 503
    before = activity_rows(lab)
    # A transfer during the outage, in the block it happened in.
    raw = lab.server.add_transfer(sender=address("stranger"), recipient=lab.wallets[0],
                                  block=lab.server.head + 1)
    start = lab.clock.now
    spent, cycles = [], 0
    while lab.clock.now - start < 3600:
        result, recs = lab.cycle()
        spent += recs
        cycles += 1
    assert cu(spent) <= 10_000, (cu(spent), cycles, dict(by_method(spent)))
    assert cycles < 40, "backed off, not every 90 s"
    for method in broken:
        del lab.server.broken[method]
    lab.next_wait = 90
    for _ in range(12):
        result, _ = lab.cycle()
        if result.ok and raw["uniqueId"] in stored_keys(lab):
            break
    assert raw["uniqueId"] in stored_keys(lab), "the missed range is caught up"
    assert activity_rows(lab) == before + 1


# ----------------------------------------------------------- 11: history job


def test_11_an_unfinished_history_job_makes_the_cycle_a_full_scan_and_finishes(lab):
    lab.ctx.db.request_history(lab.ctx.chain.chain_id, lab.ctx.settings.backfill_blocks)
    result, recs = lab.cycle()
    assert result.scan == "full"
    assert lab.ctx.db.get_sync_status(lab.ctx.chain.chain_id).history_status == "completed"
    result, _ = lab.cycle()
    assert result.scan == "none"


# --------------------------------- 14, 15: balance changes and the full scan


def test_14_a_change_in_the_cycle_a_full_scan_is_due_is_scanned_once(lab):
    for _ in range(8):
        lab.cycle()                              # the next one, 810 s on, is the full scan
    raw = lab.server.add_transfer(sender=address("stranger"), recipient=lab.wallets[0],
                                  block=lab.server.head + 1)
    result, recs = lab.cycle()
    assert result.scan == "full"
    assert by_method(recs)["alchemy_getAssetTransfers"] == FULL_TRANSFERS, "not twice for W1"
    assert raw["uniqueId"] in stored_keys(lab)


def test_15_constant_balance_changes_do_not_put_off_the_full_scan(lab):
    w = lab.wallets[0]
    fulls = 0
    for _ in range(27):
        lab.server.add_transfer(sender=address("stranger"), recipient=w, block=lab.server.head + 1)
        result, _ = lab.cycle()
        fulls += result.scan == "full"
        assert result.scan in ("full", "changed")
    assert fulls == 3


# ------------------------------------------------ the original algorithm, side by side


def _script(seed: int, wallets: list[str], holdings: dict) -> list[tuple[int, dict]]:
    """Events per cycle: ETH in and out, ERC-20 in and out, between two watched
    wallets, pairs that net to zero, and reorgs within the window."""
    rng = random.Random(seed)
    events = []
    for cycle in range(60):
        for _ in range(rng.choice([0, 0, 0, 1, 2])):
            kind = rng.choice(["eth-in", "eth-out", "token-in", "token-out", "between",
                               "net-zero", "reorg"])
            w = rng.choice(wallets)
            events.append((cycle, {"kind": kind, "wallet": w, "other": rng.choice(wallets),
                                   "offset": rng.randint(-20, 6),
                                   "token": rng.randrange(len(holdings[w]) or 1),
                                   "amount": rng.randint(1, 9) * 10**15}))
    return events


def _play(lab: Lab, script: list, cycles: int) -> None:
    by_cycle: dict[int, list[dict]] = {}
    for cycle, event in script:
        by_cycle.setdefault(cycle, []).append(event)
    last: dict | None = None
    for cycle in range(cycles):
        for e in by_cycle.get(cycle, []):
            head, w = lab.server.head, e["wallet"]
            block = head + max(1, e["offset"])
            held = lab.server.holdings.get(w) or []
            token = held[e["token"] % len(held)] if held else None
            if e["kind"] == "eth-in":
                last = lab.server.add_transfer(sender=address("ext"), recipient=w, block=block,
                                               amount=e["amount"])
            elif e["kind"] == "eth-out":
                last = lab.server.add_transfer(sender=w, recipient=address("ext"), block=block,
                                               amount=e["amount"])
            elif e["kind"] == "token-in" and token:
                last = lab.server.add_transfer(sender=address("ext"), recipient=w, block=block,
                                               token=token, amount=e["amount"])
            elif e["kind"] == "token-out" and token and token.amount > e["amount"]:
                last = lab.server.add_transfer(sender=w, recipient=address("ext"), block=block,
                                               token=token, amount=e["amount"])
            elif e["kind"] == "between" and e["other"] != w:
                last = lab.server.add_transfer(sender=w, recipient=e["other"], block=block,
                                               amount=e["amount"])
            elif e["kind"] == "net-zero" and token:
                lab.server.add_transfer(sender=address("a"), recipient=w, block=block,
                                        token=token, amount=e["amount"])
                last = lab.server.add_transfer(sender=w, recipient=address("b"), block=block,
                                               token=token, amount=e["amount"])
            elif e["kind"] == "reorg" and last is not None and last in lab.server.transfers \
                    and int(last["blockNum"], 16) > head - 60:
                lab.server.remove_transfer(last)
                last = None
        lab.cycle()


def _truth(lab: Lab) -> set[str]:
    watched = set(lab.wallets)
    return {t["uniqueId"] for t in lab.server.transfers
            if t["from"] in watched or t["to"] in watched}


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_after_a_full_scan_the_stored_transfers_equal_the_original_algorithms(tmp_path, seed):
    """The same synthetic chain through both algorithms, tick for tick: once a
    full scan has run after the last event, both hold exactly the transfers
    on chain — none lost, none extra, none duplicated."""
    wallets = synthetic_wallets()
    results = {}
    for name, settings in (("original", PRODUCTION), ("two-level", TWO_LEVEL)):
        lab = Lab(tmp_path / f"{name}-{seed}", wallets=wallets, settings=settings)
        try:
            warm(lab)
            script = _script(seed, wallets, lab.server.holdings)
            _play(lab, script, 60)
            until_full(lab) if settings is TWO_LEVEL else lab.cycle()
            assert activity_rows(lab) == len(stored_keys(lab)), f"{name}: duplicates"
            results[name] = (stored_keys(lab), _truth(lab))
        finally:
            lab.close()
    original, truth_a = results["original"]
    two_level, truth_b = results["two-level"]
    assert truth_a == truth_b, "the same chain"
    assert original == truth_a
    assert two_level == original


# --------------------------------------------------- spend and observability


def test_idle_spend_at_production_settings_is_under_450000_cu_a_day():
    from tests.quota_s10b1 import model
    m = model()
    assert m["day_cu"] <= 450_000
    assert m["reduction"] >= 0.70


def test_requests_are_paced_to_stay_under_the_plans_throughput(tmp_path):
    lab = lab_with(tmp_path, cu_per_second=250)
    try:
        warm(lab)
        result, recs = until_full(lab)[-1]
        assert result.scan == "full"
        stamps = sorted(((r["t"], i), r) for i, r in enumerate(recs))
        for (t, _), _r in stamps:                 # Alchemy measures a rolling 10 s window
            window = [r for (s, _), r in stamps if t <= s < t + 10]
            assert cu(window) <= 3000
    finally:
        lab.close()


def test_the_hourly_quota_line_counts_by_method_and_names_no_wallet(lab, caplog):
    caplog.set_level(logging.INFO, logger="app.sync")
    for _ in range(45):
        result, _ = lab.cycle()
        lab.ctx.indexer._log(result)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("quota ")]
    assert lines, "one line an hour"
    line = lines[-1]
    for needle in ("provider requests", "CU", "alchemy_getAssetTransfers", "full scan",
                   "balance-change scan", "no scan", "deferred", "last full scan"):
        assert needle in line, needle
    every = " ".join(r.getMessage() for r in caplog.records)
    for wallet in lab.wallets:
        assert wallet not in every and wallet[2:] not in every
    assert "LAB" not in every


# ------------------------------------------------------ the watermark invariant


def test_an_early_scan_moves_only_the_scanned_wallets_watermark(lab):
    """A wallet's watermark is never above the last block actually scanned
    for it: W1's early scan leaves W2..W7 where they were."""
    before = watermarks(lab)
    lab.server.add_transfer(sender=address("stranger"), recipient=lab.wallets[0],
                            block=lab.server.head + 1)
    result, _ = lab.cycle()
    assert result.scan == "changed" and result.scanned == 1
    after = watermarks(lab)
    assert after["W1"] == result.head_block
    assert {k: v for k, v in after.items() if k != "W1"} == \
        {k: v for k, v in before.items() if k != "W1"}


def test_a_wallet_left_unscanned_is_caught_up_from_its_own_watermark(tmp_path):
    """With a reorg window smaller than the blocks between two full scans
    (REORG_DEPTH 8, about 63 blocks per full-scan interval), only a correct
    per-wallet watermark finds W2's netted transfers while W1 is scanned
    every cycle. At production's 64 the window happens to cover the gap; the
    guarantee must not rest on that."""
    lab = lab_with(tmp_path, reorg_depth=8)
    try:
        warm(lab)
        until_full(lab)
        w2 = lab.wallets[1]
        token = lab.server.holdings[w2][0]
        netted = [lab.server.add_transfer(sender=address("a"), recipient=w2, block=lab.server.head + 1,
                                          token=token, amount=10**18),
                  lab.server.add_transfer(sender=w2, recipient=address("b"), block=lab.server.head + 1,
                                          token=token, amount=10**18)]
        for _ in range(12):
            lab.server.add_transfer(sender=address("stranger"), recipient=lab.wallets[0],
                                    block=lab.server.head + 1)
            result, _ = lab.cycle()
            if result.scan == "full":
                break
        assert result.scan == "full"
        assert {t["uniqueId"] for t in netted} <= stored_keys(lab)
    finally:
        lab.close()


# ------------------------------------------------------------- RC edge cases


def test_the_provider_rests_after_three_5xx_in_a_row_and_sends_nothing_meanwhile(lab):
    """The circuit breaker: three server errors in a row and the provider is
    not asked for 60 s — those requests are not sent, so not charged; the
    first answer that is not a 5xx closes it."""
    provider = lab.ctx.provider
    lab.server.broken["eth_blockNumber"] = 503
    with pytest.raises(Exception):
        lab.run(provider.get_head_block())
    sent = lab.server.take()
    assert [r["status"] for r in sent] == [503, 503, 503]
    for _ in range(5):
        with pytest.raises(Exception, match="not sent"):
            lab.run(provider.get_head_block())
    assert lab.server.take() == [], "resting: nothing reaches the provider"
    del lab.server.broken["eth_blockNumber"]
    lab.clock.now += provider.REST_SECONDS
    assert lab.run(provider.get_head_block()) == lab.server.head
    assert [r["status"] for r in lab.server.take()] == [200]
    assert provider._resting_until is None


def test_a_balance_change_while_the_provider_rests_is_scanned_once_it_is_back(lab):
    for method in ("eth_blockNumber", "eth_getBalance", "alchemy_getTokenBalances"):
        lab.server.broken[method] = 503
    raw = lab.server.add_transfer(sender=address("stranger"), recipient=lab.wallets[0],
                                  block=lab.server.head + 1)
    for _ in range(3):
        result, _ = lab.cycle()
        assert not result.ok
    lab.server.broken.clear()
    lab.next_wait = 90 + lab.ctx.provider.REST_SECONDS
    for _ in range(3):
        result, _ = lab.cycle()
        if raw["uniqueId"] in stored_keys(lab):
            break
    assert result.ok and raw["uniqueId"] in stored_keys(lab)


def test_more_than_64_blocks_pile_up_for_a_wallet_not_scanned_and_one_scan_covers_them(tmp_path):
    """With full scans 30 min apart (~150 blocks) and REORG_DEPTH 64, a wallet
    not scanned in between has a gap far wider than the reorg window; its
    own watermark, not the window, is what the full scan starts from."""
    lab = _ready(lab_with(tmp_path, full_scan_interval=1800))
    try:
        w = lab.wallets[3]
        start = watermarks(lab)["W4"]
        token = lab.server.holdings[w][0]
        netted = [lab.server.add_transfer(sender=address("a"), recipient=w, block=lab.server.head + 1,
                                          token=token, amount=10**18),
                  lab.server.add_transfer(sender=w, recipient=address("b"), block=lab.server.head + 1,
                                          token=token, amount=10**18)]
        result, recs = until_full(lab, limit=25)[-1]
        assert result.head_block - start > 64 + 64, "the gap is wider than the window"
        transfers = [r for r in recs if r["method"] == "alchemy_getAssetTransfers" and r["wallet"] == "W4"]
        assert len(transfers) == 2, "one request per side, whatever the range"
        assert {t["uniqueId"] for t in netted} <= stored_keys(lab)
    finally:
        lab.close()


def test_a_restart_after_a_scan_that_did_not_finish_scans_everything_again(lab):
    before = watermarks(lab)
    raws = [lab.server.add_transfer(sender=address("stranger"), recipient=w, block=lab.server.head + 1)
            for w in lab.wallets[:2]]
    lab.server.failures += [("alchemy_getAssetTransfers", 503, "W2 transfers-in")] * 3
    result, _ = lab.cycle()
    assert not result.ok and watermarks(lab) == before, "an unfinished scan moves nothing"
    lab.start()                                       # the process dies and comes back
    lab.next_wait = 90
    result, recs = lab.cycle()
    assert result.ok and result.scan == "full"
    assert {r["uniqueId"] for r in raws} <= stored_keys(lab)
    assert activity_rows(lab) == len(stored_keys(lab))


def test_wallets_with_different_watermarks_each_resume_from_their_own(lab):
    for _ in range(3):
        lab.server.add_transfer(sender=address("stranger"), recipient=lab.wallets[0],
                                block=lab.server.head + 1)
        lab.cycle()
    marks = watermarks(lab)
    assert len(set(marks.values())) == 2, "W1 ahead, the rest where the last full scan left them"
    result, recs = until_full(lab)[-1]
    assert result.from_block == min(marks.values()) - 63
    assert set(watermarks(lab).values()) == {result.head_block}


def test_a_transfer_between_two_watched_wallets_is_scanned_on_both_and_stored_once(lab):
    w1, w2 = lab.wallets[:2]
    raw = lab.server.add_transfer(sender=w1, recipient=w2, block=lab.server.head + 1)
    rows = activity_rows(lab)
    result, recs = lab.cycle()
    assert result.scan == "changed" and result.changed == 2 and result.scanned == 2
    assert raw["uniqueId"] in stored_keys(lab)
    assert activity_rows(lab) == rows + 1, "one event, not one per side"
    until_full(lab)
    assert activity_rows(lab) == rows + 1, "and the full scan does not add it again"


def test_full_scan_interval_zero_is_the_original_algorithm(tmp_path):
    """FULL_SCAN_INTERVAL_SECONDS=0: every cycle scans every wallet and reads
    balances on the old timer — the request pattern S10a measured."""
    lab = lab_with(tmp_path, "zero", full_scan_interval=0)
    try:
        warm(lab)
        for _ in range(12):
            result, recs = lab.cycle()
            assert result.scan == "full"
            assert by_method(recs)["alchemy_getAssetTransfers"] == FULL_TRANSFERS
            assert by_method(recs)["eth_blockNumber"] == 1
    finally:
        lab.close()
