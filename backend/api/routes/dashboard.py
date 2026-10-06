"""Time-series dashboard API - serves unit-normalized datapoints grouped for charting."""
from __future__ import annotations

import logging
import math
import re
from collections import Counter
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ...db.connection import get_db
from ...services import normalize_broker
from ...services.datapoints import CANONICAL_METRICS, _period_int, _value_in_quote
from ...services.dashboard_datapoints import (
    ensure_dashboard_datapoint_tables,
    get_active_excel_broker_dates,
    get_active_excel_brokers,
    prepare_dashboard_terminal,
    rebuild_verified_datapoints,
    store_raw_dashboard_datapoints,
)
from ...services.dashboard_cache import (
    FREQUENCIES as CACHE_FREQUENCIES,
    ensure_dashboard_cache_table,
    get_cache_status,
    get_cached_timeseries,
    mark_cache_dirty,
    mark_cache_dirty_many,
    rebuild_timeseries_cache,
    start_cache_rebuild,
    start_cache_rebuild_many,
)
from ...services.dashboard_refresh import (
    _call_dashboard_claude_json,
    kick_off_refresh, get_job, list_jobs, KNOWN_BROKERS,
    get_document_refresh_audit,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

_SEGMENT_ORDER = ["DRAM", "DDR", "NAND", "HBM", "General"]
_PEER_OUTLIER_RATIO = 2.0

# Human labels for canonical metric keys
_METRIC_LABELS = {
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
    "dram_share_samsung":  "Samsung",
    "dram_share_skhynix":  "SK Hynix",
    "dram_share_micron":   "Micron",
    "dram_share_others":   "Others",
    "nand_share_samsung":  "Samsung",
    "nand_share_skhynix":  "SK Hynix / Solidigm",
    "nand_share_kioxia":   "Kioxia",
    "nand_share_wdc":      "WDC / SanDisk",
    "nand_share_micron":   "Micron",
    "nand_share_ymtc":     "YMTC",
    "nand_share_others":   "Others",
    "hbm_share_skhynix":   "SK Hynix",
    "hbm_share_samsung":   "Samsung",
    "hbm_share_micron":    "Micron",
}


class ManualInputRequest(BaseModel):
    broker: str = Field(..., min_length=1, max_length=80)
    text: str = Field(..., min_length=5, max_length=12000)
    frequency: str = Field("yearly", max_length=20)


def _normalize_frequency(value: str | None) -> str:
    return "quarterly" if str(value or "").lower() == "quarterly" else "yearly"


def _manual_source_date(text: str) -> str:
    s = text or ""
    month_names = (
        "jan|january|feb|february|mar|march|apr|april|may|jun|june|"
        "jul|july|aug|august|sep|sept|september|oct|october|nov|november|dec|december"
    )
    months = {
        "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
        "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
        "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
        "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
    }
    m = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({month_names})\s+(20\d{{2}}|19\d{{2}})\b", s, re.I)
    if m:
        return f"{int(m.group(3)):04d}-{months[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
    m = re.search(rf"\b({month_names})\s+(\d{{1,2}})(?:st|nd|rd|th)?[,]?\s+(20\d{{2}}|19\d{{2}})\b", s, re.I)
    if m:
        return f"{int(m.group(3)):04d}-{months[m.group(1).lower()]:02d}-{int(m.group(2)):02d}"
    m = re.search(r"\b(20\d{2}|19\d{2})[-/](\d{1,2})[-/](\d{1,2})\b", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return ""


def _frequency_rule(frequency: str) -> str:
    if frequency == "quarterly":
        return (
            "- Time grain: QUARTERLY ONLY. Extract stated quarterly datapoints "
            "and normalize periods to YYYYQn, e.g. 2Q26 -> 2026Q2. "
            "Skip annual and monthly datapoints."
        )
    return (
        "- Time grain: YEARLY ONLY. Extract stated annual / CY / FY datapoints "
        "and normalize periods to YYYY, e.g. CY26/FY26/2026E -> 2026. "
        "Skip quarterly and monthly datapoints."
    )


def _manual_input_prompt(broker: str, text: str, frequency: str) -> str:
    metrics = [
        {"metric": key, "unit": unit, "segment": segment, "label": _METRIC_LABELS.get(key, key)}
        for key, (unit, segment) in CANONICAL_METRICS.items()
    ]
    metric_json = "\n".join(
        f"- {m['metric']} | {m['label']} | canonical unit: {m['unit']} | segment: {m['segment']}"
        for m in metrics
    )
    return f"""You convert a user's pasted memory-market datapoint text into dashboard rows.

Use ONLY the text provided. Classify each stated value into one of the allowed canonical metrics.

BROKER: {broker}

ALLOWED METRICS:
{metric_json}

USER TEXT:
{text[:12000]}

RULES:
- Output one row per stated datapoint.
{_frequency_rule(frequency)}
- Interpret terse broker-call notes like an analyst. A period may come from
  nearby context in USER TEXT, such as a year heading, CY/FY label, "by YYYY",
  or a sentence applying to the surrounding bullet group. Do not use the call
  date / note date as the datapoint period unless the value itself is stated
  as of that date.
- metric must be exactly one allowed metric key.
- period examples: 2026, 2027, 2026Q2, 2026-06.
- value is number only. Do not rescale values.
- unit is exactly the printed unit near the value, e.g. %, $bn, $/Gb, mn GB, EB, K WSPM.
- subject_entity is "Industry Total" unless the value is about a producer
  such as CXMT, YMTC, JHICC, Samsung, SK Hynix, or Micron.
- quote must be a short exact fragment from USER TEXT containing the value.
- The quote does not need to contain the inferred period, but the period must
  be supported by nearby text.
- For cleanroom/fab/wafer-start notes, map WSPM/WPM/K WSPM/k wspm capacity
  to wafer-capacity metrics:
  * DRAM, CXMT, ChangXin, JHICC -> dram_wafer_capacity.
  * NAND, YMTC, Yangtze Memory -> nand_wafer_capacity.
  * HBM -> hbm_wafer_capacity.
- If a line says capacity "add", "adds", or "addition", output the stated
  incremental capacity as the same wafer-capacity metric and keep the word
  "add" in the quote when possible.
- If a line states both count and per-cleanroom WSPM plus an explicit total,
  use the explicit total. Do not do arithmetic unless the text itself states
  enough information unambiguously.
- Skip only when metric, period, value, unit, or supporting quote cannot be
  interpreted from USER TEXT.
- Do not invent data.

STRICT JSON only:
{{"datapoints":[{{"metric":"...","subject_entity":"Industry Total","period":"...","value":0.0,"unit":"...","quote":"..."}}]}}"""


@router.post("/input-ai")
async def input_data_ai(body: ManualInputRequest):
    broker = normalize_broker(body.broker)
    if not broker:
        raise HTTPException(400, "broker is required")
    frequency = _normalize_frequency(body.frequency)
    prompt = _manual_input_prompt(broker, body.text, frequency)
    data = await _call_dashboard_claude_json(prompt)
    if not data:
        raise HTTPException(503, "AI extraction unavailable; check Anthropic API key")

    today = datetime.now().date().isoformat()
    batch_id = f"user_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    source_date = _manual_source_date(body.text)
    source_filename = f"manual_input_{today}.txt"
    rows: list[dict] = []
    rejected_rows: list[dict] = []
    rejected = 0
    for raw in data.get("datapoints") or []:
        metric = str(raw.get("metric") or "").strip()
        period = str(raw.get("period") or "").strip()
        unit = str(raw.get("unit") or "").strip()
        quote = str(raw.get("quote") or "").strip()[:300]
        if metric not in CANONICAL_METRICS or not period or not unit or not quote:
            rejected += 1
            rejected_rows.append({"row": raw, "reason": "missing/unknown metric, period, unit, or quote"})
            continue
        try:
            value = float(raw.get("value"))
        except (TypeError, ValueError):
            rejected += 1
            rejected_rows.append({"row": raw, "reason": "value is not numeric"})
            continue
        if not _value_in_quote(value, quote):
            rejected += 1
            rejected_rows.append({"row": raw, "reason": "value not found in quote"})
            continue
        period_int = _period_int(period)
        if period_int == 0:
            rejected += 1
            rejected_rows.append({"row": raw, "reason": "period does not parse"})
            continue
        if _period_frequency(period) != frequency:
            rejected += 1
            rejected_rows.append({"row": raw, "reason": f"period is not {frequency}"})
            continue
        canon_unit, segment = CANONICAL_METRICS[metric]
        rows.append({
            "segment": segment,
            "metric": metric,
            "period": period,
            "period_int": period_int,
            "raw_value": value,
            "value": value,
            "raw_unit": unit,
            "unit": unit,
            "broker": broker,
            "source_broker": broker,
            "source_type": "user_input",
            "subject_entity": str(raw.get("subject_entity") or "").strip(),
            "source_filename": source_filename,
            "source_date": source_date,
            "source_page": 0,
            "source_quote": quote,
            "doc_type": "user_input",
            "source_doc_type": "user_input",
            "batch_id": batch_id,
        })

    inserted = 0
    validation = {"verified": 0, "quarantined": 0, "needs_review": 0}
    if rows:
        inserted = await store_raw_dashboard_datapoints(
            rows,
            extractor="user:dashboard_ai",
        )
        validation = await rebuild_verified_datapoints({r["metric"] for r in rows})
        await mark_cache_dirty(frequency, "dashboard AI input")
        cache_job = await start_cache_rebuild(frequency)
    else:
        cache_job = {}

    return {
        "ok": True,
        "broker": broker,
        "extracted": len(rows),
        "inserted": inserted,
        "rejected": rejected,
        "rejected_rows": rejected_rows[:20],
        "validation": validation,
        "cache_job": cache_job,
        "rows": rows,
    }


def _granularity(period: str) -> str:
    p = (period or "").upper()
    if "Q" in p:
        return "Q"
    if "-" in p:
        return "M"
    return "Y"


def _period_frequency(period: str) -> str:
    granularity = _granularity(period)
    if granularity == "Q":
        return "quarterly"
    if granularity == "M":
        return "monthly"
    return "yearly"


def _period_frequency_sql(frequency: str) -> str:
    if frequency == "quarterly":
        return "instr(upper(period), 'Q') > 0"
    return "instr(upper(period), 'Q') = 0 AND instr(period, '-') = 0"


def _clean_series(pts: list[dict]) -> list[dict]:
    """Drop noise from a broker series.

    Brokers report the same metric in different bases across pages (e.g. annual
    bn Gb next to quarterly mn-unit tables), which plots as wild magnitude jumps.
    1) Keep only the dominant period granularity (annual vs quarterly vs monthly).
    2) For duplicate periods, keep the value closest to the series' median magnitude.
    3) Keep only the longest base-consistent run (a >6x jump between consecutive
       points marks a mid-series unit-base switch).
    """
    if not pts:
        return pts
    counts = Counter(_granularity(p["period"]) for p in pts)
    dom = counts.most_common(1)[0][0]
    pts = [p for p in pts if _granularity(p["period"]) == dom]

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
    cleaned = sorted(by_period.values(), key=lambda p: p["period_int"])
    return _longest_consistent_run(cleaned)


def _peer_gate_enabled(metric: str) -> bool:
    """Only peer-gate level metrics.

    Percentage/growth/share metrics can legitimately differ by more than 2x
    when the base is small, so those stay visible and are left to the upstream
    citation/unit arbiter.
    """
    m = metric or ""
    blocked = ("growth", "sufficiency", "utilization", "share")
    return not any(tok in m for tok in blocked)


def _peer_status(rows) -> tuple[dict[int, tuple[str, str]], int]:
    """Classify dashboard datapoints before charting.

    >=3 same metric/period peers: suppress the value that is >2x away from the
    peer median. Exactly 2 peers: keep both, but mark both as needs_review so
    the tooltip makes the disagreement visible without guessing which one is
    wrong.
    """
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
            (r["metric"], r["subject_entity"] or "Industry Total", int(r["period_int"])),
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
            if lo > 0 and hi / lo >= _PEER_OUTLIER_RATIO:
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
            if v / med >= _PEER_OUTLIER_RATIO or med / v >= _PEER_OUTLIER_RATIO:
                suppressed += 1
                status[int(r["id"])] = (
                    "suppressed",
                    f"Suppressed from chart: {metric} {r['period']} is >{_PEER_OUTLIER_RATIO:.0f}x away from peer median {med:.3g}.",
                )
    return status, suppressed


def _longest_consistent_run(pts: list[dict], max_jump: float = 6.0) -> list[dict]:
    """Split a sorted series wherever consecutive values jump >max_jump x
    (unit-base switch) and keep the longest segment (ties -> most recent)."""
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


# Absolute bit levels are NOT comparable across brokers (each plots a different
# Gb-equivalence base), so the dashboard shows derived YoY growth for these.
_ABS_BIT_METRICS = {
    "dram_bit_demand", "dram_bit_supply",
    "nand_bit_demand", "nand_bit_supply", "hbm_bit_demand",
}

# Multiplier to bring a stored bit-metric value into "M GB" (million 1GB units).
# bn Gb : 1e9 Gb × (1 GB / 8 Gb) = 0.125e9 GB = 125 M GB
# EB    : 1e18 B = 1e9 GB = 1000 M GB
# mn GB : already in M GB
# GB    : 1 GB = 1e-6 M GB
# Gb    : same conversion as bn Gb but unscaled (1 Gb / 8 = 0.125 GB = 1.25e-7 M GB)
_RAW_UNIT_TO_MGB = {
    "bngb": 125.0,
    "bngbit": 125.0,
    "gb": 1e-6,
    "gbmn": 1.0,
    "mngb": 1.0,
    "mgb": 1.0,
    "mmgb": 1.0,
    "eb": 1000.0,
    "pb": 1.0,            # 1 PB = 1e6 GB = 1 M GB
    "tb": 1e-3,           # 1 TB = 1e3 GB = 1e-3 M GB
    "gbbn": 1000.0,
    "bngb1gbeq": 125.0,
}


def _unit_key(unit: str | None) -> str:
    return (
        (unit or "")
        .strip()
        .lower()
        .replace(" ", "")
        .replace(".", "")
        .replace("-", "")
        .replace("_", "")
        .replace(",", "")
        .replace("(", "")
        .replace(")", "")
        .replace("'", "")
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
    """Convert a stored bit-metric value to 'M GB'. Returns None if the unit
    isn't recognized (caller should drop the point rather than mis-plot)."""
    if value is None:
        return None
    if _is_million_gbit_unit(unit):
        return value / 8.0
    mult = _RAW_UNIT_TO_MGB.get(_unit_key(unit))
    if mult is None:
        return None
    return value * mult


def _quarter_num(period: str) -> int | None:
    m = re.search(r"Q([1-4])", (period or "").upper())
    if m:
        return int(m.group(1))
    return None


def _is_excelish_row(row: dict) -> bool:
    filename = str(row.get("source_filename") or "").lower()
    extractor = str(row.get("extractor") or "").lower()
    return filename.endswith((".xlsx", ".xlsm")) or extractor.startswith("excel:")


def _derive_yearly_from_quarters(rows: list[dict]) -> list[dict]:
    """Derive annual absolute-bit levels from four quarterly Excel rows.

    Some broker workbooks expose both quarterly cells and a nearby annual
    bridge row. When the bridge row is a run-rate or average-style value, the
    yearly absolute chart becomes visibly wrong. This view-layer derivation is
    conservative: it only replaces annual points when all four quarters exist
    for the same broker/metric/year from Excel-like provenance.
    """
    grouped: dict[tuple[str, str, str, int], dict[int, dict]] = {}
    for row in rows:
        if row.get("metric") not in _ABS_BIT_METRICS:
            continue
        if _period_frequency(str(row.get("period") or "")) != "quarterly":
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
        total = sum(float(p.get("value") or 0.0) for p in pts)
        first = dict(pts[0])
        first.update({
            "id": next_id,
            "metric": metric,
            "broker": broker,
            "subject_entity": subject_entity,
            "period": str(year),
            "period_int": year * 10000,
            "value": total,
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
    """Annual YoY % growth derived from a broker's own (base-consistent) levels."""
    by_year = {p["period_int"] // 10000: p
               for p in pts if _granularity(p["period"]) == "Y"}
    out = []
    for y, p in sorted(by_year.items()):
        prev = by_year.get(y - 1)
        if prev and prev["value"]:
            g = (p["value"] / prev["value"] - 1.0) * 100.0
            if -95.0 < g < 500.0:
                cur_src = p.get("source") or {}
                prev_src = prev.get("source") or {}
                out.append({"period": p["period"], "period_int": p["period_int"],
                            "value": round(g, 1),
                            "source": {
                                "filename": cur_src.get("filename") or prev_src.get("filename") or "",
                                "page": cur_src.get("page") or prev_src.get("page") or 0,
                                "quote": cur_src.get("quote") or prev_src.get("quote") or "",
                                "date": cur_src.get("date") or prev_src.get("date") or "",
                            }})
    return out


# Merged panels: related metrics with the SAME canonical unit combined into one
# chart. Series are labeled "<broker> - <short>". Consumed metrics don't render
# as standalone charts.
_MERGED_PANELS = [
    {"key": "ddr4_price", "segment": "DDR", "label": "DDR4 Price - Contract vs Spot",
     "unit": "$/unit", "metrics": {"ddr4_contract_price": "Contract", "ddr4_spot_price": "Spot"}},
    {"key": "ddr5_price", "segment": "DDR", "label": "DDR5 Price - Contract vs Spot",
     "unit": "$/unit", "metrics": {"ddr5_contract_price": "Contract", "ddr5_spot_price": "Spot"}},
    # Absolute supply vs demand levels per broker. Cross-broker magnitudes
    # differ (different Gb-equivalence bases) - _clean_series keeps each
    # broker on its own consistent base, so the within-broker D-vs-S gap is
    # meaningful even when brokers don't agree on absolute level.
    # Absolute supply vs demand levels normalized to "M GB" (million 1GB-units)
    # so every broker plots on the same y-axis. Conversions are applied in the
    # builder via _RAW_UNIT_TO_MGB.
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
    # Market share - one panel per segment, one series per producer. Series
    # label becomes "<broker> - <producer>". y-axis fixed unit %.
    {"key": "dram_market_share", "segment": "DRAM",
     "label": "DRAM Market Share by Producer",
     "unit": "%", "metrics": {
         "dram_share_samsung": "Samsung",
         "dram_share_skhynix": "SK Hynix",
         "dram_share_micron":  "Micron",
         "dram_share_others":  "Others",
     }},
    {"key": "nand_market_share", "segment": "NAND",
     "label": "NAND Market Share by Producer",
     "unit": "%", "metrics": {
         "nand_share_samsung": "Samsung",
         "nand_share_skhynix": "SK Hynix / Solidigm",
         "nand_share_kioxia":  "Kioxia",
         "nand_share_wdc":     "WDC / SanDisk",
         "nand_share_micron":  "Micron",
         "nand_share_ymtc":    "YMTC",
         "nand_share_others":  "Others",
     }},
    {"key": "hbm_market_share", "segment": "HBM",
     "label": "HBM Market Share by Producer",
     "unit": "%", "metrics": {
         "hbm_share_skhynix": "SK Hynix",
         "hbm_share_samsung": "Samsung",
         "hbm_share_micron":  "Micron",
     }},
]


def _series_broker_key(label: str) -> str:
    return re.split(r"\s+-\s+", label or "", maxsplit=1)[0].strip().lower()


def _has_multiple_variants_per_broker(series: list[dict]) -> bool:
    counts = Counter(_series_broker_key(s.get("broker", "")) for s in series)
    return any(key and n > 1 for key, n in counts.items())


@router.get("/timeseries")
async def get_timeseries(
    frequency: str = Query("yearly", pattern="^(yearly|quarterly)$"),
):
    """All datapoints grouped: segment -> chart panels -> broker series."""
    frequency = _normalize_frequency(frequency)
    payload, cache_meta = await get_cached_timeseries(frequency)
    if payload is not None:
        if cache_meta.get("status") != "ready":
            await start_cache_rebuild(frequency)
            payload["cache"] = {**cache_meta, "job_id": cache_meta.get("job_id") or ""}
        return payload

    await mark_cache_dirty(frequency, "missing dashboard cache")
    cache_job = await start_cache_rebuild(frequency)
    return {
        "version": f"missing-{frequency}",
        "frequency": frequency,
        "segments": [],
        "suppressed_points": 0,
        "needs_review_points": 0,
        "peer_gate_ratio": _PEER_OUTLIER_RATIO,
        "cache": {
            **cache_meta,
            "status": "missing",
            "dirty_reason": "missing dashboard cache",
            "job_id": cache_job.get("job_id", ""),
        },
    }

    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT id, segment, metric, period, period_int, value, unit, broker,
                  raw_unit, source_filename, source_page, source_quote, source_date,
                  verification_status, verification_note, extractor,
                  source_type, source_broker, subject_entity, subject_type,
                  frequency, winner_reason, canonical_value, canonical_unit
           FROM dashboard_datapoints_verified
           ORDER BY segment, metric, broker, period_int"""
    )
    all_rows = [dict(r) for r in rows]
    if frequency == "yearly":
        derived_rows = _derive_yearly_from_quarters(all_rows)
        derived_keys = {
            (r["metric"], r["broker"], r.get("subject_entity") or "Industry Total", int(r["period_int"]))
            for r in derived_rows
        }
        rows = [
            r
            for r in all_rows
            if _period_frequency(r["period"]) == frequency
            and (r["metric"], r["broker"], r.get("subject_entity") or "Industry Total", int(r["period_int"])) not in derived_keys
        ] + derived_rows
    else:
        rows = [r for r in all_rows if _period_frequency(r["period"]) == frequency]
    cur = await db.execute("SELECT COALESCE(MAX(id), 0) as m FROM dashboard_datapoints_raw")
    max_id_row = await cur.fetchone()
    qcur = await db.execute(
        f"SELECT COUNT(*) as n FROM dashboard_datapoints_quarantine WHERE {_period_frequency_sql(frequency)}"
    )
    qrow = await qcur.fetchone()
    # "-m3" = response-shape revision: bump when panel structure changes so
    # version-cached frontends refetch even if the data rows are unchanged
    version = f'{max_id_row["m"] if max_id_row else 0}-m15-{frequency}'
    peer_status, suppressed_count = _peer_status(rows)
    needs_review_count = sum(
        1
        for r in rows
        if peer_status.get(
            int(r["id"]),
            (r["verification_status"] or "verified", r["verification_note"] or ""),
        )[0] == "needs_review"
    )

    # metric -> broker -> [{period, period_int, value}]; metric -> home segment
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

    # Cleaned per-metric broker series. `prepared` applies the growth transform
    # for absolute-bit metrics (used by standalone growth charts); `prepared_raw`
    # keeps absolute levels (used by the new Supply-vs-Demand absolute panels).
    prepared: dict[str, list[dict]] = {}
    prepared_raw: dict[str, list[dict]] = {}
    for met, brokers in by_metric.items():
        as_growth = met in _ABS_BIT_METRICS
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

    seg_panels: dict[str, list[dict]] = {s: [] for s in _SEGMENT_ORDER}
    consumed: set[str] = set()

    # 1. Merged panels
    for panel in _MERGED_PANELS:
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
                    if not converted:
                        continue
                    series.append({"broker": f'{s["broker"]} - {short}', "points": converted})
                else:
                    series.append({"broker": f'{s["broker"]} - {short}', "points": s["points"]})
        if sum(len(s["points"]) for s in series) < 2:
            continue
        # `raw` panels coexist with the standalone growth charts for the same
        # bit metrics - don't mark those consumed, so growth still renders.
        if not panel.get("raw"):
            consumed.update(panel["metrics"].keys())
        seg_panels[panel["segment"]].append({
            "metric": panel["key"],
            "label": panel["label"],
            "unit": panel["unit"],
            "series": series,
            "split_by_broker": _has_multiple_variants_per_broker(series),
        })

    # 2. Remaining single-metric panels
    for met, series in sorted(prepared.items()):
        if met in consumed:
            continue
        if sum(len(s["points"]) for s in series) < 2:
            continue
        as_growth = met in _ABS_BIT_METRICS
        label = _METRIC_LABELS.get(met, met)
        unit = units.get(met, CANONICAL_METRICS.get(met, ("", ""))[0])
        if as_growth:
            label += " Growth"
            unit = "% YoY (derived)"
        seg = metric_segment.get(met, CANONICAL_METRICS.get(met, ("", "General"))[1])
        seg_panels.setdefault(seg, []).append({
            "metric": met, "label": label, "unit": unit, "series": series,
        })

    segments = [
        {"segment": seg, "metrics": seg_panels[seg]}
        for seg in _SEGMENT_ORDER if seg_panels.get(seg)
    ]
    return {
        "version": version,
        "frequency": frequency,
        "segments": segments,
        "suppressed_points": suppressed_count + int(qrow["n"] if qrow else 0),
        "needs_review_points": needs_review_count,
        "peer_gate_ratio": _PEER_OUTLIER_RATIO,
    }


@router.get("/quarantine")
async def get_quarantine(
    limit: int = 100,
    frequency: str = Query("yearly", pattern="^(yearly|quarterly)$"),
):
    """Rows blocked from the Bloomberg-style dashboard, with reasons."""
    frequency = _normalize_frequency(frequency)
    await ensure_dashboard_datapoint_tables()
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT metric, period, period_int, raw_value, raw_unit, broker,
                  source_filename, source_date, source_page, source_quote,
                  source_sheet, source_cell, extractor, reason,
                  source_type, source_broker, subject_entity, subject_type,
                  frequency, batch_id, canonical_value, canonical_unit
           FROM dashboard_datapoints_quarantine
           WHERE """ + _period_frequency_sql(frequency) + """
           ORDER BY metric, period_int, broker
           LIMIT ?""",
        (max(1, min(int(limit or 100), 500)),),
    )
    return {"rows": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# Per-(metric × broker) RAG-driven datapoint refresh
# ---------------------------------------------------------------------------

@router.get("/health-audit")
async def dashboard_health_audit(
    frequency: str = Query("yearly", pattern="^(yearly|quarterly)$"),
):
    """Compact dashboard governance/audit summary."""
    frequency = _normalize_frequency(frequency)
    await ensure_dashboard_cache_table()
    db = await get_db()
    period_sql = _period_frequency_sql(frequency)
    try:
        active_models = await db.execute_fetchall(
            """SELECT broker, template, source_filename, uploaded_at, ingested_at,
                      rows_added, verified_rows, quarantined_rows
               FROM excel_ingests
               WHERE active = 1 AND status = 'ingested'
               ORDER BY broker, template"""
        )
    except Exception:
        active_models = []
    source_mix = await db.execute_fetchall(
        f"""SELECT metric, source_type, COUNT(*) AS rows
            FROM dashboard_datapoints_verified
            WHERE {period_sql}
            GROUP BY metric, source_type
            ORDER BY metric, rows DESC"""
    )
    winner_reasons = await db.execute_fetchall(
        f"""SELECT winner_reason, COUNT(*) AS rows
            FROM dashboard_datapoints_verified
            WHERE {period_sql}
            GROUP BY winner_reason
            ORDER BY rows DESC"""
    )
    latest_sources = await db.execute_fetchall(
        f"""SELECT metric, source_broker, subject_entity,
                   MAX(source_date) AS latest_source_date,
                   COUNT(*) AS rows
            FROM dashboard_datapoints_verified
            WHERE {period_sql}
            GROUP BY metric, source_broker, subject_entity
            ORDER BY metric, source_broker, subject_entity
            LIMIT 300"""
    )
    quarantine_reasons = await db.execute_fetchall(
        f"""SELECT source_type, reason, COUNT(*) AS rows
            FROM dashboard_datapoints_quarantine
            WHERE {period_sql}
            GROUP BY source_type, reason
            ORDER BY rows DESC
            LIMIT 80"""
    )
    doc_audit = await get_document_refresh_audit(limit=200)
    latest_pdf_by_broker: dict[str, dict] = {}
    stale_or_missing_counts: dict[str, int] = {}
    for row in doc_audit:
        broker_key = str(row.get("broker") or "").strip()
        status = str(row.get("status") or "")
        filename = str(row.get("filename") or "")
        source_date = str(row.get("source_date") or "")
        if broker_key and filename and source_date and status in {"extracted", "no_datapoints", "no_chunks"}:
            current = latest_pdf_by_broker.get(broker_key)
            if current is None or source_date > str(current.get("source_date") or ""):
                latest_pdf_by_broker[broker_key] = row
        if broker_key and (
            status.startswith("skipped")
            or status in {"no_eligible_pdf", "no_chunks", "no_datapoints"}
        ):
            stale_or_missing_counts[broker_key] = stale_or_missing_counts.get(broker_key, 0) + 1
    return {
        "frequency": frequency,
        "active_excel_brokers": await get_active_excel_brokers(),
        "active_excel_dates": await get_active_excel_broker_dates(),
        "active_excel_models": [dict(r) for r in active_models],
        "source_mix_by_metric": [dict(r) for r in source_mix],
        "winner_reasons": [dict(r) for r in winner_reasons],
        "latest_source_by_metric": [dict(r) for r in latest_sources],
        "quarantine_reasons": [dict(r) for r in quarantine_reasons],
        "document_refresh_audit": doc_audit,
        "latest_pdf_by_broker": list(latest_pdf_by_broker.values()),
        "stale_or_missing_document_counts": [
            {"broker": broker, "rows": rows}
            for broker, rows in sorted(stale_or_missing_counts.items())
        ],
        "cache": await get_cache_status(),
    }


@router.get("/winner")
async def inspect_winner(
    metric: str | None = None,
    period: str | None = None,
    broker: str | None = None,
    subject_entity: str | None = None,
    frequency: str = Query("yearly", pattern="^(yearly|quarterly)$"),
):
    """Inspect gold rows matching a metric/period/broker/subject filter."""
    frequency = _normalize_frequency(frequency)
    await ensure_dashboard_datapoint_tables()
    db = await get_db()
    clauses = [_period_frequency_sql(frequency)]
    params: list = []
    if metric:
        clauses.append("metric = ?")
        params.append(metric)
    if period:
        clauses.append("period = ?")
        params.append(period)
    if broker:
        clauses.append("lower(source_broker) = lower(?)")
        params.append(normalize_broker(broker))
    if subject_entity:
        clauses.append("lower(subject_entity) = lower(?)")
        params.append(subject_entity.strip())
    rows = await db.execute_fetchall(
        f"""SELECT id, raw_id, metric, period, period_int, value, unit,
                   raw_unit, broker, source_type, source_broker,
                   subject_entity, subject_type, frequency,
                   source_filename, source_date, source_page, source_quote,
                   source_sheet, source_cell, extractor,
                   verification_status, verification_note, winner_reason
            FROM dashboard_datapoints_verified
            WHERE {' AND '.join(clauses)}
            ORDER BY metric, source_broker, subject_entity, period_int
            LIMIT 500""",
        params,
    )
    return {"rows": [dict(r) for r in rows]}


@router.post("/rebuild")
async def rebuild_dashboard_gold(metric: str | None = None):
    """Rebuild the materialized gold dashboard layer from raw candidates."""
    result = await prepare_dashboard_terminal({metric} if metric else None)
    await mark_cache_dirty_many(CACHE_FREQUENCIES, "manual gold rebuild")
    cache_jobs = await start_cache_rebuild_many(CACHE_FREQUENCIES)
    return {"ok": True, "metric": metric, "result": result, "cache_jobs": cache_jobs}


@router.post("/rebuild-cache")
async def rebuild_dashboard_cache(
    frequency: str = Query("all", pattern="^(yearly|quarterly|all)$"),
    wait: bool = False,
):
    """Rebuild cached dashboard JSON without touching raw/gold rows."""
    targets = CACHE_FREQUENCIES if frequency == "all" else (frequency,)
    if wait:
        results = [await rebuild_timeseries_cache(freq) for freq in targets]
    else:
        await mark_cache_dirty_many(targets, "manual cache rebuild")
        results = await start_cache_rebuild_many(targets)
    return {"ok": True, "frequency": frequency, "wait": wait, "jobs": results}


@router.get("/cache-status")
async def dashboard_cache_status():
    return await get_cache_status()


@router.delete("/batch/{batch_id}")
async def clear_dashboard_batch(batch_id: str):
    """Remove one refresh/user-input batch and rebuild gold rows."""
    batch_id = (batch_id or "").strip()
    if len(batch_id) < 6:
        raise HTTPException(400, "batch_id is required")
    await prepare_dashboard_terminal()
    db = await get_db()
    deleted: dict[str, int] = {}
    for table in (
        "dashboard_datapoints_raw",
        "dashboard_datapoints_verified",
        "dashboard_datapoints_quarantine",
    ):
        cur = await db.execute(f"DELETE FROM {table} WHERE batch_id = ?", (batch_id,))
        deleted[table] = cur.rowcount if cur.rowcount > 0 else 0
    await db.commit()
    validation = await rebuild_verified_datapoints()
    await mark_cache_dirty_many(CACHE_FREQUENCIES, "dashboard batch cleared")
    cache_jobs = await start_cache_rebuild_many(CACHE_FREQUENCIES)
    return {
        "ok": True,
        "batch_id": batch_id,
        "deleted": deleted,
        "validation": validation,
        "cache_jobs": cache_jobs,
    }


@router.delete("/source/{source_id}")
async def clear_dashboard_source(source_id: int):
    """Remove one source_id and rebuild gold rows."""
    if source_id <= 0:
        raise HTTPException(400, "source_id must be positive")
    await prepare_dashboard_terminal()
    db = await get_db()
    deleted: dict[str, int] = {}
    for table in (
        "dashboard_datapoints_raw",
        "dashboard_datapoints_verified",
        "dashboard_datapoints_quarantine",
    ):
        cur = await db.execute(f"DELETE FROM {table} WHERE source_id = ?", (source_id,))
        deleted[table] = cur.rowcount if cur.rowcount > 0 else 0
    await db.execute("DELETE FROM source_documents WHERE source_id = ?", (source_id,))
    await db.commit()
    validation = await rebuild_verified_datapoints()
    await mark_cache_dirty_many(CACHE_FREQUENCIES, "dashboard source cleared")
    cache_jobs = await start_cache_rebuild_many(CACHE_FREQUENCIES)
    return {
        "ok": True,
        "source_id": source_id,
        "deleted": deleted,
        "validation": validation,
        "cache_jobs": cache_jobs,
    }


@router.post("/refresh-rag")
async def refresh_rag(
    metric: str | None = None,
    broker: str | None = None,
    frequency: str = Query("yearly", pattern="^(yearly|quarterly)$"),
):
    """Kick off a broker-document-first background job that finds each
    broker's latest eligible PDF, loads text/table chunks, and asks Sonnet to
    extract supported dashboard datapoints with verbatim source quotes.

    Optional `metric` / `broker` query params narrow the job (useful when
    only one chart's broker series is stale). Returns immediately with a
    `job_id`; poll `/api/dashboard/refresh-status/{job_id}` for progress.

    Wall-clock depends on eligible PDF count; Sonnet extraction is capped by
    the dashboard Claude concurrency setting.
    """
    frequency = _normalize_frequency(frequency)
    metrics = [metric] if metric else None
    brokers = [broker] if broker else None
    job_id = await kick_off_refresh(metrics=metrics, brokers=brokers, frequency=frequency)
    job = get_job(job_id) or {}
    return {
        "job_id": job_id,
        "strategy": job.get("strategy", "broker_document_first"),
        "frequency": frequency,
        "known_brokers": KNOWN_BROKERS,
        "active_excel_brokers": job.get("active_excel_brokers", []),
        "rag_brokers": job.get("brokers", brokers or list(KNOWN_BROKERS)),
        "skipped_excel_brokers": job.get("skipped_excel_brokers", []),
        "freshness_days": job.get("freshness_days", 20),
        "max_pdf_fallbacks": job.get("max_pdf_fallbacks", 2),
        "excel_override_policy": job.get(
            "excel_override_policy",
            "active Excel brokers skipped by automatic broker-PDF refresh",
        ),
        "allowed_sources": job.get("allowed_sources", []),
    }


@router.get("/refresh-status/{job_id}")
async def refresh_status(job_id: str):
    job = get_job(job_id)
    if not job:
        return {"error": "unknown job_id", "job_id": job_id}
    return job


@router.get("/refresh-jobs")
async def refresh_jobs():
    """Recent refresh jobs (up to 20 most recent, in-process)."""
    return {"jobs": list_jobs()}
