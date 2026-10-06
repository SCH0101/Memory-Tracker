"""LLM unit arbiter — reviews datapoints that disagree >=2x with peers.

For each (metric, year) group, any value >=ARBITRATE_RATIO away from the peer
median is sent to Haiku with full context. The verdict per datapoint:
  - keep:    legitimate broker disagreement -> remembered, never re-asked
  - convert: wrong unit/equivalence basis -> value corrected in place AND the
             (metric, raw-unit) -> factor rule persisted to unit_rules.json so
             every future extraction converts automatically
  - delete:  value does not belong to this metric -> removed

Verdicts are recorded in the `unit_arbitrations` table so each datapoint is
arbitrated at most once.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
from collections import defaultdict
from datetime import datetime

from ..db.connection import get_db
from .datapoints import CANONICAL_METRICS, ensure_datapoints_table, save_learned_rule

logger = logging.getLogger(__name__)

_HAIKU_MODEL = os.getenv("OPENAI_EXTRACTION_MODEL", "gpt-5.6-luna")
ARBITRATE_RATIO = 2.0     # peer-disagreement threshold that triggers review
MAX_FACTOR_DRIFT = 3.0    # converted value must land within this of peer median


def _median(vals: list[float]) -> float:
    s = sorted(vals)
    return s[len(s) // 2] if s else 0.0


async def _ensure_arbitration_table() -> None:
    db = await get_db()
    await db.execute(
        """CREATE TABLE IF NOT EXISTS unit_arbitrations (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               datapoint_key TEXT NOT NULL UNIQUE,
               metric TEXT NOT NULL,
               action TEXT NOT NULL,
               factor REAL,
               reason TEXT,
               created_at TEXT NOT NULL
           )"""
    )
    await db.commit()


def _dp_key(r) -> str:
    return f"{r['metric']}|{r['period']}|{r['broker']}|{r['source_filename']}|{r['value']}"


def _build_arbiter_prompt(metric: str, canon_unit: str, candidates: list[dict],
                          peers_desc: str) -> str:
    cand_lines = "\n".join(
        f'  {i}: broker={c["broker"]} period={c["period"]} value={c["value"]} '
        f'stored_unit="{c["unit"]}" unit_as_printed="{c["raw_unit"]}" '
        f'source="{c["source_filename"]}"'
        for i, c in enumerate(candidates)
    )
    return f"""You are auditing numeric datapoints on a memory-industry (DRAM/NAND/HBM) dashboard.

METRIC: {metric}
CANONICAL UNIT: {canon_unit}
PEER VALUES from other brokers for the same periods (assumed correctly scaled):
{peers_desc}

These candidate datapoints disagree with peers by >=2x:
{cand_lines}

For EACH candidate decide:
- "keep"    — plausible genuine broker disagreement in the SAME unit basis,
  OR a different but legitimate SCOPE (pure-play subset, single region/vendor,
  different product mix). NEVER scale scope differences to match peers — a
  subset number is correct data, just narrower. Keep it.
- "convert" — same metric but a different UNIT/equivalence basis. ONLY convert
  when the factor is a CLEAN unit ratio: powers of 2 equivalence bases
  (x2, x8, x16, x32, x0.125, x0.032), metric prefixes (x1000, x0.001),
  or time bases (x1/7, x1/12). If no clean ratio lands the value near peers,
  DO NOT invent a fitted factor — keep or delete instead. Give "factor" and
  "learned_unit" = the unit string EXACTLY as printed in unit_as_printed.
  If unit_as_printed is empty, leave learned_unit empty (no rule is saved;
  the value is still converted).
- "delete"  — the value does not belong to this metric at all (misclassified
  row, page number, footnote, unclear base that cannot be recovered).

Memory-industry anchors (2026, for sanity): DRAM bit demand ~300-450 bn Gb;
NAND bit demand ~1200-1800 EB; DRAM industry wafer capacity ~600-2800 kwpm;
HBM wafer capacity ~300-700 kwpm; DRAM contract ASP ~$1-2.5/Gb; NAND ~$0.2-0.6/GB;
memory capex ~$50-180bn/yr; inventory ~4-40 weeks.
NOTE: sufficiency metrics CAN be negative (negative = undersupply/deficit,
e.g. "DRAM sufficiency -17%" is a normal shortage forecast — keep those).
Growth metrics can legitimately be negative too.

Return STRICT JSON only:
{{"verdicts": [{{"index": 0, "action": "keep|convert|delete", "factor": 1.0, "learned_unit": "", "reason": "<short>"}}]}}"""


async def _call_haiku(prompt: str) -> dict:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return {}
    from .openai_research import AsyncResearchClient
    client = AsyncResearchClient(api_key=api_key)
    try:
        async with client.messages.stream(
            model=_HAIKU_MODEL, max_tokens=4000, temperature=0,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            resp = await stream.get_final_message()
        text = "".join(getattr(b, "text", "") for b in resp.content).strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", text, re.DOTALL)
            return json.loads(m.group(0)) if m else {}
    except Exception as exc:
        logger.warning("Arbiter LLM call failed: %s", exc)
        return {}


async def arbitrate_metric_outliers(metrics: set[str] | None = None) -> dict:
    """Find >=2x peer-disagreements and let the LLM keep/convert/delete each.

    Returns {"reviewed": n, "kept": n, "converted": n, "deleted": n}."""
    await ensure_datapoints_table()
    await _ensure_arbitration_table()
    db = await get_db()

    cur = await db.execute(
        """SELECT id, metric, period, period_int, value, unit, raw_unit,
                  broker, source_filename FROM datapoints WHERE value != 0"""
    )
    rows = [dict(r) for r in await cur.fetchall()]
    if metrics:
        in_scope = [r for r in rows if r["metric"] in metrics]
    else:
        in_scope = rows

    cur = await db.execute("SELECT datapoint_key FROM unit_arbitrations")
    already = {r["datapoint_key"] for r in await cur.fetchall()}

    # Group ALL rows by (metric, year) to compute peer medians
    groups: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for r in rows:
        groups[(r["metric"], r["period_int"] // 10000)].append(r)

    # Collect candidates per metric
    by_metric: dict[str, list[dict]] = defaultdict(list)
    peer_info: dict[str, list[str]] = defaultdict(list)
    for (metric, year), grp in groups.items():
        if metrics and metric not in metrics:
            continue
        if len(grp) < 2:
            continue
        vals = [abs(r["value"]) for r in grp]
        med = _median(vals)
        if med <= 0:
            continue
        for r in grp:
            v = abs(r["value"])
            if (v / med > ARBITRATE_RATIO or med / v > ARBITRATE_RATIO) \
                    and _dp_key(r) not in already:
                r["_peer_median"] = med
                by_metric[metric].append(r)
        peer_info[metric].append(
            f"  {year}: median={med:.4g}, values={sorted(round(v, 3) for v in vals)[:8]}"
        )

    stats = {"reviewed": 0, "kept": 0, "converted": 0, "deleted": 0}
    now = datetime.now().isoformat()

    for metric, candidates in by_metric.items():
        candidates = candidates[:20]  # cap per metric per run
        canon_unit = CANONICAL_METRICS.get(metric, ("", ""))[0]
        prompt = _build_arbiter_prompt(
            metric, canon_unit, candidates, "\n".join(peer_info[metric]))
        result = await _call_haiku(prompt)
        verdicts = {v.get("index"): v for v in result.get("verdicts", [])
                    if isinstance(v, dict)}
        for i, r in enumerate(candidates):
            v = verdicts.get(i)
            if not v:
                continue
            action = str(v.get("action", "keep")).strip().lower()
            factor = v.get("factor")
            reason = str(v.get("reason", ""))[:200]
            stats["reviewed"] += 1

            if action == "convert" and isinstance(factor, (int, float)) and factor > 0:
                new_val = r["value"] * factor
                med = r["_peer_median"]
                drift = max(abs(new_val), 1e-12) / max(med, 1e-12)
                if drift > MAX_FACTOR_DRIFT or drift < 1 / MAX_FACTOR_DRIFT:
                    action = "keep"  # conversion doesn't land near peers — distrust it
                else:
                    await db.execute(
                        "UPDATE datapoints SET value = ?, unit = ? WHERE id = ?",
                        (new_val, canon_unit, r["id"]),
                    )
                    stats["converted"] += 1
                    learned = str(v.get("learned_unit", "")).strip()
                    if learned:
                        save_learned_rule(metric, learned, float(factor))
                        logger.info("Learned unit rule: %s | '%s' -> x%g",
                                    metric, learned, factor)
                    logger.info("Arbiter CONVERTED %s %s %s: %g -> %g (%s)",
                                metric, r["broker"], r["period"],
                                r["value"], new_val, reason)
            if action == "delete":
                await db.execute("DELETE FROM datapoints WHERE id = ?", (r["id"],))
                stats["deleted"] += 1
                logger.info("Arbiter DELETED %s %s %s = %g (%s)",
                            metric, r["broker"], r["period"], r["value"], reason)
            elif action == "keep":
                stats["kept"] += 1

            await db.execute(
                """INSERT OR IGNORE INTO unit_arbitrations
                   (datapoint_key, metric, action, factor, reason, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (_dp_key(r), metric, action,
                 float(factor) if isinstance(factor, (int, float)) else None,
                 reason, now),
            )
        await db.commit()

    return stats
