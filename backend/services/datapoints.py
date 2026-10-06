"""Structured datapoint extraction from PDF text into the fact pool.

OpenAI extracts numeric time-series datapoints (segment, metric, period, value,
unit, broker). Values are normalized to canonical units, stored incrementally in
the SQLite `datapoints` table (INSERT OR IGNORE) and exported as local
Fact-schema JSON for inspection.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

from ..db.connection import get_db
from ..paths import DATA_DIR

logger = logging.getLogger(__name__)

_DATAPOINT_OPENAI_MODEL = (
    os.getenv("DATAPOINT_OPENAI_MODEL")
    or os.getenv("OPENAI_REPORT_MODEL")
    or os.getenv("DASHBOARD_OPENAI_MODEL")
    or "gpt-6-sol"
)

FACT_POOL_DIR = DATA_DIR / "fact_exports"

# Canonical metrics and their target units. The LLM is asked to output these
# metric names; the conversion table below normalizes incoming units.
CANONICAL_METRICS = {
    # metric: (canonical_unit, segment_hint)
    "dram_bit_demand": ("bn Gb", "DRAM"),
    "dram_bit_supply": ("bn Gb", "DRAM"),
    "dram_bit_growth": ("% YoY", "DRAM"),
    "dram_sufficiency": ("%", "DRAM"),
    "dram_contract_asp": ("$/Gb", "DRAM"),
    "dram_spot_asp": ("$/unit", "DRAM"),
    "dram_inventory_weeks": ("weeks", "DRAM"),
    "dram_wafer_capacity": ("kwpm", "DRAM"),
    "dram_capex": ("$bn", "DRAM"),
    "dram_utilization": ("%", "DRAM"),
    "ddr4_contract_price": ("$/unit", "DDR"),
    "ddr5_contract_price": ("$/unit", "DDR"),
    "ddr4_spot_price": ("$/unit", "DDR"),
    "ddr5_spot_price": ("$/unit", "DDR"),
    "nand_bit_demand": ("EB", "NAND"),
    "nand_bit_supply": ("EB", "NAND"),
    "nand_bit_growth": ("% YoY", "NAND"),
    "nand_sufficiency": ("%", "NAND"),
    "nand_contract_asp": ("$/GB", "NAND"),
    "nand_spot_asp": ("$/unit", "NAND"),
    "nand_wafer_price": ("$/wafer", "NAND"),
    "nand_inventory_weeks": ("weeks", "NAND"),
    "nand_wafer_capacity": ("kwpm", "NAND"),
    "nand_capex": ("$bn", "NAND"),
    "nand_utilization": ("%", "NAND"),
    "hbm_tam": ("$bn", "HBM"),
    "hbm_bit_demand": ("mn GB", "HBM"),
    "hbm_content_per_accelerator": ("GB", "HBM"),
    "hbm_wafer_capacity": ("kwpm", "HBM"),
    "hbm_asp": ("$/Gb", "HBM"),
    "memory_capex": ("$bn", "General"),
    "memory_revenue": ("$bn", "General"),
    "cloud_capex": ("$bn", "General"),
    "server_dram_share": ("%", "General"),
    # Market share by player. Unit = revenue or bit share % of segment total.
    # Each producer is a SEPARATE canonical metric so each renders as its own
    # series in the merged Market-Share panels (one panel per segment).
    "dram_share_samsung": ("%", "DRAM"),
    "dram_share_skhynix": ("%", "DRAM"),
    "dram_share_micron": ("%", "DRAM"),
    "dram_share_others": ("%", "DRAM"),
    "nand_share_samsung": ("%", "NAND"),
    "nand_share_skhynix": ("%", "NAND"),
    "nand_share_kioxia": ("%", "NAND"),
    "nand_share_wdc": ("%", "NAND"),
    "nand_share_micron": ("%", "NAND"),
    "nand_share_ymtc": ("%", "NAND"),
    "nand_share_others": ("%", "NAND"),
    "hbm_share_skhynix": ("%", "HBM"),
    "hbm_share_samsung": ("%", "HBM"),
    "hbm_share_micron": ("%", "HBM"),
}

_SEGMENTS = ("DRAM", "DDR", "NAND", "HBM", "General")


# Built-in conversion factors per canonical unit: {unit-key: multiply-factor}.
# Unit keys are lowercase with spaces/dots/dashes stripped.
# Equivalence bases: 1 GB = 8 Gb; mn XGb-eq units = X mn Gb of bits.
_BUILTIN_CONVERSIONS: dict[str, dict[str, float]] = {
    "bn Gb": {
        "bngb": 1, "bgb": 1, "bngbit": 1, "bn1gb": 1, "bn1gbeq": 1, "bngbeq": 1,
        "bn2gb": 2, "bn2gbeq": 2,
        "bn8gb": 8, "bn8gbeq": 8,
        "bn16gb": 16, "bn16gbeq": 16,
        "mn1gb": 1e-3, "mn1gbeq": 1e-3, "m1gb": 1e-3, "mngbeq": 1e-3,
        "mn2gb": 2e-3, "mn2gbeq": 2e-3,
        "mn4gb": 4e-3, "mn4gbeq": 4e-3,
        "mn8gb": 8e-3, "mn8gbeq": 8e-3, "m8gb": 8e-3, "mn8gbe": 8e-3,
        "mn16gb": 16e-3, "mn16gbeq": 16e-3,
        "mn32gb": 32e-3, "mn32gbeq": 32e-3,
        "mngb": 8e-3, "mgb": 8e-3, "mngbyte": 8e-3, "milliongb": 8e-3,  # mn GB -> bn Gb
        "eb": 8.0,            # 1 EB = 1e9 GB = 8e9 Gb = 8 bn Gb
        "exabyte": 8.0, "exabytes": 8.0,
        "tbngb": 1000.0, "tngb": 1000.0,  # tn Gb
    },
    "EB": {
        "eb": 1, "exabyte": 1, "exabytes": 1,
        "zb": 1000.0,
        "pb": 1e-3, "petabyte": 1e-3, "petabytes": 1e-3,
        "tb": 1e-6, "terabyte": 1e-6, "terabytes": 1e-6,
        "mntb": 1.0,          # 1e6 TB = 1 EB
        "bngb": 1.0,          # 1e9 GB = 1 EB (GB reading; Gb would be 0.125)
        "mngb": 1e-3, "milliongb": 1e-3,
        "gb": 1e-9,
        "mn128gb": 0.016, "mn128gbeq": 0.016,   # mn 128Gb-eq = 16e6 GB
        "mn256gb": 0.032, "mn256gbeq": 0.032,
        "mn512gb": 0.064, "mn512gbeq": 0.064,
        "bn256gb": 32.0, "bn256gbeq": 32.0,
        "mn1tb": 0.128, "mn1tbeq": 0.128,        # mn 1Tb-eq = 125e6 GB
    },
    "kwpm": {
        "kwpm": 1, "kwspm": 1, "kwsm": 1, "kwafer": 1, "kwafers": 1,
        "kwaferspermonth": 1, "kwafer/month": 1, "kw/m": 1,
        "wpm": 1e-3, "wspm": 1e-3, "waferspermonth": 1e-3, "wafers/month": 1e-3,
        "mnwaferspermonth": 1000.0, "mnwpm": 1000.0,
        "mnwafersperyear": 1000.0 / 12, "mnwpy": 1000.0 / 12,
        "kwpy": 1.0 / 12, "kwafersperyear": 1.0 / 12,
    },
    "$bn": {
        "$bn": 1, "usdbn": 1, "bn": 1, "billion": 1, "$b": 1, "b": 1, "usd bn": 1,
        "$mn": 1e-3, "usdmn": 1e-3, "mn": 1e-3, "million": 1e-3, "$m": 1e-3, "m": 1e-3,
        "$tn": 1000.0, "tn": 1000.0, "trillion": 1000.0, "$t": 1000.0,
        "$k": 1e-6, "k": 1e-6,
    },
    "weeks": {
        "week": 1, "weeks": 1, "wk": 1, "wks": 1, "weeksofsupply": 1, "wos": 1,
        "day": 1 / 7, "days": 1 / 7, "dio": 1 / 7, "daysofinventory": 1 / 7,
        "month": 4.345, "months": 4.345, "mo": 4.345,
    },
    "%": {
        "%": 1, "pct": 1, "percent": 1, "%yoy": 1, "yoy%": 1, "%qoq": 1, "qoq%": 1,
        "ratio": 100.0, "x": 100.0,
        "bps": 0.01, "bp": 0.01,
    },
    "% YoY": {
        "%": 1, "pct": 1, "percent": 1, "%yoy": 1, "yoy%": 1,
        "ratio": 100.0, "x": 100.0,
        "bps": 0.01, "bp": 0.01,
    },
    "mn GB": {
        "mngb": 1, "milliongb": 1, "mn8gb": 1, "mn8gbeq": 1,  # 8Gb = 1GB
        "bngb": 125.0,        # bn Gb -> mn GB (1e9 Gb = 125e6 GB)
        "bngbyte": 1000.0,    # bn GB -> mn GB
        "gb": 1e-6,
        "tb": 1e-3, "mntb": 1000.0,
        "eb": 1000.0,         # 1 EB = 1000 mn GB
        "mn16gb": 2.0, "mn16gbeq": 2.0,
        "mn24gb": 3.0, "mn36gb": 4.5,
        "bnstacksx24gb": 24000.0,
    },
    "$/Gb": {
        "$/gb": 1, "usd/gb": 1, "$pergb": 1,
        "$/gbyte": 1 / 8, "$/gigabyte": 1 / 8,  # $/GB -> $/Gb
        "cents/gb": 0.01, "c/gb": 0.01,
        "$/mb": 1000.0,
    },
    "$/GB": {
        "$/gb": 1, "usd/gb": 1, "$pergb": 1,
        "cents/gb": 0.01, "c/gb": 0.01,
        "$/tb": 1e-3, "$/terabyte": 1e-3,
    },
    "GB": {
        "gb": 1, "gigabyte": 1, "gigabytes": 1,
        "gbit": 1 / 8, "gbits": 1 / 8,
        "tb": 1000.0,
        "mb": 1e-3,
    },
}

from ..paths import UNIT_RULES_PATH as _UNIT_RULES_PATH
_learned_rules: dict[str, float] | None = None


def _unit_key(unit: str) -> str:
    return (unit or "").strip().lower().replace(" ", "").replace(".", "").replace("-", "").replace("_", "")


def _compact_unit_preserve_case(unit: str) -> str:
    return re.sub(r"[\s._\-/(),]+", "", unit or "")


def _is_million_gbit_unit(unit: str) -> bool:
    compact = _compact_unit_preserve_case(unit)
    lower = compact.lower()
    if re.fullmatch(r"(?:m|mn|mm|million)g(?:bit|bits)", lower):
        return True
    return bool(re.fullmatch(r"(?:m|mn|mm|M|MN|MM)[Gg]b", compact))


def _load_learned_rules() -> dict[str, float]:
    """Learned conversions ('metric|unitkey' -> factor) written by the LLM
    unit arbiter. Reloaded lazily; survives restarts."""
    global _learned_rules
    if _learned_rules is None:
        try:
            _learned_rules = json.loads(_UNIT_RULES_PATH.read_text(encoding="utf-8"))
        except Exception:
            _learned_rules = {}
    return _learned_rules


def save_learned_rule(metric: str, unit: str, factor: float) -> None:
    """Persist a conversion discovered by the unit arbiter so the same
    (metric, unit) auto-converts on every future extraction."""
    global _learned_rules
    rules = _load_learned_rules()
    rules[f"{metric}|{_unit_key(unit)}"] = factor
    _UNIT_RULES_PATH.parent.mkdir(parents=True, exist_ok=True)
    _UNIT_RULES_PATH.write_text(json.dumps(rules, indent=1), encoding="utf-8")
    _learned_rules = rules


def _normalize(metric: str, value: float, unit: str) -> tuple[float, str]:
    """Convert value to the canonical unit for its metric. Returns (value, unit).

    Order: built-in conversion table -> learned rules (from the LLM unit
    arbiter) -> pass-through with canonical label."""
    canon_unit = CANONICAL_METRICS.get(metric, (unit, None))[0]
    u = _unit_key(unit)

    if _is_million_gbit_unit(unit):
        if canon_unit == "mn GB":
            return value / 8.0, canon_unit
        if canon_unit == "bn Gb":
            return value * 0.001, canon_unit
        if canon_unit == "EB":
            return value * 0.000125, canon_unit

    table = _BUILTIN_CONVERSIONS.get(canon_unit)
    if table and u in table:
        return value * table[u], canon_unit

    learned = _load_learned_rules()
    factor = learned.get(f"{metric}|{u}")
    if factor:
        return value * factor, canon_unit

    # $/unit, $/wafer and unknown units pass through with the canonical label
    return value, canon_unit


def _period_int(period: str) -> int:
    """Sortable int from period strings: '2026'->20260000, '2026Q2'->20260200, '2026-06'->20260600."""
    p = (period or "").strip().upper().replace("E", "")
    m = re.match(r"^(\d{4})$", p)
    if m:
        return int(m.group(1)) * 10000
    m = re.match(r"^(\d{4})Q([1-4])$", p)
    if m:
        return int(m.group(1)) * 10000 + int(m.group(2)) * 300
    m = re.match(r"^(\d{4})-(\d{1,2})$", p)
    if m:
        return int(m.group(1)) * 10000 + int(m.group(2)) * 100
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", p)
    if m:
        return int(m.group(1)) * 10000 + int(m.group(2)) * 100 + int(m.group(3))
    return 0


async def ensure_datapoints_table() -> None:
    db = await get_db()
    await db.execute(
        """CREATE TABLE IF NOT EXISTS datapoints (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               segment TEXT NOT NULL,
               metric TEXT NOT NULL,
               period TEXT NOT NULL,
               period_int INTEGER NOT NULL DEFAULT 0,
               value REAL NOT NULL,
               unit TEXT NOT NULL,
               broker TEXT NOT NULL DEFAULT '',
               source_filename TEXT NOT NULL DEFAULT '',
               source_date TEXT NOT NULL DEFAULT '',
               created_at TEXT NOT NULL,
               UNIQUE(segment, metric, period, broker, source_filename)
           )"""
    )
    try:  # original unit as written in the doc — used by the unit arbiter
        await db.execute("ALTER TABLE datapoints ADD COLUMN raw_unit TEXT NOT NULL DEFAULT ''")
    except Exception:
        pass
    try:  # verbatim source quote — provenance for the citation gate
        await db.execute("ALTER TABLE datapoints ADD COLUMN source_quote TEXT NOT NULL DEFAULT ''")
    except Exception:
        pass
    try:
        await db.execute("ALTER TABLE datapoints ADD COLUMN source_page INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass
    await db.commit()


def _build_extraction_prompt(pages_block: str, broker: str, date: str) -> str:
    metric_list = "\n".join(
        f"  - {m} (unit: {u[0]}, segment: {u[1]})" for m, u in CANONICAL_METRICS.items()
    )
    return f"""You are extracting structured numeric datapoints from a semiconductor memory research report.

REPORT BROKER: {broker or "unknown"}
REPORT DATE: {date or "unknown"}

DOCUMENT TEXT (per page):
{pages_block}

TASK: Extract ALL numeric time-series datapoints that match the canonical metrics below.

CANONICAL METRICS (use these exact metric names):
{metric_list}

For each datapoint return:
- "segment": one of DRAM, DDR, NAND, HBM, General
- "metric": one of the canonical metric names above
- "period": the time period the value applies to — "2026", "2026Q2", or "2026-06" format.
  Estimates like "2026e" -> "2026". Quarterly like "2Q26" -> "2026Q2".
- "value": the number only (float). Convert "48%" -> 48, "$2.1/Gb" -> 2.1, "$106bn" -> 106
- "unit": the unit AS WRITTEN in the document (e.g. "%", "$bn", "mn GB", "kwpm", "EB", "$/Gb", "weeks", "days")
- "broker": "{broker}"
- "page": the page number (from the [PAGE n] marker) where you read the value
- "quote": the EXACT text fragment (max 120 chars) from the document that contains
  the value — copied CHARACTER-FOR-CHARACTER including the number as printed
  (e.g. "57,379" not "57379"). No paraphrasing. Datapoints whose quote cannot be
  found verbatim in the document, or whose quote does not contain the number,
  are AUTOMATICALLY DISCARDED — so only extract what you can cite.

RULES:
- Blocks starting with a [TABLE] line are tables serialized as markdown pipe-rows
  (| cell | cell | ...). The FIRST row(s) usually hold column headers (years/quarters);
  each later row starts with the metric name. Match each value to its column header
  period. PREFER these table blocks over loose page text — they are the most reliable.
- UNIT CLASSIFICATION (critical — misclassifying creates 100x errors):
  * wafer_capacity metrics ONLY for values measured in WAFERS: kwpm, wspm,
    "k wafers/month". Industry DRAM/NAND wafer capacity is roughly 300-3000 kwpm;
    HBM wafer capacity roughly 50-700 kwpm. If your number is far outside that
    scale, it is NOT wafer capacity.
  * Rows labeled "(8Gbe., M)", "8Gb eq", "Gb-eq", "1Gb eq", "M units" are BIT
    supply/demand/consumption — use *_bit_supply / *_bit_demand and write the
    unit exactly as the base says (e.g. "mn 8Gb", "mn 1Gb", "bn Gb").
  * Never extract the same table row under two different metrics.
  * Copy the unit EXACTLY as printed (including the equivalence base). Do not
    guess a unit that is not visible in the document.
- Extract EVERY year/quarter of a series, not just the latest (e.g. 2024, 2025, 2026e, 2027e, 2028e all become separate datapoints)
- Historical AND forecast values both count
- Only extract values you can read with high confidence; skip vague mentions
- Skip percentages of unclear base, page numbers, footnote numbers
- If the document has no matching datapoints, return an empty array

Return STRICT JSON only, no markdown fence:
{{"datapoints": [{{"segment": "...", "metric": "...", "period": "...", "value": 0.0, "unit": "...", "broker": "...", "page": 0, "quote": "..."}}]}}"""


def _ws_norm(s: str) -> str:
    return " ".join((s or "").split()).lower()


def _value_in_quote(value: float, quote: str) -> bool:
    """Does the raw numeric value appear in the quote (comma/format tolerant)?"""
    q = (quote or "").replace("−", "-")
    if not q.strip():
        return False
    target = float(value)
    nums: list[float] = []
    for m in re.finditer(
        r"(?<![A-Za-z0-9])(?:US\$|USD|[$€£¥])?\s*(-?\d[\d,]*(?:\.\d+)?)(?:\s*([kKmMbBtT]))?(?![A-Za-z0-9])",
        q,
        re.IGNORECASE,
    ):
        try:
            base = float(m.group(1).replace(",", ""))
            nums.append(base)
            suffix = (m.group(2) or "").lower()
            if suffix == "k":
                nums.append(base * 1_000)
            elif suffix == "m":
                nums.append(base * 1_000_000)
            elif suffix == "b":
                nums.append(base * 1_000_000_000)
            elif suffix == "t":
                nums.append(base * 1_000_000_000_000)
        except ValueError:
            continue
    if not nums:
        return False
    for n in nums:
        scale = max(1.0, abs(target), abs(n))
        if abs(n - target) <= 1e-9 * scale:
            return True
        if abs(n - abs(target)) <= 1e-9 * scale:
            return True
    return False


def _verify_citation(dp_raw: dict, raw_val: float, page_texts: dict[int, str]) -> bool:
    """Mechanical grounding check: the quote must exist verbatim in the source
    text AND contain the number. Catches hallucinated/misread values at the
    door — no LLM involved."""
    quote = str(dp_raw.get("quote", "") or "").strip()
    if len(quote) < 4:
        return False
    if not _value_in_quote(raw_val, quote):
        return False
    nq = _ws_norm(quote)
    try:
        page = int(dp_raw.get("page") or 0)
    except (TypeError, ValueError):
        page = 0
    # exact page first, then any page (page numbering can be off by one)
    if page in page_texts and nq in _ws_norm(page_texts[page]):
        return True
    return any(nq in _ws_norm(t) for t in page_texts.values())


async def _call_datapoint_claude(prompt: str) -> dict:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return {}
    from .openai_research import AsyncResearchClient
    client = AsyncResearchClient(api_key=api_key)
    try:
        # Streaming avoids 60s idle-connection kills on long generations
        async with client.messages.stream(
            model=_DATAPOINT_OPENAI_MODEL, max_tokens=8192, temperature=0,
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
            if m:
                return json.loads(m.group(0))
            return {}
    except Exception as exc:
        logger.warning("Datapoint extraction LLM call failed: %s", exc)
        return {}


_call_haiku = _call_datapoint_claude


def _write_fact_json(datapoints: list[dict], filename: str, broker: str, date: str) -> None:
    """Export datapoints as local Fact-schema JSON."""
    try:
        FACT_POOL_DIR.mkdir(parents=True, exist_ok=True)
        date_compact = (date or "").replace("-", "")
        facts = []
        for dp in datapoints:
            facts.append({
                "entity": "MEMORY",
                "metric": dp["metric"],
                "period": dp["period"],
                "value": dp["value"],
                "unit": dp["unit"],
                "source": {
                    "kind": "sellside_pdf",
                    "id": f"{broker} {date_compact}".strip(),
                    "date": date or None,
                    "full_filename": filename,
                },
                "confidence": 0.90,
                "extracted_at": datetime.now().isoformat(),
                "extractor": "memory_chart_datapoints.v1",
                "tags": ["memory_industry", dp["segment"].lower()],
            })
        out = FACT_POOL_DIR / f"{Path(filename).stem}.json"
        out.write_text(json.dumps({"facts": facts}, indent=1), encoding="utf-8")
    except Exception as exc:
        logger.warning("Fact JSON write failed for %s: %s", filename, exc)


OUTLIER_RATIO = 30.0  # cross-broker tolerance: same metric+year shouldn't differ >30x


def _median(vals: list[float]) -> float:
    s = sorted(vals)
    return s[len(s) // 2] if s else 0.0


async def _drop_cross_broker_outliers(dps: list[dict], filename: str) -> list[dict]:
    """Drop candidate datapoints that are >OUTLIER_RATIO away from what OTHER
    brokers report for the same metric in the same year.

    Catches unit-base misclassifications (e.g. bit supply in mn-8Gb units stored
    as kwpm wafer capacity = 100x error) without touching legitimate spread.
    Only enforced when at least 2 peer values exist.
    """
    db = await get_db()
    kept: list[dict] = []
    dropped = 0
    for dp in dps:
        year = dp["period_int"] // 10000
        cur = await db.execute(
            """SELECT value FROM datapoints
               WHERE metric = ? AND period_int / 10000 = ?
                 AND source_filename != ? AND value != 0""",
            (dp["metric"], year, filename),
        )
        peers = [abs(r["value"]) for r in await cur.fetchall() if r["value"]]
        if len(peers) >= 2 and dp["value"]:
            med = _median(peers)
            v = abs(dp["value"])
            if med > 0 and (v / med > OUTLIER_RATIO or med / v > OUTLIER_RATIO):
                logger.warning(
                    "Outlier dropped: %s %s %s=%s %s (peer median %.3g)",
                    filename, dp["metric"], dp["period"], dp["value"], dp["unit"], med,
                )
                dropped += 1
                continue
        kept.append(dp)
    if dropped:
        logger.info("Cross-broker validation dropped %d/%d datapoints from %s",
                    dropped, len(dps), filename)
    return kept


async def extract_and_store_datapoints(
    page_texts: dict[int, str],
    filename: str,
    broker: str = "",
    date: str = "",
) -> int:
    """Extract datapoints from PDF page texts and store them incrementally.

    Returns the number of NEW datapoints inserted (existing ones are ignored).
    """
    await ensure_datapoints_table()

    texts = [t for t in page_texts.values() if t and t.strip()]
    if not texts:
        return 0

    # Chunk pages into prompt-sized blocks (~30K chars per call)
    blocks: list[str] = []
    cur, cur_len = [], 0
    for pno in sorted(page_texts):
        t = page_texts[pno]
        if not t.strip():
            continue
        entry = f"[PAGE {pno}]\n{t}"
        if cur_len + len(entry) > 30000 and cur:
            blocks.append("\n\n".join(cur))
            cur, cur_len = [], 0
        cur.append(entry)
        cur_len += len(entry)
    if cur:
        blocks.append("\n\n".join(cur))

    all_dps: list[dict] = []
    uncited = 0
    for block in blocks:
        result = await _call_datapoint_claude(_build_extraction_prompt(block, broker, date))
        for dp in result.get("datapoints", []):
            try:
                metric = str(dp.get("metric", "")).strip()
                period = str(dp.get("period", "")).strip()
                raw_val = float(dp.get("value"))
                raw_unit = str(dp.get("unit", "")).strip()
                if metric not in CANONICAL_METRICS or not period:
                    continue
                # CITATION GATE: value must be quotable verbatim from the source
                if not _verify_citation(dp, raw_val, page_texts):
                    uncited += 1
                    continue
                # Segment is determined by the metric, not the LLM's guess
                segment = CANONICAL_METRICS[metric][1]
                value, unit = _normalize(metric, raw_val, raw_unit)
                pint = _period_int(period)
                if pint == 0:
                    continue
                all_dps.append({
                    "segment": segment, "metric": metric, "period": period,
                    "period_int": pint, "value": value, "unit": unit,
                    "raw_unit": raw_unit,
                    "source_page": int(dp.get("page") or 0),
                    "source_quote": str(dp.get("quote", ""))[:200],
                    "broker": dp.get("broker") or broker or "",
                })
            except (TypeError, ValueError):
                continue
    if uncited:
        logger.info("Citation gate rejected %d uncitable datapoints from %s",
                    uncited, filename)

    if not all_dps:
        logger.info("No datapoints extracted from %s", filename)
        return 0

    all_dps = await _drop_cross_broker_outliers(all_dps, filename)
    if not all_dps:
        return 0

    db = await get_db()
    now = datetime.now().isoformat()

    # Don't resurrect rows the unit arbiter deleted (INSERT OR IGNORE would
    # silently re-add them on every re-extraction)
    deleted_keys: set[str] = set()
    try:
        cur = await db.execute(
            "SELECT datapoint_key FROM unit_arbitrations WHERE action = 'delete'")
        deleted_keys = {r["datapoint_key"] for r in await cur.fetchall()}
    except Exception:
        pass

    inserted = 0
    for dp in all_dps:
        key = f"{dp['metric']}|{dp['period']}|{dp['broker']}|{filename}|{dp['value']}"
        if key in deleted_keys:
            continue
        cur = await db.execute(
            """INSERT OR IGNORE INTO datapoints
               (segment, metric, period, period_int, value, unit, raw_unit,
                source_page, source_quote, broker, source_filename, source_date, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (dp["segment"], dp["metric"], dp["period"], dp["period_int"],
             dp["value"], dp["unit"], dp.get("raw_unit", ""),
             dp.get("source_page", 0), dp.get("source_quote", ""), dp["broker"],
             filename, date or "", now),
        )
        inserted += cur.rowcount if cur.rowcount > 0 else 0
    await db.commit()

    _write_fact_json(all_dps, filename, broker, date)
    logger.info("Datapoints for %s: %d extracted, %d new", filename, len(all_dps), inserted)

    # LLM unit arbitration: any value >=2x off same-period peers gets reviewed —
    # belongs to this metric? wrong unit basis? -> convert (and learn the rule)
    if inserted:
        try:
            from .unit_arbiter import arbitrate_metric_outliers
            result = await arbitrate_metric_outliers({dp["metric"] for dp in all_dps})
            if result.get("converted") or result.get("deleted"):
                logger.info("Unit arbitration for %s: %s", filename, result)
        except Exception as exc:
            logger.warning("Unit arbitration failed (non-fatal): %s", exc)
    return inserted
