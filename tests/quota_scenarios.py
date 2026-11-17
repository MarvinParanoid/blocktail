"""Scenarios A–J of the Alchemy quota audit, on tests/quota_lab.py.

Each returns what the application sent to Alchemy — counted per HTTP request,
by method and reason — so tests/test_alchemy_quota.py can hold the numbers
and docs/audit/alchemy-quota.md can quote them (`python -m tests.quota_lab`).
"""

from __future__ import annotations

import tempfile
from collections import Counter
from pathlib import Path

from tests.quota_lab import (
    CU,
    CU_SOURCE,
    FREE_PLAN_CU_PER_MONTH,
    PRODUCTION,
    Holding,
    Lab,
    by_method,
    by_reason,
    cu,
    warm,
)
from tests.conftest import address

HOUR_CYCLES = 3600 // PRODUCTION["sync_interval"]      # 40: whole cadences of 4 (prices) and 7 (balances) fit


def _lab(**kwargs) -> Lab:
    return Lab(Path(tempfile.mkdtemp(prefix="quota-")), **kwargs)


def _hour(lab: Lab, **kwargs) -> list[dict]:
    records: list[dict] = []
    for _ in range(HOUR_CYCLES):
        result, recs = lab.cycle(**kwargs)
        assert result.ok, result.error
        records += recs
    return records


def _summary(records: list[dict]) -> dict:
    return {"requests": len(records), "cu": cu(records), "methods": dict(by_method(records)),
            "reasons": dict(by_reason(records))}


# --------------------------------------------------------------- scenarios


def a_idle_no_blocks() -> dict:
    """A: nothing happens at all — not even new blocks."""
    lab = _lab()
    try:
        warm(lab)
        _, one = lab.cycle(blocks=0)
        hour = _hour(lab, blocks=0)
        return {"cycle": _summary(one), "hour": _summary(hour)}
    finally:
        lab.close()


def b_blocks_no_transfers() -> dict:
    """B: the chain moves on (90 s ≈ 7 blocks); no wallet is touched."""
    lab = _lab()
    try:
        warm(lab)
        _, one = lab.cycle()
        hour = _hour(lab)
        return {"cycle": _summary(one), "hour": _summary(hour),
                "scanned_blocks": lab.ctx.indexer.settings.reorg_depth + 7}
    finally:
        lab.close()


def _event_cycle(lab: Lab, add) -> tuple[list[dict], list[dict]]:
    """A quiet cycle, then one with the event: (baseline, with event)."""
    _, base = lab.cycle()
    add(lab)
    _, event = lab.cycle()
    return base, event


def c_eth_received() -> dict:
    """C: one wallet receives ETH."""
    lab = _lab()
    try:
        warm(lab)
        w1 = lab.wallets[0]

        def add(lab):
            lab.server.add_transfer(sender=address("stranger"), recipient=w1,
                                    block=lab.server.head + 3)
        base, event = _event_cycle(lab, add)
        return {"baseline": _summary(base), "event": _summary(event),
                "extra": dict(by_method(event) - by_method(base))}
    finally:
        lab.close()


def d_token_received(*, decimals_in_transfer: bool = True) -> dict:
    """D: one wallet receives an ERC-20 it never held."""
    lab = _lab()
    try:
        warm(lab)
        w1 = lab.wallets[0]
        token = Holding(address("quota-new-token"), "NEW", 6)

        def add(lab):
            sent = token if decimals_in_transfer else Holding(token.contract, token.symbol, None)
            lab.server.add_transfer(sender=address("stranger"), recipient=w1,
                                    block=lab.server.head + 3, token=sent, move_balances=False)
            lab.server.holdings[w1] = [*lab.server.holdings[w1], token]
        base, event = _event_cycle(lab, add)
        _, after = lab.cycle()
        return {"baseline": _summary(base), "event": _summary(event),
                "extra": dict(by_method(event) - by_method(base)), "next": _summary(after)}
    finally:
        lab.close()


def e_overlapping_ranges() -> dict:
    """E: wallets scanned over different ranges in one cycle — one far behind
    (a wallet re-added after a long gap) with 2,500 transfers in its range,
    and a transfer between two watched wallets."""
    lab = _lab()
    try:
        warm(lab)
        w1, w2, w3 = lab.wallets[:3]
        lab.server.add_transfer(sender=w1, recipient=w2, block=lab.server.head + 2)
        far = lab.server.head - 50_000
        for i in range(2_500):
            lab.server.add_transfer(sender=address(f"payer-{i}"), recipient=w3, block=far + i)
        account = next(a for a in lab.ctx.db.list_accounts() if a.name == "W3")
        lab.ctx.db.connect().execute(
            "UPDATE accounts SET synced_to_block = ? WHERE id = ?", (far - 10, account.id))
        lab.ctx.db.connect().commit()
        result, recs = lab.cycle()
        assert result.ok, result.error
        per_wallet = Counter((r["wallet"], r["reason"]) for r in recs
                             if r["method"] == "alchemy_getAssetTransfers")
        return {"cycle": _summary(recs), "stored": result.stored,
                "transfer_calls_per_wallet": {f"{w} {why}": n for (w, why), n in sorted(per_wallet.items())}}
    finally:
        lab.close()


def f_transient_error() -> dict:
    """F: one getAssetTransfers answers 503 once."""
    lab = _lab()
    try:
        warm(lab)
        lab.server.failures.append(("alchemy_getAssetTransfers", 503))
        slept_before = len(lab.clock.slept)
        result, recs = lab.cycle()
        return {"ok": result.ok, "cycle": _summary(recs),
                "retries": [r for r in recs if r["attempt"] > 1],
                "backoff_seconds": lab.clock.slept[slept_before:]}
    finally:
        lab.close()


def g_throttled() -> dict:
    """G: Alchemy throttles — 429 twice on one call; then a provider that
    fails every attempt (503 × 5) on one call."""
    lab = _lab()
    try:
        warm(lab)
        lab.server.failures += [("alchemy_getAssetTransfers", 429)] * 2
        slept_before = len(lab.clock.slept)
        result, recs = lab.cycle()
        throttled = {"ok": result.ok, "cycle": _summary(recs),
                     "backoff_seconds": lab.clock.slept[slept_before:]}
        # As many 503s as the provider tries a 5xx (3 since S10b.1): this call fails.
        attempts = lab.ctx.provider._max_server_attempts
        lab.server.failures += [("alchemy_getAssetTransfers", 503, "W1 transfers-out")] * attempts
        watermark = lab.ctx.db.get_sync_status(lab.ctx.chain.chain_id).last_synced_block
        result, recs = lab.cycle()
        failing = {"ok": result.ok, "error": result.error, "cycle": _summary(recs),
                   "attempts_on_failing_call": sum(1 for r in recs if r["wallet"] == "W1"
                                                   and r["reason"] == "transfers-out"),
                   "watermark_moved": lab.ctx.db.get_sync_status(
                       lab.ctx.chain.chain_id).last_synced_block != watermark}
        _, recovered = lab.cycle()
        return {"throttled": throttled, "failing": failing, "next": _summary(recovered)}
    finally:
        lab.close()


def h_history_job() -> dict:
    """H: a reader asks for older history; the next cycle reaches back for
    every wallet, finishes the job, and the one after is ordinary again."""
    lab = _lab()
    try:
        warm(lab)
        lab.ctx.db.request_history(lab.ctx.chain.chain_id, lab.ctx.settings.backfill_blocks)
        status = lab.ctx.db.get_sync_status(lab.ctx.chain.chain_id).history_status
        result, during = lab.cycle()
        done = lab.ctx.db.get_sync_status(lab.ctx.chain.chain_id).history_status
        _, after = lab.cycle()
        return {"status_before": status, "status_after": done, "during": _summary(during),
                "after": _summary(after), "from_block": result.from_block}
    finally:
        lab.close()


def i_restart() -> dict:
    """I: the application restarts (deploy, crash, reboot) and continues."""
    lab = _lab()
    try:
        warm(lab)
        lab.cycle()
        lab.server.take()
        lab.start()                        # new provider caches, new indexer cadences
        result, first = lab.cycle()
        _, second = lab.cycle()
        return {"first": _summary(first), "second": _summary(second),
                "from_block": result.from_block, "head": result.head_block}
    finally:
        lab.close()


def j_no_ui() -> dict:
    """J: nobody opens the page. Everything above is the background; the page
    adds a provider call only when a transaction is opened (and "older
    history", scenario H)."""
    lab = _lab()
    try:
        warm(lab)
        hour = _hour(lab)
        lab.run(lab.ctx.provider.get_transaction_cost("0x" + "ab" * 32))
        opened = lab.server.take()
        return {"hour": _summary(hour), "one_transaction_opened": _summary(opened)}
    finally:
        lab.close()


# ------------------------------------------------------------------- model


# Prices every 4th cycle (≥300 s at a 90 s tick) and the balance sweep every
# 7th (≥600 s): 28 cycles hold a whole number of both, 280 cycles = 7 hours.
MODEL_CYCLES = 280


def model(**settings) -> dict:
    """Idle cost (scenario B) at production's settings — or others — averaged
    over MODEL_CYCLES simulated cycles, scaled to an hour, a day, 30 days."""
    lab = _lab(settings=settings or None)
    try:
        warm(lab)
        records: list[dict] = []
        for _ in range(MODEL_CYCLES):
            result, recs = lab.cycle()
            assert result.ok, result.error
            records += recs
        interval = lab.params["sync_interval"]
    finally:
        lab.close()
    cycles_per_day = 86_400 / interval
    scale = cycles_per_day / MODEL_CYCLES
    total_cu = cu(records)
    by_cu = Counter()
    for r in records:
        by_cu[r["method"]] += CU[r["method"]]
    day_cu = total_cu * scale
    return {
        "interval": interval, "cycles_per_day": cycles_per_day,
        "per_cycle_requests": len(records) / MODEL_CYCLES, "per_cycle_cu": total_cu / MODEL_CYCLES,
        "hour_requests": len(records) * scale / 24, "hour_cu": day_cu / 24,
        "day_requests": len(records) * scale, "day_cu": day_cu, "month_cu": day_cu * 30,
        "free_plan_share": day_cu * 30 / FREE_PLAN_CU_PER_MONTH,
        "days_to_free_limit": FREE_PLAN_CU_PER_MONTH / day_cu,
        "day_cu_by_method": {k: v * scale for k, v in by_cu.most_common()},
        "day_requests_by_method": {k: v * scale for k, v in by_method(records).most_common()},
    }


def report() -> str:
    m = model()
    lines = [f"CU source: {CU_SOURCE}", f"Settings: {PRODUCTION}, 7 wallets, 13 tokens", ""]
    lines.append(f"Idle: {m['per_cycle_requests']:.2f} requests and {m['per_cycle_cu']:.1f} CU per cycle; "
                 f"{m['hour_requests']:.0f} requests/h, {m['hour_cu']:,.0f} CU/h, "
                 f"{m['day_requests']:,.0f} requests/day, {m['day_cu']:,.0f} CU/day, "
                 f"{m['month_cu']:,.0f} CU/30 days = {m['free_plan_share']:.0%} of the free plan; "
                 f"free plan lasts {m['days_to_free_limit']:.1f} days")
    lines.append("CU per day by method: " + ", ".join(
        f"{k} {v:,.0f} ({v / m['day_cu']:.1%})" for k, v in m["day_cu_by_method"].items()))
    lines.append("Requests per day by method: " + ", ".join(
        f"{k} {v:,.0f}" for k, v in m["day_requests_by_method"].items()))
    for name, fn in [("A", a_idle_no_blocks), ("B", b_blocks_no_transfers), ("C", c_eth_received),
                     ("D", d_token_received),
                     ("D2", lambda: d_token_received(decimals_in_transfer=False)),
                     ("E", e_overlapping_ranges), ("F", f_transient_error), ("G", g_throttled),
                     ("H", h_history_job), ("I", i_restart), ("J", j_no_ui)]:
        lines.append(f"{name}: {fn()}")
    return "\n".join(lines)
