"""Memory Chart-side numeric terminal for dashboard datapoints.

Equity Swarm remains the document/RAG layer. This module is the app-specific
"Bloomberg screen" layer: raw numeric candidates -> strict metric/unit
validation -> verified rows for charting, with rejected rows quarantined.
"""
from __future__ import annotations

import logging
import calendar
import json
import re
from datetime import date, datetime, timedelta

from ..db.connection import get_db
from . import normalize_broker
from .datapoints import CANONICAL_METRICS, _period_int

logger = logging.getLogger(__name__)

PEER_OUTLIER_RATIO = 2.0
ALLOWED_DASHBOARD_SOURCE_TYPES = {"user_input", "broker_research", "industry_report", "tables", "excel"}
NON_EXCEL_FRESHNESS_DAYS = 20

_MONTHS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}


def _month_end_key(year: int, month: int) -> str:
    day = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-{day:02d}"


def _date_key(value: str | None) -> str:
    """Normalize loose metadata dates to a lexicographically sortable key."""
    s = str(value or "").strip()
    if not s:
        return ""
    m = re.search(r"\b(20\d{2}|19\d{2})[-/._ ](0?[1-9]|1[0-2])[-/._ ]([0-3]?\d)\b", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"\b(20\d{2}|19\d{2})[-/._ ](0?[1-9]|1[0-2])\b", s)
    if m:
        return _month_end_key(int(m.group(1)), int(m.group(2)))
    m = re.search(
        r"\b("
        r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
        r")[\s._-]+(?:'?\s*)?(\d{2}|\d{4})\b",
        s,
        re.IGNORECASE,
    )
    if m:
        month = _MONTHS.get(m.group(1).lower(), 0)
        year = int(m.group(2))
        if year < 100:
            year += 2000 if year < 50 else 1900
        if month:
            return _month_end_key(year, month)
    m = re.search(r"\b(20\d{2}|19\d{2})\b", s)
    if m:
        return f"{int(m.group(1)):04d}-12-31"
    return ""


def _exact_source_date(value: str | None) -> date | None:
    """Exact source/report date for dashboard freshness gating.

    This deliberately ignores month-only/year-only labels. The broker-PDF
    refresh may fall back to a filename YYYYMMDD, but it should store that as a
    full YYYY-MM-DD source_date before gold validation sees it.
    """
    s = str(value or "").strip()
    m = re.search(r"\b(20\d{2}|19\d{2})[-/._](0[1-9]|1[0-2])[-/._]([0-3]\d)\b", s)
    if not m:
        m = re.search(r"\b(20\d{2})(0[1-9]|1[0-2])([0-3]\d)\b", s)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _non_excel_freshness_reject_reason(source_date: str | None) -> str:
    raw = str(source_date or "").strip()
    if not raw:
        return "missing source date cannot become dashboard winner"
    parsed = _exact_source_date(raw)
    if parsed is None:
        return f"non-exact source date {raw!r} cannot become dashboard winner"
    today = datetime.now().date()
    cutoff = today - timedelta(days=NON_EXCEL_FRESHNESS_DAYS)
    if parsed > today:
        return f"future source date {parsed.isoformat()} > {today.isoformat()}"
    if parsed < cutoff:
        return (
            f"stale source date {parsed.isoformat()} older than "
            f"{NON_EXCEL_FRESHNESS_DAYS}-day cutoff {cutoff.isoformat()}"
        )
    return ""


async def _add_column_if_missing(db, table: str, ddl: str) -> None:
    try:
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
    except Exception:
        pass


def _unit_key(unit: str) -> str:
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


def _compact_unit_preserve_case(unit: str) -> str:
    return re.sub(r"[\s._\-/(),]+", "", unit or "")


def _is_million_gbit_unit(unit: str) -> bool:
    """Case-aware detector for million-gigabit units.

    The lowercased unit key cannot distinguish mGb/mn Gb from mGB/mn GB. Keep
    this separate so HBM demand does not get an 8x byte/bit error.
    """
    compact = _compact_unit_preserve_case(unit)
    lower = compact.lower()
    if re.fullmatch(r"(?:m|mn|mm|million)g(?:bit|bits)", lower):
        return True
    return bool(re.fullmatch(r"(?:m|mn|mm|M|MN|MM)[Gg]b", compact))


def _period_frequency(period: str) -> str:
    p = (period or "").upper()
    if "Q" in p:
        return "quarterly"
    if "-" in p:
        return "monthly"
    return "yearly"


def _source_type_from(extractor: str, filename: str = "", explicit: str = "") -> str:
    explicit = (explicit or "").strip().lower()
    if explicit:
        return explicit
    ex = (extractor or "").strip().lower()
    fn = (filename or "").strip().lower()
    if ex.startswith("excel:") or fn.endswith((".xlsx", ".xlsm")):
        return "excel"
    if ex.startswith("user:"):
        return "user_input"
    if ex == "dashboard_rag_refresh":
        return "broker_research"
    return "broker_research"


_METRIC_SUBJECTS = {
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


def _infer_subject_entity(metric: str, quote: str = "", explicit: str = "") -> str:
    explicit = (explicit or "").strip()
    if explicit:
        return explicit
    if metric in _METRIC_SUBJECTS:
        return _METRIC_SUBJECTS[metric]
    q = (quote or "").lower()
    if re.search(r"\bcxmt\b|changxin|changxin memory", q):
        return "CXMT"
    if re.search(r"\bymtc\b|yangtze memory", q):
        return "YMTC"
    if re.search(r"\bjhicc\b", q):
        return "JHICC"
    if "samsung" in q:
        return "Samsung"
    if "sk hynix" in q or "skhynix" in q or "hynix" in q:
        return "SK Hynix"
    if "micron" in q:
        return "Micron"
    return "Industry Total"


def _subject_type(subject_entity: str) -> str:
    return "industry" if (subject_entity or "") == "Industry Total" else "producer"


def _winner_priority(source_type: str, source_date: str) -> tuple[int, str]:
    priority = {
        "user_input": 4,
        "broker_research": 3,
        "tables": 3,
        "industry_report": 2,
        "excel": 1,
    }.get(source_type, 0)
    return (priority, source_date or "")


_METRIC_RANGES: dict[str, tuple[float | None, float | None]] = {
    "hbm_tam": (0.1, 400.0),
    "hbm_bit_demand": (0.1, 60000.0),
    "hbm_content_per_accelerator": (1.0, 2000.0),
    "hbm_wafer_capacity": (1.0, 3000.0),
    "hbm_asp": (0.01, 100.0),
    "dram_bit_demand": (0.1, 200000.0),
    "dram_bit_supply": (0.1, 200000.0),
    "dram_bit_growth": (-95.0, 500.0),
    "dram_sufficiency": (-100.0, 100.0),
    "dram_contract_asp": (0.01, 50.0),
    "dram_spot_asp": (0.01, 500.0),
    "dram_inventory_weeks": (0.0, 104.0),
    "dram_wafer_capacity": (1.0, 5000.0),
    "dram_utilization": (0.0, 120.0),
    "ddr4_contract_price": (0.01, 500.0),
    "ddr5_contract_price": (0.01, 500.0),
    "ddr4_spot_price": (0.01, 500.0),
    "ddr5_spot_price": (0.01, 500.0),
    "nand_bit_demand": (0.01, 10000.0),
    "nand_bit_supply": (0.01, 10000.0),
    "nand_bit_growth": (-95.0, 500.0),
    "nand_sufficiency": (-100.0, 100.0),
    "nand_contract_asp": (0.001, 50.0),
    "nand_spot_asp": (0.001, 500.0),
    "nand_wafer_price": (100.0, 50000.0),
    "nand_inventory_weeks": (0.0, 104.0),
    "nand_wafer_capacity": (1.0, 5000.0),
    "nand_utilization": (0.0, 120.0),
    "dram_capex": (0.0, 1000.0),
    "nand_capex": (0.0, 1000.0),
    "memory_capex": (0.0, 2000.0),
    "memory_revenue": (0.0, 1000.0),
    "cloud_capex": (0.0, 5000.0),
    "server_dram_share": (0.0, 100.0),
    "dram_share_samsung": (0.0, 100.0),
    "dram_share_skhynix": (0.0, 100.0),
    "dram_share_micron": (0.0, 100.0),
    "dram_share_others": (0.0, 100.0),
    "nand_share_samsung": (0.0, 100.0),
    "nand_share_skhynix": (0.0, 100.0),
    "nand_share_kioxia": (0.0, 100.0),
    "nand_share_wdc": (0.0, 100.0),
    "nand_share_micron": (0.0, 100.0),
    "nand_share_ymtc": (0.0, 100.0),
    "nand_share_others": (0.0, 100.0),
    "hbm_share_skhynix": (0.0, 100.0),
    "hbm_share_samsung": (0.0, 100.0),
    "hbm_share_micron": (0.0, 100.0),
}


def _is_percent_unit(u: str) -> bool:
    return u in {
        "%", "pct", "percent", "yoy%", "%yoy", "qoq%", "%qoq",
        "%pa", "%p.a", "%p.a.", "%yoypa", "bps", "bp", "ratio", "x",
    }


def _is_currency_unit(u: str) -> bool:
    return any(tok in u for tok in ("$", "usd", "us$", "usdmn", "us$mn", "us$bn"))


def _is_weeks_unit(u: str) -> bool:
    return u in {
        "week", "weeks", "wk", "wks", "weeksofsupply", "wos",
        "day", "days", "dio", "daysofinventory",
        "month", "months", "mo",
    }


def _reject_bad_kind(metric: str, raw_unit: str) -> str | None:
    canon = CANONICAL_METRICS.get(metric, ("", ""))[0]
    u = _unit_key(raw_unit)
    if not u:
        return "missing raw unit"
    if _is_percent_unit(u) and canon not in {"%", "% YoY"}:
        return f"raw unit {raw_unit!r} is a percentage, not a {canon} level"
    if canon in {"%", "% YoY"}:
        return None if _is_percent_unit(u) else f"raw unit {raw_unit!r} is not a percentage"
    if canon == "weeks":
        return None if _is_weeks_unit(u) else f"raw unit {raw_unit!r} is not an inventory-weeks unit"
    if canon in {"$bn", "$/Gb", "$/GB", "$/unit", "$/wafer"}:
        return None
    if _is_currency_unit(u):
        return f"raw unit {raw_unit!r} is currency, not {canon}"
    return None


def _normalize_strict(
    metric: str,
    raw_value: float,
    raw_unit: str,
    extractor: str = "",
) -> tuple[bool, float | None, str, str]:
    """Metric-specific normalizer.

    Returns (ok, value, canonical_unit, note_or_reason). It intentionally
    rejects broad "maybe" conversions. Bad rows should be quarantined instead
    of squeezed into a chart.
    """
    if metric not in CANONICAL_METRICS:
        return False, None, raw_unit or "", f"unknown metric {metric!r}"

    canon_unit = CANONICAL_METRICS[metric][0]
    u = _unit_key(raw_unit)
    kind_err = _reject_bad_kind(metric, raw_unit)
    if kind_err:
        return False, None, canon_unit, kind_err

    v = float(raw_value)
    note = "accepted as canonical"

    if canon_unit in {"%", "% YoY"}:
        if u in {"%pa", "%p.a", "%p.a.", "%yoypa"}:
            note = "accepted annual percentage"
        elif u in {"bps", "bp"}:
            v = v * 0.01
            note = "converted bps to %"
        elif u in {"ratio", "x"} and abs(v) <= 5:
            v = v * 100
            note = "converted ratio to %"
        elif extractor.startswith("excel:") and u in {"%", "pct", "percent"} and -1.5 < v < 1.5:
            v = v * 100
            note = "converted Excel decimal percentage to %"
        elif _is_percent_unit(u):
            note = "accepted percentage"
        else:
            return False, None, canon_unit, f"unit {raw_unit!r} not allowed for percentage metric"

    elif canon_unit == "$bn":
        if any(tok in u for tok in ("rmb", "cny", "krw", "won", "wtn", "wbn")):
            return False, None, canon_unit, f"non-USD currency {raw_unit!r} requires FX, quarantined"
        if u in {
            "us$mn", "$mn", "usdmn", "us$mm", "$mm", "usdmm", "us$m",
            "$m", "usdm", "mn", "m", "million", "us$million",
            "usdmillion", "$million",
        }:
            if abs(v) > 500:
                v = v / 1000.0
                note = "converted US$mn to $bn"
            else:
                note = "legacy value already appears to be $bn despite mn raw unit"
        elif u in {"us$bn", "$bn", "usdbn", "us$b", "bn", "b", "$b", "billion", "$billions", "us$billions"}:
            note = "accepted $bn"
        elif u in {"us$tn", "$tn", "usdtn", "$t"}:
            v = v * 1000.0
            note = "converted US$tn to $bn"
        else:
            return False, None, canon_unit, f"unit {raw_unit!r} not allowed for $bn metric"

    elif canon_unit == "kwpm":
        if u in {"kwpm", "kwspm", "kwsm", "kwaferspermonth", "kwafers/month", "kw/m", "kwpm/month", "kwpm12inchequiv", "wpm12inchequiv"}:
            note = "accepted kwpm-equivalent"
        elif u in {"wpm", "wspm", "waferspermonth", "wafers/month"}:
            if abs(v) >= 5000:
                v = v / 1000.0
                note = "converted WPM to kwpm"
            elif abs(v) >= 1:
                note = "accepted as k-wafers scale based on value"
            else:
                return False, None, canon_unit, f"{v:g} {raw_unit} is too small for wafer capacity"
        elif u in {"mnwaferspermonth", "mnwpm"}:
            v = v * 1000.0
            note = "converted mn wafers/month to kwpm"
        else:
            return False, None, canon_unit, f"unit {raw_unit!r} not allowed for wafer capacity"

    elif canon_unit == "weeks":
        if u in {"week", "weeks", "wk", "wks", "weeksofsupply", "wos"}:
            note = "accepted weeks"
        elif u in {"day", "days", "dio", "daysofinventory"}:
            v = v / 7.0
            note = "converted days to weeks"
        elif u in {"month", "months", "mo"}:
            v = v * 4.345
            note = "converted months to weeks"
        else:
            return False, None, canon_unit, f"unit {raw_unit!r} not allowed for inventory weeks"

    elif canon_unit == "mn GB":
        if _is_million_gbit_unit(raw_unit):
            v = v / 8.0
            note = "converted mn Gb to mn GB"
        elif u in {"mngb", "mgb", "mmgb", "milliongb", "mn8gb", "mn8gbeq"}:
            note = "accepted mn GB"
        elif u in {"'000gb", "000gb", "kgb", "thousandgb"}:
            v = v / 1000.0
            note = "converted '000 GB to mn GB"
        elif u in {"bngb", "bngbit", "bngbbit", "bgb"}:
            if abs(v) > 1000:
                note = "legacy value already appears converted to mn GB"
            else:
                v = v * 125.0
                note = "converted bn Gb to mn GB"
        elif u in {"gb"}:
            v = v / 1_000_000.0
            note = "converted GB to mn GB"
        elif u in {"tb"}:
            v = v / 1000.0
            note = "converted TB to mn GB"
        elif u in {"eb"}:
            v = v * 1000.0
            note = "converted EB to mn GB"
        elif u in {"mn", "m"}:
            note = "accepted broker mn-unit HBM demand as mn GB"
        else:
            return False, None, canon_unit, f"unit {raw_unit!r} not allowed for HBM GB demand"

    elif canon_unit == "bn Gb":
        if u in {"bngb", "bngbit", "bgb", "bn1gb", "bn1gbeq", "bngbeq"}:
            note = "accepted bn Gb"
        elif _is_million_gbit_unit(raw_unit):
            v = v * 0.001
            note = "converted mn Gb to bn Gb"
        elif u in {"mngb", "mgb", "mmgb", "milliongb"}:
            v = v * 0.008
            note = "converted mn GB to bn Gb"
        elif u in {"mn1gb", "mn1gbeq", "m1gb", "mngbeq"}:
            v = v * 0.001
            note = "converted mn 1Gb-eq to bn Gb"
        elif u in {"mn8gb", "mn8gbeq", "mn8gbequiv", "mn8gbequivalent", "m8gb"}:
            v = v * 0.008
            note = "converted mn 8Gb-eq to bn Gb"
        elif u in {"eb", "exabyte", "exabytes"}:
            v = v * 8.0
            note = "converted EB to bn Gb"
        else:
            return False, None, canon_unit, f"unit {raw_unit!r} not allowed for bit level"

    elif canon_unit == "EB":
        if u in {"eb", "exabyte", "exabytes"}:
            note = "accepted EB"
        elif u in {"pb", "petabyte", "petabytes"}:
            v = v / 1000.0
            note = "converted PB to EB"
        elif u in {"tb", "terabyte", "terabytes"}:
            v = v / 1_000_000.0
            note = "converted TB to EB"
        elif _is_million_gbit_unit(raw_unit):
            v = v * 0.000125
            note = "converted mn Gb to EB"
        elif u in {"mngb", "mgb", "milliongb"}:
            v = v / 1000.0
            note = "converted mn GB to EB"
        elif u in {"bngb", "bngbyte"}:
            v = v
            note = "accepted bn GB as EB"
        else:
            return False, None, canon_unit, f"unit {raw_unit!r} not allowed for NAND bit level"

    elif canon_unit == "$/Gb":
        if u in {"$/gb", "usd/gb", "$pergb", "us$/gb", "us$1gbequiv", "us$1gbeq", "us$1gb"}:
            note = "accepted $/Gb"
        elif u in {"$/gbyte", "$/gigabyte", "$/gbbyte", "$pergbyte", "usd/gbyte", "us/gb", "us$/gbyte", "usd/gbbyte"}:
            v = v / 8.0
            note = "converted $/GB to $/Gb"
        elif u in {"cents/gb", "c/gb"}:
            v = v * 0.01
            note = "converted cents/Gb to $/Gb"
        else:
            return False, None, canon_unit, f"unit {raw_unit!r} not allowed for $/Gb"

    elif canon_unit == "$/GB":
        if u in {"$/gb", "usd/gb", "$pergb", "us$/gb", "$/gbyte", "$pergbyte"}:
            note = "accepted $/GB"
        elif u in {"cents/gb", "c/gb"}:
            v = v * 0.01
            note = "converted cents/GB to $/GB"
        elif u in {"$/tb", "usd/tb"}:
            v = v / 1000.0
            note = "converted $/TB to $/GB"
        elif "256gb" in u:
            v = v / 32.0
            note = "converted 256Gb-equivalent price to $/GB"
        elif "16gb" in u:
            v = v / 2.0
            note = "converted 16Gb-equivalent price to $/GB"
        else:
            return False, None, canon_unit, f"unit {raw_unit!r} not allowed for $/GB"

    elif canon_unit in {"$/unit", "$/wafer"}:
        if _is_percent_unit(u):
            return False, None, canon_unit, f"raw unit {raw_unit!r} is percentage, not price level"
        if not (_is_currency_unit(u) or "/" in (raw_unit or "") or "$" in (raw_unit or "")):
            return False, None, canon_unit, f"unit {raw_unit!r} not allowed for price metric"
        note = "accepted price unit"

    elif canon_unit == "GB":
        if u in {"gb", "gigabyte", "gigabytes"}:
            note = "accepted GB"
        elif u in {"tb"}:
            v = v * 1000.0
            note = "converted TB to GB"
        elif u in {"gbit", "gbits"}:
            v = v / 8.0
            note = "converted Gb to GB"
        else:
            return False, None, canon_unit, f"unit {raw_unit!r} not allowed for GB metric"

    lo, hi = _METRIC_RANGES.get(metric, (None, None))
    if lo is not None and v < lo:
        return False, None, canon_unit, f"normalized value {v:g} {canon_unit} below plausible range {lo:g}"
    if hi is not None and v > hi:
        return False, None, canon_unit, f"normalized value {v:g} {canon_unit} above plausible range {hi:g}"

    return True, v, canon_unit, note


def _peer_gate_enabled(metric: str) -> bool:
    return not any(tok in (metric or "") for tok in ("growth", "sufficiency", "utilization", "share"))


async def ensure_dashboard_datapoint_tables() -> None:
    db = await get_db()
    await db.execute(
        """CREATE TABLE IF NOT EXISTS source_documents (
               source_id INTEGER PRIMARY KEY AUTOINCREMENT,
               source_type TEXT NOT NULL DEFAULT '',
               source_broker TEXT NOT NULL DEFAULT '',
               filename TEXT NOT NULL DEFAULT '',
               source_date TEXT NOT NULL DEFAULT '',
               model_date TEXT NOT NULL DEFAULT '',
               doc_type TEXT NOT NULL DEFAULT '',
               source_doc_type TEXT NOT NULL DEFAULT '',
               media_type TEXT NOT NULL DEFAULT '',
               ingested_at TEXT NOT NULL,
               UNIQUE(source_type, source_broker, filename, source_date, model_date,
                      doc_type, source_doc_type, media_type)
           )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS dashboard_datapoints_raw (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               segment TEXT NOT NULL,
               metric TEXT NOT NULL,
               period TEXT NOT NULL,
               period_int INTEGER NOT NULL,
               raw_value REAL NOT NULL,
               raw_unit TEXT NOT NULL DEFAULT '',
               broker TEXT NOT NULL DEFAULT '',
               source_filename TEXT NOT NULL DEFAULT '',
               source_date TEXT NOT NULL DEFAULT '',
               source_page INTEGER NOT NULL DEFAULT 0,
               source_quote TEXT NOT NULL DEFAULT '',
               source_sheet TEXT NOT NULL DEFAULT '',
               source_cell TEXT NOT NULL DEFAULT '',
               extractor TEXT NOT NULL DEFAULT '',
               created_at TEXT NOT NULL,
               UNIQUE(metric, period, broker, source_filename, source_page, source_quote)
           )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS dashboard_datapoints_verified (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               raw_id INTEGER REFERENCES dashboard_datapoints_raw(id),
               segment TEXT NOT NULL,
               metric TEXT NOT NULL,
               period TEXT NOT NULL,
               period_int INTEGER NOT NULL,
               value REAL NOT NULL,
               unit TEXT NOT NULL,
               raw_unit TEXT NOT NULL DEFAULT '',
               broker TEXT NOT NULL DEFAULT '',
               source_filename TEXT NOT NULL DEFAULT '',
               source_date TEXT NOT NULL DEFAULT '',
               source_page INTEGER NOT NULL DEFAULT 0,
               source_quote TEXT NOT NULL DEFAULT '',
               source_sheet TEXT NOT NULL DEFAULT '',
               source_cell TEXT NOT NULL DEFAULT '',
               extractor TEXT NOT NULL DEFAULT '',
               verification_status TEXT NOT NULL DEFAULT 'verified',
               verification_note TEXT NOT NULL DEFAULT '',
               created_at TEXT NOT NULL,
               UNIQUE(metric, period, broker, source_filename, source_page, source_quote)
           )"""
    )
    await db.execute(
        """CREATE TABLE IF NOT EXISTS dashboard_datapoints_quarantine (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               raw_id INTEGER,
               segment TEXT NOT NULL,
               metric TEXT NOT NULL,
               period TEXT NOT NULL,
               period_int INTEGER NOT NULL,
               raw_value REAL NOT NULL,
               raw_unit TEXT NOT NULL DEFAULT '',
               broker TEXT NOT NULL DEFAULT '',
               source_filename TEXT NOT NULL DEFAULT '',
               source_date TEXT NOT NULL DEFAULT '',
               source_page INTEGER NOT NULL DEFAULT 0,
               source_quote TEXT NOT NULL DEFAULT '',
               source_sheet TEXT NOT NULL DEFAULT '',
               source_cell TEXT NOT NULL DEFAULT '',
               extractor TEXT NOT NULL DEFAULT '',
               reason TEXT NOT NULL,
               created_at TEXT NOT NULL,
               UNIQUE(metric, period, broker, source_filename, source_page, source_quote, reason)
           )"""
    )
    for table in (
        "dashboard_datapoints_raw",
        "dashboard_datapoints_verified",
        "dashboard_datapoints_quarantine",
    ):
        await _add_column_if_missing(db, table, "source_id INTEGER NOT NULL DEFAULT 0")
        await _add_column_if_missing(db, table, "source_type TEXT NOT NULL DEFAULT ''")
        await _add_column_if_missing(db, table, "source_broker TEXT NOT NULL DEFAULT ''")
        await _add_column_if_missing(db, table, "subject_entity TEXT NOT NULL DEFAULT 'Industry Total'")
        await _add_column_if_missing(db, table, "subject_type TEXT NOT NULL DEFAULT 'industry'")
        await _add_column_if_missing(db, table, "frequency TEXT NOT NULL DEFAULT ''")
        await _add_column_if_missing(db, table, "batch_id TEXT NOT NULL DEFAULT ''")
        await _add_column_if_missing(db, table, "canonical_value REAL")
        await _add_column_if_missing(db, table, "canonical_unit TEXT NOT NULL DEFAULT ''")
        await _add_column_if_missing(db, table, "winner_reason TEXT NOT NULL DEFAULT ''")
        await _add_column_if_missing(db, table, "source_sheet TEXT NOT NULL DEFAULT ''")
        await _add_column_if_missing(db, table, "source_cell TEXT NOT NULL DEFAULT ''")
        await _add_column_if_missing(db, table, "extractor TEXT NOT NULL DEFAULT ''")
    await db.commit()


async def get_active_excel_brokers() -> list[str]:
    """Brokers whose latest accepted source is an uploaded Excel model."""
    db = await get_db()
    try:
        from .excel_ingest import ensure_excel_ingests_table
        await ensure_excel_ingests_table()
    except Exception:
        pass
    try:
        rows = await db.execute_fetchall(
            """SELECT DISTINCT broker
               FROM excel_ingests
               WHERE active = 1 AND status = 'ingested' AND broker != ''
               ORDER BY broker"""
        )
    except Exception:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for r in rows:
        broker = normalize_broker(str(r["broker"] or "").strip())
        key = broker.lower()
        if broker and key not in seen:
            out.append(broker)
            seen.add(key)
    return out


async def get_active_excel_broker_dates() -> dict[str, str]:
    """Return active Excel model date cutoffs keyed by normalized broker.

    Prefer workbook/model dates from coverage metadata or filename. Upload time
    is only a fallback, because uploading an old model today should not block a
    newer dated RAG source.
    """
    db = await get_db()
    try:
        from .excel_ingest import ensure_excel_ingests_table
        await ensure_excel_ingests_table()
    except Exception:
        pass
    try:
        rows = await db.execute_fetchall(
            """SELECT broker, source_filename, coverage_json, uploaded_at, ingested_at
               FROM excel_ingests
               WHERE active = 1 AND status = 'ingested' AND broker != ''"""
        )
    except Exception:
        return {}
    out: dict[str, str] = {}
    for r in rows:
        broker = normalize_broker(str(r["broker"] or "").strip())
        if not broker:
            continue
        primary_dates: list[str] = []
        fallback_dates: list[str] = []
        try:
            coverage = json.loads(r["coverage_json"] or "{}")
        except Exception:
            coverage = {}
        if isinstance(coverage, dict):
            for key in ("model_date", "source_date", "as_of_date", "report_date", "date"):
                dk = _date_key(coverage.get(key))
                if dk:
                    primary_dates.append(dk)
        filename_date = _date_key(r["source_filename"] or "")
        if filename_date:
            primary_dates.append(filename_date)
        for key in ("uploaded_at", "ingested_at"):
            dk = _date_key(r[key] if key in r.keys() else "")
            if dk:
                fallback_dates.append(dk)
        cutoff = max(primary_dates or fallback_dates or [""])
        bkey = broker.lower()
        if cutoff and cutoff > out.get(bkey, ""):
            out[bkey] = cutoff
    return out


async def get_active_excel_file_keys() -> set[tuple[str, str]]:
    """Return (broker_key, source_filename) pairs for active Excel ingests."""
    db = await get_db()
    try:
        from .excel_ingest import ensure_excel_ingests_table
        await ensure_excel_ingests_table()
    except Exception:
        pass
    try:
        rows = await db.execute_fetchall(
            """SELECT broker, source_filename
               FROM excel_ingests
               WHERE active = 1 AND status = 'ingested'
                 AND broker != '' AND source_filename != ''"""
        )
    except Exception:
        return set()
    out: set[tuple[str, str]] = set()
    for r in rows:
        broker = normalize_broker(str(r["broker"] or "").strip()).lower()
        filename = str(r["source_filename"] or "").strip()
        if broker and filename:
            out.add((broker, filename))
    return out


async def delete_excel_dashboard_rows(
    broker: str,
    *,
    source_filename: str | None = None,
    exclude_source_filename: str | None = None,
    extractor: str | None = None,
    include_raw: bool = False,
) -> int:
    """Retire Excel-sourced dashboard terminal rows for a broker.

    By default this clears only materialized verified/quarantine rows. Raw rows
    are retained for audit and replay; pass include_raw=True only for an
    explicit source/batch purge.
    """
    await ensure_dashboard_datapoint_tables()
    db = await get_db()
    broker = normalize_broker(broker)
    clauses = [
        "broker = ?",
        "(extractor LIKE 'excel:%' OR lower(source_filename) LIKE '%.xlsx' OR lower(source_filename) LIKE '%.xlsm')",
    ]
    params: list = [broker]
    if source_filename is not None:
        clauses.append("source_filename = ?")
        params.append(source_filename)
    if exclude_source_filename is not None:
        clauses.append("source_filename != ?")
        params.append(exclude_source_filename)
    if extractor is not None:
        clauses.append("extractor = ?")
        params.append(extractor)
    where = " AND ".join(clauses)
    deleted = 0
    tables = [
        "dashboard_datapoints_verified",
        "dashboard_datapoints_quarantine",
    ]
    if include_raw:
        tables.append("dashboard_datapoints_raw")
    for table in tables:
        cur = await db.execute(f"DELETE FROM {table} WHERE {where}", params)
        deleted += cur.rowcount if cur.rowcount > 0 else 0
    await db.commit()
    return deleted


async def count_verified_dashboard_rows(broker: str, source_filename: str) -> int:
    await ensure_dashboard_datapoint_tables()
    db = await get_db()
    cur = await db.execute(
        """SELECT COUNT(*) AS n
           FROM dashboard_datapoints_verified
           WHERE broker = ? AND source_filename = ? AND extractor LIKE 'excel:%'""",
        (broker, source_filename),
    )
    row = await cur.fetchone()
    return int(row["n"] or 0) if row else 0


async def count_quarantined_dashboard_rows(broker: str, source_filename: str) -> int:
    await ensure_dashboard_datapoint_tables()
    db = await get_db()
    cur = await db.execute(
        """SELECT COUNT(*) AS n
           FROM dashboard_datapoints_quarantine
           WHERE broker = ? AND source_filename = ? AND extractor LIKE 'excel:%'""",
        (broker, source_filename),
    )
    row = await cur.fetchone()
    return int(row["n"] or 0) if row else 0


async def _source_document_id(
    db,
    *,
    source_type: str,
    source_broker: str,
    filename: str,
    source_date: str,
    model_date: str = "",
    doc_type: str = "",
    source_doc_type: str = "",
    media_type: str = "",
) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    values = (
        source_type or "",
        normalize_broker(source_broker or ""),
        filename or "",
        source_date or "",
        model_date or "",
        doc_type or "",
        source_doc_type or "",
        media_type or "",
    )
    await db.execute(
        """INSERT OR IGNORE INTO source_documents
           (source_type, source_broker, filename, source_date, model_date,
            doc_type, source_doc_type, media_type, ingested_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (*values, now),
    )
    cur = await db.execute(
        """SELECT source_id
           FROM source_documents
           WHERE source_type=? AND source_broker=? AND filename=?
             AND source_date=? AND model_date=? AND doc_type=?
             AND source_doc_type=? AND media_type=?""",
        values,
    )
    row = await cur.fetchone()
    return int(row["source_id"] or 0) if row else 0


def _legacy_excel_extractor(source_filename: str, fallback: str = "legacy_datapoints") -> str:
    name = (source_filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        return "excel:legacy_import"
    return fallback


async def mark_legacy_excel_rows() -> int:
    """Retag pre-registry Excel imports so active Excel does not hide them.

    Older JPM workbook rows were copied through the legacy datapoints bridge
    before the `excel:<template>` extractor convention existed. They still
    have `.xlsx` provenance, so keep them visible as Excel-sourced rows while
    preserving their original source filename and quote audit fields.
    """
    await ensure_dashboard_datapoint_tables()
    db = await get_db()
    updated = 0
    excel_name_filter = "(lower(source_filename) LIKE '%.xlsx' OR lower(source_filename) LIKE '%.xlsm')"
    for table in (
        "dashboard_datapoints_raw",
        "dashboard_datapoints_verified",
        "dashboard_datapoints_quarantine",
    ):
        cur = await db.execute(
            f"""UPDATE {table}
                SET extractor = 'excel:legacy_import'
                WHERE {excel_name_filter}
                  AND (extractor = '' OR extractor = 'legacy_datapoints')"""
        )
        updated += cur.rowcount if cur.rowcount > 0 else 0
    await db.commit()
    return updated


async def sync_legacy_datapoints_to_raw() -> int:
    """Bootstrap from the legacy datapoints table without touching it."""
    await ensure_dashboard_datapoint_tables()
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            """SELECT segment, metric, period, period_int, value, unit, raw_unit, broker,
                      source_filename, source_date, source_page, source_quote
               FROM datapoints"""
        )
    except Exception:
        return 0
    now = datetime.now().isoformat(timespec="seconds")
    inserted = 0
    for r in rows:
        # `datapoints.value` is already normalized to `datapoints.unit`.
        # Treat it as the raw terminal input under that canonical unit so the
        # stricter dashboard validator does not apply the original unit
        # conversion a second time.
        terminal_unit = r["unit"] or r["raw_unit"] or ""
        cur = await db.execute(
            """INSERT OR IGNORE INTO dashboard_datapoints_raw
               (segment, metric, period, period_int, raw_value, raw_unit, broker,
                source_filename, source_date, source_page, source_quote, extractor, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                r["segment"], r["metric"], r["period"], r["period_int"], r["value"],
                terminal_unit, r["broker"] or "", r["source_filename"] or "",
                r["source_date"] or "", int(r["source_page"] or 0),
                r["source_quote"] or "",
                _legacy_excel_extractor(r["source_filename"] or ""),
                now,
            ),
        )
        inserted += cur.rowcount if cur.rowcount > 0 else 0
        if cur.rowcount == 0:
            await db.execute(
                """UPDATE dashboard_datapoints_raw
                   SET raw_value = ?, raw_unit = ?, extractor = ?
                   WHERE metric = ?
                     AND period = ?
                     AND broker = ?
                     AND source_filename = ?
                     AND source_page = ?
                     AND source_quote = ?
                     AND (extractor = '' OR extractor = 'legacy_datapoints'
                          OR extractor = 'excel:legacy_import')""",
                (
                    float(r["value"]), terminal_unit,
                    _legacy_excel_extractor(r["source_filename"] or ""),
                    r["metric"], r["period"], r["broker"] or "",
                    r["source_filename"] or "", int(r["source_page"] or 0),
                    r["source_quote"] or "",
                ),
            )
    updated = await mark_legacy_excel_rows()
    await db.commit()
    return inserted + updated


async def store_raw_dashboard_datapoints(
    datapoints: list[dict],
    replace_metric_broker: tuple[str, str] | None = None,
    replace_frequency: str | None = None,
    extractor: str = "dashboard_rag_refresh",
) -> int:
    """Persist raw extraction candidates. Validation runs separately."""
    await ensure_dashboard_datapoint_tables()
    db = await get_db()
    if replace_metric_broker is not None:
        metric, broker = replace_metric_broker
        freq = (replace_frequency or "").strip().lower()
        period_clause = ""
        if freq == "yearly":
            period_clause = " AND instr(upper(period), 'Q') = 0 AND instr(period, '-') = 0"
        elif freq == "quarterly":
            period_clause = " AND instr(upper(period), 'Q') > 0"
        for table in (
            "dashboard_datapoints_verified",
            "dashboard_datapoints_quarantine",
        ):
            await db.execute(
                f"DELETE FROM {table} WHERE metric=? AND broker=?{period_clause}",
                (metric, broker),
            )

    now = datetime.now().isoformat(timespec="seconds")
    inserted = 0
    for dp in datapoints:
        metric = dp.get("metric", "")
        period = dp.get("period", "")
        raw_value = dp.get("raw_value", dp.get("value"))
        if metric not in CANONICAL_METRICS or not period or raw_value is None:
            continue
        period_int = dp.get("period_int") or _period_int(period)
        source_broker = normalize_broker(dp.get("source_broker") or dp.get("broker") or "")
        source_filename = dp.get("source_filename") or ""
        source_date = dp.get("source_date") or ""
        source_type = _source_type_from(extractor, source_filename, dp.get("source_type") or "")
        subject_entity = _infer_subject_entity(
            metric,
            dp.get("source_quote") or "",
            dp.get("subject_entity") or "",
        )
        subject_type = dp.get("subject_type") or _subject_type(subject_entity)
        frequency = dp.get("frequency") or _period_frequency(period)
        source_id = int(dp.get("source_id") or 0)
        if not source_id:
            source_id = await _source_document_id(
                db,
                source_type=source_type,
                source_broker=source_broker,
                filename=source_filename,
                source_date=source_date,
                model_date=dp.get("model_date") or "",
                doc_type=dp.get("doc_type") or "",
                source_doc_type=dp.get("source_doc_type") or "",
                media_type=dp.get("media_type") or "",
            )
        cur = await db.execute(
            """INSERT OR IGNORE INTO dashboard_datapoints_raw
               (segment, metric, period, period_int, raw_value, raw_unit, broker,
                source_filename, source_date, source_page, source_quote,
                source_sheet, source_cell, extractor, created_at, source_id,
                source_type, source_broker, subject_entity, subject_type,
                frequency, batch_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                dp.get("segment") or CANONICAL_METRICS[metric][1],
                metric,
                period,
                int(period_int),
                float(raw_value),
                dp.get("raw_unit") or "",
                source_broker,
                source_filename,
                source_date,
                int(dp.get("source_page") or 0),
                dp.get("source_quote") or "",
                dp.get("source_sheet") or "",
                dp.get("source_cell") or "",
                extractor,
                now,
                source_id,
                source_type,
                source_broker,
                subject_entity,
                subject_type,
                frequency,
                dp.get("batch_id") or "",
            ),
        )
        if cur.rowcount > 0:
            inserted += cur.rowcount
        else:
            await db.execute(
                """UPDATE dashboard_datapoints_raw
                   SET segment=?, period_int=?, raw_value=?, raw_unit=?,
                       source_date=?, source_sheet=?, source_cell=?,
                       extractor=?, source_id=?, source_type=?,
                       source_broker=?, subject_entity=?, subject_type=?,
                       frequency=?, batch_id=?, canonical_value=NULL,
                       canonical_unit='', winner_reason=''
                   WHERE metric=?
                     AND period=?
                     AND broker=?
                     AND source_filename=?
                     AND source_page=?
                     AND source_quote=?""",
                (
                    dp.get("segment") or CANONICAL_METRICS[metric][1],
                    int(period_int),
                    float(raw_value),
                    dp.get("raw_unit") or "",
                    source_date,
                    dp.get("source_sheet") or "",
                    dp.get("source_cell") or "",
                    extractor,
                    source_id,
                    source_type,
                    source_broker,
                    subject_entity,
                    subject_type,
                    frequency,
                    dp.get("batch_id") or "",
                    metric,
                    period,
                    source_broker,
                    source_filename,
                    int(dp.get("source_page") or 0),
                    dp.get("source_quote") or "",
                ),
            )
    await db.commit()
    return inserted


async def rebuild_verified_datapoints(metrics: set[str] | None = None) -> dict:
    """Rebuild verified/quarantine tables from raw candidates."""
    await ensure_dashboard_datapoint_tables()
    db = await get_db()
    where = ""
    params: list = []
    if metrics:
        placeholders = ",".join("?" for _ in metrics)
        where = f"WHERE metric IN ({placeholders})"
        params = list(metrics)
        await db.execute(f"DELETE FROM dashboard_datapoints_verified {where}", params)
        await db.execute(f"DELETE FROM dashboard_datapoints_quarantine {where}", params)
    else:
        await db.execute("DELETE FROM dashboard_datapoints_verified")
        await db.execute("DELETE FROM dashboard_datapoints_quarantine")

    rows = await db.execute_fetchall(
        f"""SELECT * FROM dashboard_datapoints_raw {where}
            ORDER BY metric, broker, period_int""",
        params,
    )
    active_excel_dates = await get_active_excel_broker_dates()
    active_excel_file_keys = await get_active_excel_file_keys()
    active_excel_brokers = set(active_excel_dates)
    active_excel_key_dates: dict[tuple[str, str, str, int, str], str] = {}
    for r in rows:
        extractor = r["extractor"] or ""
        source_broker = normalize_broker(r["source_broker"] or r["broker"] or "")
        broker_key = source_broker.lower()
        source_type = _source_type_from(extractor, r["source_filename"] or "", r["source_type"] or "")
        is_excel_row = source_type == "excel" or extractor.startswith("excel:")
        if not is_excel_row or not broker_key:
            continue
        source_filename = r["source_filename"] or ""
        is_active_excel = (
            (broker_key, source_filename) in active_excel_file_keys
            if active_excel_file_keys
            else broker_key in active_excel_brokers
        )
        if not is_active_excel:
            continue
        subject_entity = _infer_subject_entity(
            r["metric"],
            r["source_quote"] or "",
            r["subject_entity"] or "",
        )
        frequency = r["frequency"] or _period_frequency(r["period"])
        key = (r["metric"], broker_key, subject_entity, int(r["period_int"]), frequency)
        row_date = _date_key(r["source_date"] or "") or _date_key(source_filename)
        if row_date > active_excel_key_dates.get(key, ""):
            active_excel_key_dates[key] = row_date

    now = datetime.now().isoformat(timespec="seconds")
    verified = 0
    quarantined = 0
    candidates: list[dict] = []
    winners: dict[tuple[str, str, str, int, str], dict] = {}
    for r in rows:
        extractor = r["extractor"] or ""
        source_broker = normalize_broker(r["source_broker"] or r["broker"] or "")
        broker_key = source_broker.lower()
        source_type = _source_type_from(extractor, r["source_filename"] or "", r["source_type"] or "")
        subject_entity = _infer_subject_entity(
            r["metric"],
            r["source_quote"] or "",
            r["subject_entity"] or "",
        )
        subject_type = r["subject_type"] or _subject_type(subject_entity)
        frequency = r["frequency"] or _period_frequency(r["period"])
        key = (r["metric"], broker_key, subject_entity, int(r["period_int"]), frequency)
        ok, value, unit, note = _normalize_strict(
            r["metric"],
            float(r["raw_value"]),
            r["raw_unit"],
            r["extractor"] or "",
        )
        if not ok:
            await _insert_quarantine(db, r, note, now)
            quarantined += 1
            continue
        if source_type not in ALLOWED_DASHBOARD_SOURCE_TYPES:
            await _insert_quarantine(db, r, f"source type {source_type!r} not allowed for dashboard", now)
            quarantined += 1
            continue
        source_id = int(r["source_id"] or 0)
        if not source_id:
            source_id = await _source_document_id(
                db,
                source_type=source_type,
                source_broker=source_broker,
                filename=r["source_filename"] or "",
                source_date=r["source_date"] or "",
                model_date="",
                doc_type="",
                source_doc_type="",
                media_type="",
            )
        await db.execute(
            """UPDATE dashboard_datapoints_raw
               SET source_id=?, source_type=?, source_broker=?,
                   subject_entity=?, subject_type=?, frequency=?,
                   canonical_value=?, canonical_unit=?
               WHERE id=?""",
            (
                source_id, source_type, source_broker, subject_entity,
                subject_type, frequency, float(value), unit, r["id"],
            ),
        )
        source_filename = r["source_filename"] or ""
        is_excel = source_type == "excel" or extractor.startswith("excel:")
        is_active_excel = (
            is_excel
            and bool(broker_key)
            and (
                (broker_key, source_filename) in active_excel_file_keys
                if active_excel_file_keys
                else broker_key in active_excel_brokers
            )
        )
        if is_excel and active_excel_file_keys and broker_key in active_excel_brokers and not is_active_excel:
            await _insert_quarantine(db, r, "inactive Excel model", now)
            quarantined += 1
            continue
        if not is_excel:
            freshness_reason = _non_excel_freshness_reject_reason(r["source_date"] or "")
            if freshness_reason:
                await _insert_quarantine(db, r, freshness_reason, now)
                quarantined += 1
                continue
        source_date = _date_key(r["source_date"] or "")
        cutoff_date = active_excel_key_dates.get(key, "")
        newer_than_excel = bool(
            not is_excel
            and source_date
            and cutoff_date
            and source_date > cutoff_date
        )
        if cutoff_date and not is_excel and not newer_than_excel:
            reason = "suppressed by Excel source"
            if cutoff_date:
                if source_date:
                    reason += f" (source date {source_date} <= Excel date {cutoff_date})"
                else:
                    reason = "missing source date cannot override active Excel"
            await _insert_quarantine(db, r, reason, now)
            quarantined += 1
            continue
        winner_reason = "verified"
        if is_active_excel or (is_excel and not active_excel_file_keys):
            winner_reason = "active_excel"
        elif newer_than_excel:
            winner_reason = "newer_allowed_source"
        elif source_type == "user_input":
            winner_reason = "user_confirmed"
        elif source_date:
            winner_reason = "latest_non_excel"
        item = {
            "row": r,
            "value": value,
            "unit": unit,
            "note": note,
            "is_excel": is_excel,
            "source_type": source_type,
            "source_broker": source_broker,
            "subject_entity": subject_entity,
            "subject_type": subject_type,
            "frequency": frequency,
            "source_date_key": source_date,
            "source_id": source_id,
            "winner_reason": winner_reason,
            "key": key,
        }
        candidates.append(item)
        cur = winners.get(item["key"])
        if cur is None:
            winners[item["key"]] = item
        else:
            cur_score = _winner_priority(cur["source_type"], cur["source_date_key"])
            new_score = _winner_priority(item["source_type"], item["source_date_key"])
            if new_score > cur_score or (
                new_score == cur_score and int(item["row"]["id"]) > int(cur["row"]["id"])
            ):
                winners[item["key"]] = item

    for item in candidates:
        r = item["row"]
        winner = winners.get(item["key"])
        if winner is not item:
            reason = "superseded by verified winner"
            if item["is_excel"] and winner and winner.get("winner_reason") == "newer_allowed_source":
                reason = "overridden by newer allowed source"
            await _insert_quarantine(db, r, reason, now)
            quarantined += 1
            continue
        cur = await db.execute(
            """INSERT OR IGNORE INTO dashboard_datapoints_verified
               (raw_id, segment, metric, period, period_int, value, unit, raw_unit,
                broker, source_filename, source_date, source_page, source_quote,
                source_sheet, source_cell, extractor, verification_status,
                verification_note, created_at, source_id, source_type,
                source_broker, subject_entity, subject_type, frequency,
                batch_id, canonical_value, canonical_unit, winner_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                r["id"], r["segment"], r["metric"], r["period"], r["period_int"],
                float(item["value"]), item["unit"], r["raw_unit"] or "", item["source_broker"],
                r["source_filename"] or "", r["source_date"] or "",
                int(r["source_page"] or 0), r["source_quote"] or "",
                r["source_sheet"] or "", r["source_cell"] or "",
                r["extractor"] or "", "verified", item["note"], now,
                int(item["source_id"] or 0), item["source_type"], item["source_broker"],
                item["subject_entity"], item["subject_type"], item["frequency"],
                r["batch_id"] or "", float(item["value"]), item["unit"],
                item["winner_reason"],
            ),
        )
        verified += cur.rowcount if cur.rowcount > 0 else 0

    peer_result = await _apply_peer_quarantine(db, metrics, now)
    spike_result = await _apply_series_spike_quarantine(db, metrics, now)
    await db.commit()
    return {
        "verified": verified,
        "quarantined": quarantined + peer_result["quarantined"] + spike_result["quarantined"],
        "needs_review": peer_result["needs_review"],
    }


async def _insert_quarantine(db, r, reason: str, now: str) -> None:
    def g(key: str, default=""):
        try:
            return r[key]
        except Exception:
            return r.get(key, default) if isinstance(r, dict) else default

    source_broker = normalize_broker(g("source_broker") or g("broker") or "")
    source_type = _source_type_from(g("extractor") or "", g("source_filename") or "", g("source_type") or "")
    subject_entity = _infer_subject_entity(g("metric") or "", g("source_quote") or "", g("subject_entity") or "")
    subject_type = g("subject_type") or _subject_type(subject_entity)
    frequency = g("frequency") or _period_frequency(g("period") or "")
    await db.execute(
        """INSERT OR IGNORE INTO dashboard_datapoints_quarantine
           (raw_id, segment, metric, period, period_int, raw_value, raw_unit,
            broker, source_filename, source_date, source_page, source_quote,
            source_sheet, source_cell, extractor, reason, created_at,
            source_id, source_type, source_broker, subject_entity, subject_type,
            frequency, batch_id, canonical_value, canonical_unit)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            g("id", 0), g("segment") or "", g("metric") or "", g("period") or "", g("period_int") or 0,
            float(g("raw_value", 0) or 0), g("raw_unit") or "", source_broker,
            g("source_filename") or "", g("source_date") or "",
            int(g("source_page", 0) or 0), g("source_quote") or "",
            g("source_sheet") or "", g("source_cell") or "",
            g("extractor") or "", reason, now,
            int(g("source_id", 0) or 0), source_type, source_broker,
            subject_entity, subject_type, frequency, g("batch_id") or "",
            g("canonical_value", None), g("canonical_unit") or "",
        ),
    )


async def _apply_peer_quarantine(db, metrics: set[str] | None, now: str) -> dict:
    where = ""
    params: list = []
    if metrics:
        placeholders = ",".join("?" for _ in metrics)
        where = f"WHERE metric IN ({placeholders})"
        params = list(metrics)
    rows = await db.execute_fetchall(
        f"SELECT * FROM dashboard_datapoints_verified {where}", params
    )
    grouped: dict[tuple[str, str, int], list] = {}
    for r in rows:
        if _peer_gate_enabled(r["metric"]) and r["value"]:
            grouped.setdefault((r["metric"], r["subject_entity"] or "Industry Total", int(r["period_int"])), []).append(r)

    quarantined = 0
    needs_review = 0
    for (metric, _subject_entity, period_int), peers in grouped.items():
        vals = sorted(abs(float(r["value"])) for r in peers if r["value"])
        if len(vals) < 2:
            continue
        if len(vals) == 2:
            lo, hi = vals[0], vals[1]
            if lo > 0 and hi / lo >= PEER_OUTLIER_RATIO:
                note = f"only two peer values; spread is {hi / lo:.1f}x"
                for r in peers:
                    await db.execute(
                        """UPDATE dashboard_datapoints_verified
                           SET verification_status='needs_review',
                               verification_note=?
                           WHERE id=?""",
                        (note, r["id"]),
                    )
                    needs_review += 1
            continue

        med = vals[len(vals) // 2]
        if med <= 0:
            continue
        for r in peers:
            v = abs(float(r["value"]))
            if v / med >= PEER_OUTLIER_RATIO or med / v >= PEER_OUTLIER_RATIO:
                reason = f"peer spread >{PEER_OUTLIER_RATIO:.0f}x vs median {med:.3g}"
                await _insert_quarantine(db, {
                    "id": r["raw_id"], "segment": r["segment"], "metric": r["metric"],
                    "period": r["period"], "period_int": r["period_int"],
                    "raw_value": r["value"], "raw_unit": r["raw_unit"],
                    "broker": r["broker"], "source_filename": r["source_filename"],
                    "source_date": r["source_date"], "source_page": r["source_page"],
                    "source_quote": r["source_quote"],
                    "source_sheet": r["source_sheet"],
                    "source_cell": r["source_cell"],
                    "extractor": r["extractor"],
                    "source_id": r["source_id"],
                    "source_type": r["source_type"],
                    "source_broker": r["source_broker"],
                    "subject_entity": r["subject_entity"],
                    "subject_type": r["subject_type"],
                    "frequency": r["frequency"],
                    "batch_id": r["batch_id"],
                    "canonical_value": r["canonical_value"],
                    "canonical_unit": r["canonical_unit"],
                }, reason, now)
                await db.execute("DELETE FROM dashboard_datapoints_verified WHERE id=?", (r["id"],))
                quarantined += 1
    return {"quarantined": quarantined, "needs_review": needs_review}


async def _apply_series_spike_quarantine(db, metrics: set[str] | None, now: str) -> dict:
    """Hide isolated same-series magnitude mistakes before the chart layer.

    This catches RAG/unit slips that do not have enough same-period peers to
    arbitrate, such as one broker series where a single point is 100x away from
    its own surrounding values.
    """
    where = ""
    params: list = []
    if metrics:
        placeholders = ",".join("?" for _ in metrics)
        where = f"WHERE metric IN ({placeholders})"
        params = list(metrics)
    rows = await db.execute_fetchall(
        f"SELECT * FROM dashboard_datapoints_verified {where}", params
    )
    grouped: dict[tuple[str, str, str], list] = {}
    for r in rows:
        if not _peer_gate_enabled(r["metric"]):
            continue
        try:
            v = abs(float(r["value"]))
        except (TypeError, ValueError):
            continue
        if v <= 0:
            continue
        grouped.setdefault((r["metric"], r["broker"] or "", r["subject_entity"] or "Industry Total"), []).append(r)

    quarantined = 0
    for (metric, broker, _subject_entity), pts in grouped.items():
        if len(pts) < 3:
            continue
        vals = sorted(abs(float(r["value"])) for r in pts if abs(float(r["value"])) > 0)
        if len(vals) < 3:
            continue
        med = vals[len(vals) // 2]
        if med <= 0:
            continue
        for r in pts:
            v = abs(float(r["value"]))
            if v / med < 50.0 and med / v < 50.0:
                continue
            reason = f"same-series spike >50x vs {broker or 'broker'} {metric} median {med:.3g}"
            await _insert_quarantine(db, {
                "id": r["raw_id"], "segment": r["segment"], "metric": r["metric"],
                "period": r["period"], "period_int": r["period_int"],
                "raw_value": r["value"], "raw_unit": r["raw_unit"],
                "broker": r["broker"], "source_filename": r["source_filename"],
                "source_date": r["source_date"], "source_page": r["source_page"],
                "source_quote": r["source_quote"],
                "source_sheet": r["source_sheet"],
                "source_cell": r["source_cell"],
                "extractor": r["extractor"],
                "source_id": r["source_id"],
                "source_type": r["source_type"],
                "source_broker": r["source_broker"],
                "subject_entity": r["subject_entity"],
                "subject_type": r["subject_type"],
                "frequency": r["frequency"],
                "batch_id": r["batch_id"],
                "canonical_value": r["canonical_value"],
                "canonical_unit": r["canonical_unit"],
            }, reason, now)
            await db.execute("DELETE FROM dashboard_datapoints_verified WHERE id=?", (r["id"],))
            quarantined += 1
    return {"quarantined": quarantined}


async def prepare_dashboard_terminal(metrics: set[str] | None = None) -> dict:
    """Cheap deterministic refresh before serving the dashboard."""
    inserted = await sync_legacy_datapoints_to_raw()
    migrated = await mark_legacy_excel_rows()
    result = await rebuild_verified_datapoints(metrics)
    result["raw_inserted"] = inserted
    result["legacy_excel_rows_marked"] = migrated
    return result
