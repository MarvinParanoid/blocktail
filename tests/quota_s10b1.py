"""S10b.1 numbers for docs/audit/alchemy-quota-s10b1.md: idle spend with the
two-level sync, beside S10a's baseline, on tests/quota_lab.py.

    python -m tests.quota_s10b1
"""

from __future__ import annotations

import tempfile
from collections import Counter
from pathlib import Path

from tests.quota_lab import CU, FREE_PLAN_CU_PER_MONTH, TWO_LEVEL, Lab, by_method, cu, warm

BASELINE_CU_PER_DAY = 1_680_000     # S10a, measured the same way (tests/quota_scenarios.model)
# A full scan every 9th cycle (due ≥ 810 s after the last, so never more than
# 900 s apart) and prices every 4th: 36 cycles hold a whole number of both.
MODEL_CYCLES = 360


def model(**settings) -> dict:
    lab = Lab(Path(tempfile.mkdtemp(prefix="s10b1-")), settings={**TWO_LEVEL, **settings})
    try:
        warm(lab)
        for _ in range(20):
            if lab.cycle()[0].scan == "full":
                break
        else:
            raise AssertionError("no full scan within 20 cycles")
        lab.server.take()
        records, scans = [], Counter()
        for _ in range(MODEL_CYCLES):
            result, recs = lab.cycle()
            assert result.ok, result.error
            records += recs
            scans[result.scan] += 1
        interval = lab.params["sync_interval"]
    finally:
        lab.close()
    scale = 86_400 / interval / MODEL_CYCLES
    day_cu = cu(records) * scale
    by_cu = Counter()
    for r in records:
        by_cu[r["method"]] += CU[r["method"]]
    return {
        "day_cu": day_cu, "month_cu": day_cu * 30, "hour_cu": day_cu / 24,
        "day_requests": len(records) * scale,
        "reduction": 1 - day_cu / BASELINE_CU_PER_DAY,
        "free_plan_share": day_cu * 30 / FREE_PLAN_CU_PER_MONTH,
        "scans_per_day": {k: v * scale for k, v in scans.items()},
        "day_cu_by_method": {k: v * scale for k, v in by_cu.most_common()},
        "day_requests_by_method": {k: v * scale for k, v in by_method(records).most_common()},
    }


def report() -> str:
    m = model()
    lines = [
        f"two-level idle: {m['day_requests']:,.0f} requests/day, {m['day_cu']:,.0f} CU/day, "
        f"{m['hour_cu']:,.0f} CU/h, {m['month_cu']:,.0f} CU/30 days "
        f"({m['free_plan_share']:.0%} of the free plan); "
        f"{m['reduction']:.1%} less than the baseline {BASELINE_CU_PER_DAY:,} CU/day",
        "cycles per day: " + ", ".join(f"{k} {v:,.1f}" for k, v in m["scans_per_day"].items()),
        "CU per day by method: " + ", ".join(
            f"{k} {v:,.0f} ({v / m['day_cu']:.1%})" for k, v in m["day_cu_by_method"].items()),
        "requests per day by method: " + ", ".join(
            f"{k} {v:,.0f}" for k, v in m["day_requests_by_method"].items()),
    ]
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover
    print(report())
