"""Cross-broker datapoint audit: flag and remove magnitude outliers.

For each (metric, year), values from different brokers/sources should agree
within OUTLIER_RATIO (default 30x). Anything further off the peer median is a
unit/classification error and gets deleted.

Run:           python memory_chart/audit_datapoints.py          (report only)
Apply fixes:   python memory_chart/audit_datapoints.py --apply
"""
from __future__ import annotations

import asyncio
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from memory_chart.backend.db.connection import get_db, close_db

OUTLIER_RATIO = 30.0


def _median(vals: list[float]) -> float:
    s = sorted(vals)
    return s[len(s) // 2] if s else 0.0


async def main() -> None:
    apply = "--apply" in sys.argv
    db = await get_db()
    cur = await db.execute(
        """SELECT id, metric, period, period_int, value, unit, broker, source_filename
           FROM datapoints WHERE value != 0"""
    )
    rows = await cur.fetchall()

    groups: dict[tuple[str, int], list] = defaultdict(list)
    for r in rows:
        groups[(r["metric"], r["period_int"] // 10000)].append(r)

    # Metric-wide medians (all periods) — tiebreaker for 2-value groups
    metric_vals: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        metric_vals[r["metric"]].append(abs(r["value"]))
    metric_med = {m: _median(v) for m, v in metric_vals.items()}

    bad_ids: list[int] = []
    for (metric, year), grp in sorted(groups.items()):
        vals = [abs(r["value"]) for r in grp]
        if len(grp) >= 3:
            med = _median(vals)
            if med <= 0:
                continue
            for r in grp:
                v = abs(r["value"])
                if v / med > OUTLIER_RATIO or med / v > OUTLIER_RATIO:
                    print(f"OUTLIER {metric} {r['period']}: {r['broker']} = {r['value']} {r['unit']}"
                          f"  (peer median {med:.3g})  <- {r['source_filename']}")
                    bad_ids.append(r["id"])
        elif len(grp) == 2 and min(vals) > 0 and max(vals) / min(vals) > OUTLIER_RATIO:
            # Two values disagreeing >30x: drop the one further (log-scale)
            # from the metric's all-period median
            import math
            mm = metric_med.get(metric, 0)
            if mm <= 0:
                continue
            r_bad = max(grp, key=lambda r: abs(math.log10(max(abs(r["value"]), 1e-12) / mm)))
            print(f"OUTLIER-PAIR {metric} {r_bad['period']}: {r_bad['broker']} = {r_bad['value']} "
                  f"{r_bad['unit']}  (metric median {mm:.3g})  <- {r_bad['source_filename']}")
            bad_ids.append(r_bad["id"])

    if not bad_ids:
        print("No cross-broker outliers found.")
    elif apply:
        for bid in bad_ids:
            await db.execute("DELETE FROM datapoints WHERE id = ?", (bid,))
        await db.commit()
        print(f"\nDeleted {len(bad_ids)} outlier rows.")
    else:
        print(f"\n{len(bad_ids)} outliers found. Re-run with --apply to delete them.")
    await close_db()


asyncio.run(main())
