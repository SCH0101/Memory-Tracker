"""Cached dashboard time-series payload builder.

This module builds the chart JSON from the materialized gold/quarantine tables
only. It must not rebuild raw/verified datapoints or call RAG/LLM paths.
"""
from __future__ import annotations

import math
import re
from collections import Counter

from ..db.connection import get_db
from .datapoints import CANONICAL_METRICS
from .dashboard_datapoints import ensure_dashboard_datapoint_tables

SEGMENT_ORDER = ["DRAM", "DDR", "NAND", "HBM", "General"]
PEER_OUTLIER_RATIO = 2.0

METRIC_LABELS = {
    "dram_bit_demand": "DRAM Bit Demand",
    "dram_bit_supply": "DRAM Bit Supply",
    "dram_bit_growth": "DRAM Bit Growth",
    "dram_sufficiency": "DRAM Sufficiency",
    "dram_contract_asp": "DRAM Contract ASP",
    "dram_spot_asp": "DRAM Spot ASP",
    "dram_inventory_weeks": "DRAM Inventory",
    "dram_wafer_capacity": "DRAM Wafer Capacity",
    "dram_capex": "DRAM Capex",
    "dram_utilization": "DRAM Utilization",
    "ddr4_contract_price": "DDR4 Contract Price",
    "ddr5_contract_price": "DDR5 Contract Price",
    "ddr4_spot_price": "DDR4 Spot Price",
    "ddr5_spot_price": "DDR5 Spot Price",
    "nand_bit_demand": "NAND Bit Demand",
    "nand_bit_supply": "NAND Bit Supply",
    "nand_bit_growth": "NAND Bit Growth",
    "nand_sufficiency": "NAND Sufficiency",
    "nand_contract_asp": "NAND Contract ASP",
    "nand_spot_asp": "NAND Spot ASP",
    "nand_wafer_price": "NAND Wafer Price",
    "nand_inventory_weeks": "NAND Inventory",
    "nand_wafer_capacity": "NAND Wafer Capacity",
    "nand_capex": "NAND Capex",
    "nand_utilization": "NAND Utilization",
    "hbm_tam": "HBM TAM",
    "hbm_bit_demand": "HBM Bit Demand",
    "hbm_content_per_accelerator": "HBM Content / Accelerator",
    "hbm_wafer_capacity": "HBM Wafer Capacity",
    "hbm_asp": "HBM ASP",
    "memory_capex": "Memory Capex",
    "memory_revenue": "Memory Revenue",
    "cloud_capex": "Cloud Capex",
    "server_dram_share": "Server DRAM Share",
    "dram_share_samsung": "Samsung",
    "dram_share_skhynix": "SK Hynix",
    "dram_share_micron": "Micron",
    "dram_share_others": "Others",
    "nand_share_samsung": "Samsung",
    "nand_share_skhynix": "SK Hynix / Solidigm",
    "nand_share_kioxia": "Kioxia",
    "nand_share_wdc": "WDC / SanDisk",
    "nand_share_micron": "Micron",
    "nand_share_ymtc": "YMTC",
    "nand_share_others": "Others",
    "hbm_share_skhynix": "SK Hynix",
    "hbm_share_samsung": "Samsung",
    "hbm_share_micron": "Micron",
}

ABS_BIT_METRICS = {
    "dram_bit_demand", "dram_bit_supply",
    "nand_bit_demand", "nand_bit_supply", "hbm_bit_demand",
}

RAW_UNIT_TO_MGB = {
    "bngb": 125.0,
    "bngbit": 125.0,
    "gb": 1e-6,
    "gbmn": 1.0,
    "mngb": 1.0,
    "mgb": 1.0,
    "mmgb": 1.0,
    "eb": 1000.0,
    "pb": 1.0,
    "tb": 1e-3,
    "gbbn": 1000.0,
    "bngb1gbeq": 125.0,
}

MERGED_PANELS = [
    {"key": "ddr4_price", "segment": "DDR", "label": "DDR4 Price - Contract vs Spot",
     "unit": "$/unit", "metrics": {"ddr4_contract_price": "Contract", "ddr4_spot_price": "Spot"}},
    {"key": "ddr5_price", "segment": "DDR", "label": "DDR5 Price - Contract vs Spot",
     "unit": "$/unit", "metrics": {"ddr5_contract_price": "Contract", "ddr5_spot_price": "Spot"}},
    {"key": "dram_supply_demand_abs", "segment": "DRAM",
     "label": "DRAM Supply vs Demand - Absolute",
     "unit": "M GB", "metrics": {"dram_bit_demand": "Demand", "dram_bit_supply": "Supply"},
     "raw": True},
    {"key": "nand_supply_demand_abs", "segment": "NAND",
     "label": "NAND Supply vs Demand - Absolute",
     "unit": "M GB", "metrics": {"nand_bit_demand": "Demand", "nand_bit_supply": "Supply"},
     "raw": True},
    {"key": "hbm_demand_abs", "segment": "HBM",
     "label": "HBM Bit Demand - Absolute",
     "unit": "M GB", "metrics": {"hbm_bit_demand": "Demand"},
     "raw": True},
    {"key": "inventory_weeks", "segment": "General", "label": "Inventory Weeks - DRAM vs NAND",
     "unit": "weeks", "metrics": {"dram_inventory_weeks": "DRAM", "nand_inventory_weeks": "NAND"}},
    {"key": "utilization", "segment": "General", "label": "Utilization - DRAM vs NAND",
     "unit": "%", "metrics": {"dram_utilization": "DRAM", "nand_utilization": "NAND"}},
    {"key": "capex_all", "segment": "General", "label": "Memory Capex - DRAM / NAND / Total",
     "unit": "$bn", "metrics": {"dram_capex": "DRAM", "nand_capex": "NAND", "memory_capex": "Total"}},
    {"key": "dram_wafer_starts", "segment": "DRAM", "label": "DRAM Wafer Starts",
     "unit": "K WSPM", "metrics": {"dram_wafer_capacity": "Capacity"}},
    {"key": "nand_wafer_starts", "segment": "NAND", "label": "NAND Wafer Starts",
     "unit": "K WSPM", "metrics": {"nand_wafer_capacity": "Capacity"}},
    {"key": "hbm_wafer_starts", "segment": "HBM", "label": "HBM Wafer Starts",
     "unit": "K WSPM", "metrics": {"hbm_wafer_capacity": "Capacity"}},
    {"key": "dram_market_share", "segment": "DRAM",
     "label": "DRAM Market Share by Producer",
     "unit": "%", "metrics": {
         "dram_share_samsung": "Samsung",
         "dram_share_skhynix": "SK Hynix",
         "dram_share_micron": "Micron",
         "dram_share_others": "Others",
     }},
    {"key": "nand_market_share", "segment": "NAND",
     "label": "NAND Market Share by Producer",
     "unit": "%", "metrics": {
         "nand_share_samsung": "Samsung",
         "nand_share_skhynix": "SK Hynix / Solidigm",
         "nand_share_kioxia": "Kioxia",
         "nand_share_wdc": "WDC / SanDisk",
         "nand_share_micron": "Micron",
         "nand_share_ymtc": "YMTC",
         "nand_share_others": "Others",
     }},
    {"key": "hbm_market_share", "segment": "HBM",
     "label": "HBM Market Share by Producer",
     "unit": "%", "metrics": {
         "hbm_share_skhynix": "SK Hynix",
         "hbm_share_samsung": "Samsung",
         "hbm_share_micron": "Micron",
     }},
]


def normalize_frequency(value: str | None) -> str:
    return "quarterly" if str(value or "").lower() == "quarterly" else "yearly"


def granularity(period: str) -> str:
    p = (period or "").upper()
    if "Q" in p:
        return "Q"
    if "-" in p:
        return "M"
    return "Y"


def period_frequency(period: str) -> str:
    g = granularity(period)
    if g == "Q":
        return "quarterly"
    if g == "M":
        return "monthly"
    return "yearly"


def period_frequency_sql(frequency: str, column: str = "period") -> str:
    if frequency == "quarterly":
        return f"(frequency='quarterly' OR (frequency='' AND instr(upper({column}), 'Q') > 0))"
    return (
        f"(frequency='yearly' OR (frequency='' AND instr(upper({column}), 'Q') = 0 "
        f"AND instr({column}, '-') = 0))"
    )


def _peer_gate_enabled(metric: str) -> bool:
    return not any(tok in (metric or "") for tok in ("growth", "sufficiency", "utilization", "share"))


def _peer_status(rows) -> tuple[dict[int, tuple[str, str]], int]:
    grouped: dict[tuple[str, str, int], list] = {}
    for r in rows:
        if not _peer_gate_enabled(r["metric"]):
            continue
        try:
            v = abs(float(r["value"]))
        except (TypeError, ValueError):
            continue
        if v <= 0:
            continue
        grouped.setdefault(
            (r["metric"], r.get("subject_entity") or "Industry Total", int(r["period_int"])),
            [],
        ).append(r)

    status: dict[int, tuple[str, str]] = {}
    suppressed = 0
    for (metric, _subject_entity, period_int), peers in grouped.items():
        vals = sorted(abs(float(r["value"])) for r in peers if abs(float(r["value"])) > 0)
        if len(vals) < 2:
            continue
        if len(vals) == 2:
            lo, hi = vals[0], vals[1]
            if lo > 0 and hi / lo >= PEER_OUTLIER_RATIO:
                note = (
                    f"Peer check: only two values for {metric} at {period_int}; "
                    f"spread is {hi / lo:.1f}x, review source before relying on it."
                )
                for r in peers:
                    status[int(r["id"])] = ("needs_review", note)
            continue
        med = vals[len(vals) // 2]
        if med <= 0:
            continue
        for r in peers:
            v = abs(float(r["value"]))
            if v / med >= PEER_OUTLIER_RATIO or med / v >= PEER_OUTLIER_RATIO:
                suppressed += 1
                status[int(r["id"])] = (
                    "suppressed",
                    f"Suppressed from chart: {metric} {r['period']} is >{PEER_OUTLIER_RATIO:.0f}x away from peer median {med:.3g}.",
                )
    return status, suppressed


def _longest_consistent_run(pts: list[dict], max_jump: float = 6.0) -> list[dict]:
    if len(pts) < 3:
        return pts
    runs: list[list[dict]] = []
    cur = [pts[0]]
    for prev, p in zip(pts, pts[1:]):
        a, b = abs(prev["value"]), abs(p["value"])
        lo, hi = min(a, b), max(a, b)
        if lo > 0 and hi / lo > max_jump:
            runs.append(cur)
            cur = [p]
        else:
            cur.append(p)
    runs.append(cur)
    return max(runs, key=lambda r: (len(r), r[-1]["period_int"]))


def _clean_series(pts: list[dict]) -> list[dict]:
    if not pts:
        return pts
    counts = Counter(granularity(p["period"]) for p in pts)
    dom = counts.most_common(1)[0][0]
    pts = [p for p in pts if granularity(p["period"]) == dom]
    vals = sorted(abs(p["value"]) for p in pts if p["value"])
    med = vals[len(vals) // 2] if vals else 0.0

    def dist(p: dict) -> float:
        v = abs(p["value"])
        if v <= 0 or med <= 0:
            return 999.0
        return abs(math.log10(v) - math.log10(med))

    by_period: dict[str, dict] = {}
    for p in pts:
        k = p["period"]
        if k not in by_period or dist(p) < dist(by_period[k]):
            by_period[k] = p
    return _longest_consistent_run(sorted(by_period.values(), key=lambda p: p["period_int"]))


def _unit_key(unit: str | None) -> str:
    return (
        (unit or "").strip().lower()
        .replace(" ", "").replace(".", "").replace("-", "")
        .replace("_", "").replace(",", "").replace("(", "")
        .replace(")", "").replace("'", "")
    )


def _compact_unit_preserve_case(unit: str | None) -> str:
    return re.sub(r"[\s._\-/(),]+", "", unit or "")


def _is_million_gbit_unit(unit: str | None) -> bool:
    compact = _compact_unit_preserve_case(unit)
    lower = compact.lower()
    if re.fullmatch(r"(?:m|mn|mm|million)g(?:bit|bits)", lower):
        return True
    return bool(re.fullmatch(r"(?:m|mn|mm|M|MN|MM)[Gg]b", compact))


def _to_mgb(value: float, unit: str | None) -> float | None:
    if value is None:
        return None
    if _is_million_gbit_unit(unit):
        return value / 8.0
    mult = RAW_UNIT_TO_MGB.get(_unit_key(unit))
    if mult is None:
        return None
    return value * mult


def _quarter_num(period: str) -> int | None:
    m = re.search(r"Q([1-4])", (period or "").upper())
    return int(m.group(1)) if m else None


def _is_excelish_row(row: dict) -> bool:
    filename = str(row.get("source_filename") or "").lower()
    extractor = str(row.get("extractor") or "").lower()
    return filename.endswith((".xlsx", ".xlsm")) or extractor.startswith("excel:")


def _derive_yearly_from_quarters(rows: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, str, str, int], dict[int, dict]] = {}
    for row in rows:
        if row.get("metric") not in ABS_BIT_METRICS:
            continue
        if period_frequency(str(row.get("period") or "")) != "quarterly":
            continue
        if not _is_excelish_row(row):
            continue
        q = _quarter_num(str(row.get("period") or ""))
        if not q:
            continue
        year = int(row.get("period_int") or 0) // 10000
        if year <= 0:
            continue
        key = (
            str(row.get("metric") or ""),
            str(row.get("broker") or ""),
            str(row.get("subject_entity") or "Industry Total"),
            year,
        )
        grouped.setdefault(key, {})[q] = row

    derived: list[dict] = []
    next_id = -1
    for (metric, broker, subject_entity, year), by_q in grouped.items():
        if set(by_q) != {1, 2, 3, 4}:
            continue
        pts = [by_q[q] for q in (1, 2, 3, 4)]
        units = {str(p.get("unit") or "") for p in pts}
        if len(units) != 1:
            continue
        first = dict(pts[0])
        first.update({
            "id": next_id,
            "metric": metric,
            "broker": broker,
            "subject_entity": subject_entity,
            "period": str(year),
            "period_int": year * 10000,
            "value": sum(float(p.get("value") or 0.0) for p in pts),
            "source_quote": "Derived from four quarterly workbook rows",
            "source_date": max(str(p.get("source_date") or "") for p in pts),
            "source_page": 0,
            "verification_status": "verified",
            "verification_note": "derived annual total from four quarterly Excel rows",
        })
        derived.append(first)
        next_id -= 1
    return derived


def _to_yoy_growth(pts: list[dict]) -> list[dict]:
    by_year = {p["period_int"] // 10000: p for p in pts if granularity(p["period"]) == "Y"}
    out = []
    for y, p in sorted(by_year.items()):
        prev = by_year.get(y - 1)
        if prev and prev["value"]:
            g = (p["value"] / prev["value"] - 1.0) * 100.0
            if -95.0 < g < 500.0:
                cur_src = p.get("source") or {}
                prev_src = prev.get("source") or {}
                out.append({
                    "period": p["period"],
                    "period_int": p["period_int"],
                    "value": round(g, 1),
                    "source": {
                        "filename": cur_src.get("filename") or prev_src.get("filename") or "",
                        "page": cur_src.get("page") or prev_src.get("page") or 0,
                        "quote": cur_src.get("quote") or prev_src.get("quote") or "",
                        "date": cur_src.get("date") or prev_src.get("date") or "",
                    },
                })
    return out


def _series_broker_key(label: str) -> str:
    return re.split(r"\s+-\s+", label or "", maxsplit=1)[0].strip().lower()


def _has_multiple_variants_per_broker(series: list[dict]) -> bool:
    counts = Counter(_series_broker_key(s.get("broker", "")) for s in series)
    return any(key and n > 1 for key, n in counts.items())


async def _max_id(db, table: str) -> int:
    cur = await db.execute(f"SELECT COALESCE(MAX(id), 0) AS m FROM {table}")
    row = await cur.fetchone()
    return int(row["m"] or 0) if row else 0


async def build_dashboard_timeseries_payload(frequency: str) -> dict:
    """Build the dashboard chart payload from materialized verified rows."""
    frequency = normalize_frequency(frequency)
    await ensure_dashboard_datapoint_tables()
    db = await get_db()
    if frequency == "yearly":
        metric_placeholders = ",".join("?" for _ in ABS_BIT_METRICS)
        where = (
            f"(({period_frequency_sql('yearly')}) OR "
            f"(metric IN ({metric_placeholders}) AND {period_frequency_sql('quarterly')}))"
        )
        params = tuple(ABS_BIT_METRICS)
    else:
        where = period_frequency_sql("quarterly")
        params = ()
    rows = await db.execute_fetchall(
        f"""SELECT id, segment, metric, period, period_int, value, unit, broker,
                   raw_unit, source_filename, source_page, source_quote, source_date,
                   verification_status, verification_note, extractor,
                   source_type, source_broker, subject_entity, subject_type,
                   frequency, winner_reason, canonical_value, canonical_unit
            FROM dashboard_datapoints_verified
            WHERE {where}
            ORDER BY segment, metric, broker, period_int""",
        params,
    )
    all_rows = [dict(r) for r in rows]
    if frequency == "yearly":
        derived_rows = _derive_yearly_from_quarters(all_rows)
        derived_keys = {
            (r["metric"], r["broker"], r.get("subject_entity") or "Industry Total", int(r["period_int"]))
            for r in derived_rows
        }
        rows = [
            r for r in all_rows
            if period_frequency(r["period"]) == "yearly"
            and (r["metric"], r["broker"], r.get("subject_entity") or "Industry Total", int(r["period_int"])) not in derived_keys
        ] + derived_rows
    else:
        rows = all_rows

    qcur = await db.execute(
        f"SELECT COUNT(*) AS n FROM dashboard_datapoints_quarantine WHERE {period_frequency_sql(frequency)}"
    )
    qrow = await qcur.fetchone()
    raw_max = await _max_id(db, "dashboard_datapoints_raw")
    verified_max = await _max_id(db, "dashboard_datapoints_verified")
    quarantine_max = await _max_id(db, "dashboard_datapoints_quarantine")
    version = f"{raw_max}-{verified_max}-{quarantine_max}-m16-{frequency}"

    peer_status, suppressed_count = _peer_status(rows)
    needs_review_count = sum(
        1 for r in rows
        if peer_status.get(
            int(r["id"]),
            (r["verification_status"] or "verified", r["verification_note"] or ""),
        )[0] == "needs_review"
    )

    by_metric: dict[str, dict[str, list[dict]]] = {}
    metric_segment: dict[str, str] = {}
    units: dict[str, str] = {}
    for r in rows:
        status, note = peer_status.get(
            int(r["id"]),
            (r["verification_status"] or "verified", r["verification_note"] or ""),
        )
        if status == "suppressed":
            continue
        met = r["metric"]
        source_broker = r["source_broker"] or r["broker"] or "Unknown"
        subject_entity = r["subject_entity"] or "Industry Total"
        brk = f"{source_broker} - {subject_entity}"
        units[met] = r["unit"]
        metric_segment[met] = r["segment"]
        by_metric.setdefault(met, {}).setdefault(brk, []).append({
            "period": r["period"],
            "period_int": r["period_int"],
            "value": r["value"],
            "unit": r["unit"],
            "raw_unit": r["raw_unit"] or "",
            "source": {
                "filename": r["source_filename"] or "",
                "page": int(r["source_page"] or 0),
                "quote": r["source_quote"] or "",
                "date": r["source_date"] or "",
                "source_type": r.get("source_type") or "",
                "winner_reason": r.get("winner_reason") or "",
                "source_broker": source_broker,
                "subject_entity": subject_entity,
            },
            "verification_status": status,
            "verification_note": note,
            "source_type": r.get("source_type") or "",
            "winner_reason": r.get("winner_reason") or "",
            "source_broker": source_broker,
            "subject_entity": subject_entity,
        })

    prepared: dict[str, list[dict]] = {}
    prepared_raw: dict[str, list[dict]] = {}
    for met, brokers in by_metric.items():
        as_growth = met in ABS_BIT_METRICS
        series = []
        series_raw = []
        for brk, pts in sorted(brokers.items()):
            cleaned = _clean_series(pts)
            if not cleaned:
                continue
            series_raw.append({"broker": brk, "points": cleaned})
            if as_growth:
                grown = _to_yoy_growth(cleaned)
                if grown:
                    series.append({"broker": brk, "points": grown})
            else:
                series.append({"broker": brk, "points": cleaned})
        if series:
            prepared[met] = series
        if series_raw:
            prepared_raw[met] = series_raw

    seg_panels: dict[str, list[dict]] = {s: [] for s in SEGMENT_ORDER}
    consumed: set[str] = set()
    for panel in MERGED_PANELS:
        src = prepared_raw if panel.get("raw") else prepared
        normalize_mgb = panel.get("raw") and panel.get("unit") == "M GB"
        series = []
        for met, short in panel["metrics"].items():
            for s in src.get(met, []):
                if normalize_mgb:
                    converted = []
                    for p in s["points"]:
                        v = _to_mgb(p["value"], p.get("unit"))
                        if v is None:
                            continue
                        converted.append({**p, "value": round(v, 2)})
                    if converted:
                        series.append({"broker": f'{s["broker"]} - {short}', "points": converted})
                else:
                    series.append({"broker": f'{s["broker"]} - {short}', "points": s["points"]})
        if sum(len(s["points"]) for s in series) < 2:
            continue
        if not panel.get("raw"):
            consumed.update(panel["metrics"].keys())
        seg_panels[panel["segment"]].append({
            "metric": panel["key"],
            "label": panel["label"],
            "unit": panel["unit"],
            "series": series,
            "split_by_broker": _has_multiple_variants_per_broker(series),
        })

    for met, series in sorted(prepared.items()):
        if met in consumed:
            continue
        if sum(len(s["points"]) for s in series) < 2:
            continue
        as_growth = met in ABS_BIT_METRICS
        label = METRIC_LABELS.get(met, met)
        unit = units.get(met, CANONICAL_METRICS.get(met, ("", ""))[0])
        if as_growth:
            label += " Growth"
            unit = "% YoY (derived)"
        seg = metric_segment.get(met, CANONICAL_METRICS.get(met, ("", "General"))[1])
        seg_panels.setdefault(seg, []).append({
            "metric": met,
            "label": label,
            "unit": unit,
            "series": series,
        })

    segments = [
        {"segment": seg, "metrics": seg_panels[seg]}
        for seg in SEGMENT_ORDER if seg_panels.get(seg)
    ]
    return {
        "version": version,
        "frequency": frequency,
        "segments": segments,
        "suppressed_points": suppressed_count + int(qrow["n"] if qrow else 0),
        "needs_review_points": needs_review_count,
        "peer_gate_ratio": PEER_OUTLIER_RATIO,
    }
