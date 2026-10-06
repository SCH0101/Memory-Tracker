"""Excel model ingestion -> dashboard datapoint terminal.

Pipeline:
  1. SHA256 the file; skip if already ingested.
  2. calamine via pandas reads cached workbook values from every relevant sheet.
  3. Detect a deterministic provider template and extract rows into the
     universal raw dashboard contract with source sheet/cell audit metadata.
  4. Insert into the strict dashboard raw/verified/quarantine terminal.
  5. Record the active broker Excel model in `excel_ingests` for audit / dedup.

The known-template path is the fast path (no LLM). Workbooks that do not match
a known template return `needs_template` and do not write charted data.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Iterable

from ..db.connection import get_db
from . import broker_from_filename, normalize_broker
from .datapoints import (
    CANONICAL_METRICS, _period_int,
)
from .dashboard_datapoints import (
    _date_key,
    count_quarantined_dashboard_rows,
    count_verified_dashboard_rows,
    delete_excel_dashboard_rows,
    rebuild_verified_datapoints,
    store_raw_dashboard_datapoints,
)
from .dashboard_cache import mark_cache_dirty_many, start_cache_rebuild_many

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Period parsing (JPM column headers use mixed conventions)
# ---------------------------------------------------------------------------

_QTR_RE  = re.compile(r"^\s*([1-4])\s*Q\s*(\d{2,4})\s*E?\s*$", re.IGNORECASE)
_YR_RE   = re.compile(r"^\s*(\d{4})\s*E?\s*$")
_MON_RE  = re.compile(r"^\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[-\s]?(\d{2,4})\s*$",
                      re.IGNORECASE)
_MON_MAP = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
            "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}


def _two_to_four_year(y: int) -> int:
    """JPM convention: 04 = 2004, 26 = 2026, 99 = 1999. Cutoff at 50."""
    if y >= 100:
        return y
    return 2000 + y if y < 50 else 1900 + y


def _parse_period(cell) -> str:
    """Normalize a JPM column header into the canonical period string used
    by datapoints (`"2026"`, `"2026Q2"`, `"2026-06"`). Returns "" if the
    cell isn't a recognizable period."""
    if cell is None:
        return ""
    if isinstance(cell, datetime):
        return f"{cell.year}-{cell.month:02d}"
    s = str(cell).strip()
    if not s:
        return ""
    m = _QTR_RE.match(s)
    if m:
        q, y = int(m.group(1)), _two_to_four_year(int(m.group(2)))
        return f"{y}Q{q}"
    m = _YR_RE.match(s)
    if m:
        return m.group(1)
    m = _MON_RE.match(s)
    if m:
        mo = _MON_MAP[m.group(1).lower()]
        y = _two_to_four_year(int(m.group(2)))
        return f"{y}-{mo:02d}"
    return ""


# ---------------------------------------------------------------------------
# JPM "Global Memory Model" — section → canonical metric map
# ---------------------------------------------------------------------------
#
# Each entry says: when scanning this sheet, if you see a row whose col-A
# value matches `section_a` AND a sub-row whose col-B value matches `label`
# below it (before the next section_a flips), emit a datapoint for the
# canonical metric tagged with the unit hint.
#
# Players (Samsung/SK Hynix/Micron/Kioxia/WDC) under share / capacity sub-
# sections are emitted as one metric per player automatically.

_PLAYER_TO_CANON = {
    # DRAM share
    ("dram", "share"): {
        "samsung":   "dram_share_samsung",
        "sk hynix":  "dram_share_skhynix",
        "skhynix":   "dram_share_skhynix",
        "micron":    "dram_share_micron",
        "others":    "dram_share_others",
    },
    ("nand", "share"): {
        "samsung":   "nand_share_samsung",
        "sk hynix":  "nand_share_skhynix",
        "kioxia":    "nand_share_kioxia",
        "wdc":       "nand_share_wdc",
        "sandisk":   "nand_share_wdc",
        "western digital": "nand_share_wdc",
        "micron":    "nand_share_micron",
        "ymtc":      "nand_share_ymtc",
        "solidigm":  "nand_share_skhynix",
        "intel":     "nand_share_others",
        "others":    "nand_share_others",
    },
    ("hbm", "share"): {
        "sk hynix":  "hbm_share_skhynix",
        "skhynix":   "hbm_share_skhynix",
        "samsung":   "hbm_share_samsung",
        "micron":    "hbm_share_micron",
    },
}


def _player_metric(segment: str, sub_kind: str, label: str) -> str | None:
    table = _PLAYER_TO_CANON.get((segment, sub_kind))
    if not table:
        return None
    norm = re.sub(r"\s+", " ", label.strip().lower())
    return table.get(norm)


def _metric_and_unit(label_spec, fallback_unit: str) -> tuple[str | None, str]:
    """Template labels may override the section unit for mixed-unit blocks."""
    if isinstance(label_spec, dict):
        return label_spec.get("metric"), label_spec.get("unit") or fallback_unit
    return label_spec, fallback_unit


def _capacity_total_metric(segment: str, label: str) -> str | None:
    norm = re.sub(r"\s+", " ", label.strip().lower())
    if norm not in {"total", "total capacity"}:
        return None
    return {
        "dram": "dram_wafer_capacity",
        "nand": "nand_wafer_capacity",
        "hbm": "hbm_wafer_capacity",
    }.get(segment.strip().lower())


# Per-sheet section walker spec. Format:
# {sheet_name: [
#   {"section_a": "<col A header start>",     # regex (re.I)
#    "kind": "metric|player_share|player_capacity",
#    "segment": "DRAM|NAND|HBM",
#    "unit": "<canonical raw unit>",
#    "labels": {<col-B label>: <canonical metric>}    # for kind=metric
#   }, ...
# ]}

_JPM_DRAM_MAIN: list[dict] = [
    # Headline supply/demand (rows 3-16)
    {"section_a": r"^DRAM Supply/Demand model",
     "kind": "metric", "segment": "DRAM", "unit": "mn 8Gb-eq",
     "labels": {
         "total demand (m units)": {"metric": "dram_bit_demand", "unit": "mn 8Gb-eq"},
         "total supply (m units)": {"metric": "dram_bit_supply", "unit": "mn 8Gb-eq"},
         # only used if total demand absent
         "consumption (wsts)":     {"metric": "dram_bit_demand", "unit": "mn 8Gb-eq"},
         "sufficiency (%)":        {"metric": "dram_sufficiency", "unit": "%"},
         "sufficiency ex inventory (%)": {"metric": "dram_sufficiency", "unit": "%"},
         "inventory weeks":        {"metric": "dram_inventory_weeks", "unit": "weeks"},
         "dram revenues (us$, m)": {"metric": "memory_revenue", "unit": "$mn"},
         "dram asp ($)":           {"metric": "dram_contract_asp", "unit": "$/Gb"},
     }},
    # Per-player supply (mn 8Gb)
    {"section_a": r"^DRAM Supply \(8Gb\. Eq\., M\)",
     "kind": "player_supply", "segment": "dram", "unit": "mn 8Gb-eq"},
    # Per-player market share
    {"section_a": r"^DRAM supply - bit share \(%\)",
     "kind": "player_share", "segment": "dram", "unit": "%"},
    # Per-player wafer capacity
    {"section_a": r"^DRAM supply - fab capacity",
     "kind": "player_capacity", "segment": "dram", "unit": "K wspm"},
    # Y/Y growth (under bit-demand top section already; emit explicitly)
]

_JPM_NAND_MAIN: list[dict] = [
    {"section_a": r"^NAND Supply/Demand model",
     "kind": "metric", "segment": "NAND", "unit": "EB",
     "labels": {
         "total demand (m units)": {"metric": "nand_bit_demand", "unit": "EB"},
         "total supply (m units)": {"metric": "nand_bit_supply", "unit": "EB"},
         "sufficiency (%)":        {"metric": "nand_sufficiency", "unit": "%"},
         "inventory weeks":        {"metric": "nand_inventory_weeks", "unit": "weeks"},
         "nand revenues (us$, m)": {"metric": "memory_revenue", "unit": "$mn"},
         "nand asp ($)":           {"metric": "nand_contract_asp", "unit": "$/GB"},
     }},
    {"section_a": r"^NAND Supply \(",
     "kind": "player_supply", "segment": "nand", "unit": "EB"},
    {"section_a": r"^NAND supply - bit share \(%\)",
     "kind": "player_share", "segment": "nand", "unit": "%"},
    {"section_a": r"^NAND (supply - fab capacity|capacity)",
     "kind": "player_capacity", "segment": "nand", "unit": "K wspm"},
]

# HBM demand sheet exists in both workbooks; columns are annual.
_JPM_HBM_DEMAND: list[dict] = [
    {"section_a": r"^HBM (demand|TAM)",
     "kind": "metric", "segment": "HBM", "unit": "$bn",
     "labels": {
         "hbm tam ($, bn)":     {"metric": "hbm_tam", "unit": "$bn"},
         "hbm tam ($bn)":       {"metric": "hbm_tam", "unit": "$bn"},
         "total hbm tam":       {"metric": "hbm_tam", "unit": "$bn"},
         "hbm demand (m units)": {"metric": "hbm_bit_demand", "unit": "mn"},
         "total hbm demand":    {"metric": "hbm_bit_demand", "unit": "mn"},
         "hbm content per gpu": {"metric": "hbm_content_per_accelerator", "unit": "GB"},
         "hbm gb per gpu":      {"metric": "hbm_content_per_accelerator", "unit": "GB"},
     }},
]

# HBM client model: HBM S-D sheet has supply/demand by player; share by gen.
_JPM_HBM_SD: list[dict] = [
    {"section_a": r"^HBM (supply|share|S/D|S-D)",
     "kind": "player_share", "segment": "hbm", "unit": "%"},
]

# Col-B-style template: used for the "HBM client model" workbook where col-A
# is empty and both group headers and player rows live in col-B. The walker
# emits a datapoint for a group header itself (kind="metric") AND for each
# subsequent player row matching `players` until another group header fires.
_HBM_CLIENT_COMPARISON: list[dict] = [
    {"col": "B", "headers": {
        # The first occurrence of "HBM revenue (US$ mn)" is the industry
        # total — emit as hbm_tam ($bn after conversion).
        r"^HBM revenue \(US\$ mn\)$": {
            "metric": "hbm_tam", "segment": "HBM", "unit": "$mn",
            "players": None,
        },
    }, "self_metric": {
        r"^HBM capacity \(wspm\)$": {
            "metric": "hbm_wafer_capacity", "segment": "HBM", "unit": "K wspm",
        },
    }},
]

_HBM_CLIENT_SD: list[dict] = [
    {"col": "B", "self_metric": {
        r"^HBM bit demand$":            {"metric": "hbm_bit_demand", "segment": "HBM", "unit": "mn GB"},
        r"^HBM ASP \(\$/bit\)$":        {"metric": "hbm_asp",        "segment": "HBM", "unit": "$/Gb"},
        # bit supply isn't a canonical metric — skip for now.
    }},
]

_TEMPLATES: dict[str, list[dict]] = {
    "DRAM Main": _JPM_DRAM_MAIN,
    "NAND Main": _JPM_NAND_MAIN,
    "HBM demand": _JPM_HBM_DEMAND,
    # Col-B style sheets — different walker codepath in _walk_sheet
    "Comparison":    _HBM_CLIENT_COMPARISON,
    "HBM S-D":       _HBM_CLIENT_SD,
    "HBM S-D by Gen": _HBM_CLIENT_SD,
}

_PROVIDER_TEMPLATES: dict[str, set[str]] = {
    "jpm_global_memory_model_v1": {"DRAM Main", "NAND Main", "HBM demand"},
    "jpm_hbm_client_model_v1": {"HBM S-D", "Comparison", "HBM S-D by Gen"},
}


def _detect_template(sheet_names: list[str]) -> str:
    sheets = set(sheet_names)
    if {"DRAM Main", "NAND Main"}.issubset(sheets):
        return "jpm_global_memory_model_v1"
    if {"HBM S-D", "Comparison"}.issubset(sheets):
        return "jpm_hbm_client_model_v1"
    return "unknown"


def _template_sheet_names(template: str) -> set[str]:
    return _PROVIDER_TEMPLATES.get(template, set())


def _coverage(rows: list[dict], sheets_done: list[str]) -> dict:
    by_metric: dict[str, int] = {}
    by_sheet: dict[str, int] = {}
    periods: set[str] = set()
    for r in rows:
        by_metric[r["metric"]] = by_metric.get(r["metric"], 0) + 1
        by_sheet[r["source_sheet"]] = by_sheet.get(r["source_sheet"], 0) + 1
        periods.add(r["period"])
    return {
        "metrics": by_metric,
        "sheets": by_sheet,
        "sheets_processed": sheets_done,
        "periods": sorted(periods),
    }


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _find_period_row(df) -> tuple[int, list[tuple[int, str]]]:
    """Scan the first 12 rows for the one that has the most parseable periods
    (1Q04-style or 2024-style). Returns (row_index, [(col_index, period)])."""
    best = (0, [])
    for r in range(min(12, df.shape[0])):
        cols = []
        for c in range(df.shape[1]):
            p = _parse_period(df.iat[r, c])
            if p:
                cols.append((c, p))
        if len(cols) > len(best[1]):
            best = (r, cols)
    return best


def _label_at(df, row: int, col: int) -> str:
    v = df.iat[row, col]
    if v is None or (isinstance(v, float) and v != v):  # NaN
        return ""
    return str(v).strip()


def _excel_col(n: int) -> str:
    """Zero-based column index -> Excel column label."""
    out = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def _emit_row(canon: str, raw_unit: str, df, row: int, period_cols: list[tuple[int, str]],
              broker: str, source_filename: str, ingest_dt: str, sheet: str,
              row_label: str = "") -> list[dict]:
    """Emit one datapoint per (period column) for a single label row."""
    if canon not in CANONICAL_METRICS:
        return []
    canon_unit, segment_hint = CANONICAL_METRICS[canon]
    model_date = _date_key(source_filename)
    source_date = model_date or ingest_dt[:10]
    out = []
    for (c, period) in period_cols:
        v = df.iat[row, c]
        if v is None:
            continue
        try:
            val = float(v)
        except (TypeError, ValueError):
            continue
        if val != val:  # NaN
            continue
        if raw_unit == "%" and -1.5 < val < 1.5:
            val *= 100.0
        cell = f"{_excel_col(c)}{row + 1}"
        out.append({
            "segment":  segment_hint, "metric": canon, "period": period,
            "period_int": _period_int(period), "raw_value": round(val, 6),
            "raw_unit": raw_unit, "broker": broker,
            "source_broker": broker, "source_type": "excel",
            "source_filename": source_filename, "source_date": source_date,
            "model_date": model_date,
            "source_quote": f"{sheet}!{cell} {row_label or canon}".strip(),
            "source_sheet": sheet, "source_cell": cell, "source_page": 0,
        })
    return out


def _walk_sheet_col_b(df, sheet: str, broker: str, source_filename: str,
                     ingest_dt: str, period_cols: list[tuple[int, str]],
                     period_row: int, spec_entry: dict) -> list[dict]:
    """Walker for col-B-only layouts (HBM client model). Two flavors per
    spec entry:
      - headers:      pattern -> {metric, segment, unit, players: dict|None}
                      emits the header row itself, then any subsequent player
                      row whose B-cell matches a known player gets emitted as
                      the corresponding per-player metric.
      - self_metric:  pattern -> {metric, segment, unit}
                      emits the matching row directly; no player context.
    """
    out: list[dict] = []
    headers = spec_entry.get("headers") or {}
    self_metric = spec_entry.get("self_metric") or {}
    active_players: dict | None = None  # {label_lower: metric_name}
    for r in range(period_row + 1, df.shape[0]):
        b_raw = _label_at(df, r, 1)
        if not b_raw:
            active_players = None  # blank row resets context
            continue
        b_norm = re.sub(r"\s+", " ", b_raw.strip())
        # self_metric: row IS the metric (no player context)
        for pat, spec_v in self_metric.items():
            if re.match(pat, b_norm, re.I):
                out.extend(_emit_row(spec_v["metric"], spec_v["unit"], df, r,
                                     period_cols, broker, source_filename,
                                     ingest_dt, sheet, b_norm))
        # headers: row is a group header → emit itself + arm player lookup
        matched_header = False
        for pat, spec_v in headers.items():
            if re.match(pat, b_norm, re.I):
                matched_header = True
                out.extend(_emit_row(spec_v["metric"], spec_v["unit"], df, r,
                                     period_cols, broker, source_filename,
                                     ingest_dt, sheet, b_norm))
                active_players = spec_v.get("players")
                break
        if matched_header:
            continue
        # Player row under an active header
        if active_players:
            key = b_norm.lower()
            metric = active_players.get(key)
            if metric:
                out.extend(_emit_row(metric, headers[pat]["unit"], df, r,
                                     period_cols, broker, source_filename,
                                     ingest_dt, sheet, b_norm))
    return out


def _walk_sheet(df, sheet: str, broker: str, source_filename: str,
                ingest_dt: str) -> list[dict]:
    """Extract datapoints from one sheet using the templates registered for
    that sheet name. Returns a list of dicts ready for INSERT."""
    spec = _TEMPLATES.get(sheet)
    if not spec:
        return []
    period_row, period_cols = _find_period_row(df)
    if not period_cols:
        return []

    # Col-B-style template (HBM client model) — different walker.
    if spec and spec[0].get("col") == "B":
        out: list[dict] = []
        for entry in spec:
            out.extend(_walk_sheet_col_b(df, sheet, broker, source_filename,
                                         ingest_dt, period_cols, period_row,
                                         entry))
        return out

    out: list[dict] = []
    # Walk from row 0 — section headers usually sit ABOVE the period row, so
    # iterating from row 0 catches them. Skip row emission for r <= period_row
    # so we never read the period header itself as data.
    active = None
    for r in range(df.shape[0]):
        a_raw = _label_at(df, r, 0)
        b_raw = _label_at(df, r, 1)
        # Section flip?
        for section in spec:
            if a_raw and re.match(section["section_a"], a_raw, re.I):
                active = section
                break
        if r <= period_row:
            continue
        if active is None:
            continue
        # Row matches a known canonical label?
        if not b_raw:
            continue
        b_norm = re.sub(r"\s+", " ", b_raw.strip().lower())
        canon: str | None = None
        raw_unit = active.get("unit", "")
        # 1. Direct label mapping (top-line metrics)
        if active.get("kind") == "metric":
            canon, raw_unit = _metric_and_unit(
                (active.get("labels") or {}).get(b_norm),
                raw_unit,
            )
        # 2. Player-share / player-capacity / player-supply rows
        elif active["kind"] in ("player_share", "player_capacity", "player_supply"):
            seg = active["segment"]
            if active["kind"] == "player_capacity":
                canon = _capacity_total_metric(seg, b_norm)
            if not canon:
                sub_kind = "share" if "share" in active["kind"] else "capacity"
                canon = _player_metric(seg, sub_kind, b_norm)
        if not canon or canon not in CANONICAL_METRICS:
            continue
        canon_unit, segment_hint = CANONICAL_METRICS[canon]
        for (c, period) in period_cols:
            v = df.iat[r, c]
            if v is None:
                continue
            try:
                val = float(v)
            except (TypeError, ValueError):
                continue
            if val != val:  # NaN
                continue
            # JPM stores ratios as decimals (0.234 not 23.4); detect via unit.
            if raw_unit == "%" and -1.5 < val < 1.5:
                val *= 100.0
            cell = f"{_excel_col(c)}{r + 1}"
            out.append({
                "segment":  segment_hint,
                "metric":   canon,
                "period":   period,
                "period_int": _period_int(period),
                "raw_value": round(val, 6),
                "raw_unit": raw_unit,
                "broker":   broker,
                "source_broker": broker,
                "source_type": "excel",
                "source_filename": source_filename,
                "source_date": _date_key(source_filename) or ingest_dt[:10],
                "model_date": _date_key(source_filename),
                "source_quote": f"{sheet}!{cell} {b_raw}",
                "source_sheet": sheet,
                "source_cell": cell,
                "source_page": 0,
            })
    return out


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------

async def ensure_excel_ingests_table() -> None:
    db = await get_db()
    await db.execute(
        """CREATE TABLE IF NOT EXISTS excel_ingests (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               file_sha256 TEXT NOT NULL UNIQUE,
               source_filename TEXT NOT NULL,
               broker TEXT NOT NULL DEFAULT '',
               template TEXT NOT NULL DEFAULT '',
               status TEXT NOT NULL DEFAULT 'ingested',
               active INTEGER NOT NULL DEFAULT 0,
               coverage_json TEXT NOT NULL DEFAULT '{}',
               uploaded_at TEXT NOT NULL DEFAULT '',
               ingested_at TEXT NOT NULL,
               rows_added INTEGER NOT NULL DEFAULT 0,
               raw_rows INTEGER NOT NULL DEFAULT 0,
               verified_rows INTEGER NOT NULL DEFAULT 0,
               quarantined_rows INTEGER NOT NULL DEFAULT 0,
               warnings_json TEXT NOT NULL DEFAULT '[]',
               sheets_processed TEXT NOT NULL DEFAULT ''
           )"""
    )
    for ddl in (
        "template TEXT NOT NULL DEFAULT ''",
        "status TEXT NOT NULL DEFAULT 'ingested'",
        "active INTEGER NOT NULL DEFAULT 0",
        "coverage_json TEXT NOT NULL DEFAULT '{}'",
        "uploaded_at TEXT NOT NULL DEFAULT ''",
        "raw_rows INTEGER NOT NULL DEFAULT 0",
        "verified_rows INTEGER NOT NULL DEFAULT 0",
        "quarantined_rows INTEGER NOT NULL DEFAULT 0",
        "warnings_json TEXT NOT NULL DEFAULT '[]'",
    ):
        try:
            await db.execute(f"ALTER TABLE excel_ingests ADD COLUMN {ddl}")
        except Exception:
            pass
    try:
        rows = await db.execute_fetchall("SELECT id, broker FROM excel_ingests")
        for r in rows:
            normalized = normalize_broker(r["broker"] or "")
            if normalized and normalized != (r["broker"] or ""):
                await db.execute(
                    "UPDATE excel_ingests SET broker = ? WHERE id = ?",
                    (normalized, r["id"]),
                )
    except Exception:
        pass
    try:
        await db.execute(
            """UPDATE excel_ingests
               SET status = 'ingested'
               WHERE (status = '' OR status IS NULL) AND rows_added > 0"""
        )
        await db.execute(
            """UPDATE excel_ingests
               SET uploaded_at = ingested_at
               WHERE uploaded_at = '' OR uploaded_at IS NULL"""
        )
        await db.execute(
            """UPDATE excel_ingests
               SET template = 'jpm_global_memory_model_v1'
               WHERE (template = '' OR template IS NULL)
                 AND sheets_processed LIKE '%DRAM Main%'
                 AND sheets_processed LIKE '%NAND Main%'"""
        )
        await db.execute(
            """UPDATE excel_ingests
               SET template = 'jpm_hbm_client_model_v1'
               WHERE (template = '' OR template IS NULL)
                 AND sheets_processed LIKE '%HBM S-D%'
                 AND sheets_processed LIKE '%Comparison%'"""
        )
        await db.execute("UPDATE excel_ingests SET active = 0")
        await db.execute(
            """UPDATE excel_ingests
               SET active = 1
               WHERE id IN (
                 SELECT MAX(id)
                 FROM excel_ingests
                 WHERE status = 'ingested' AND broker != '' AND rows_added > 0
                 GROUP BY lower(broker), template
               )"""
        )
    except Exception:
        pass
    await db.commit()


async def ingest_excel(path: str | Path, *, broker: str | None = None,
                       force: bool = False) -> dict:
    """End-to-end ingest. Returns a summary dict.

    If `force=True`, re-runs even if the file SHA256 was already ingested
    (used by the "re-process" admin button)."""
    import pandas as pd  # local import — pandas is heavy
    p = Path(path)
    if not p.exists():
        return {"ok": False, "error": f"file not found: {p}"}
    await ensure_excel_ingests_table()
    fhash = _sha256_file(p)
    broker = normalize_broker(broker or broker_from_filename(p.name))
    if not broker:
        return {
            "ok": False,
            "status": "needs_broker",
            "needs_broker": True,
            "file_sha256": fhash,
            "filename": p.name,
            "message": "Broker name required before ingesting this Excel file.",
        }
    db = await get_db()
    cur = await db.execute(
        """SELECT id, rows_added, status, template, raw_rows, verified_rows,
                  quarantined_rows, broker, active
           FROM excel_ingests WHERE file_sha256 = ?""",
        (fhash,),
    )
    prior = await cur.fetchone()
    if prior and not force:
        prior_broker = normalize_broker(prior["broker"] or "")
        if prior_broker and prior_broker != broker:
            return {
                "ok": False,
                "status": "broker_conflict",
                "skipped": True,
                "file_sha256": fhash,
                "broker": broker,
                "prior_broker": prior_broker,
                "message": f"same workbook already cached for {prior_broker}",
            }
        return {"ok": True, "skipped": True, "status": prior["status"],
                "template": prior["template"], "file_sha256": fhash,
                "broker": prior_broker or broker, "active": bool(prior["active"]),
                "rows_added": prior["rows_added"],
                "raw_rows": prior["raw_rows"],
                "verified_rows": prior["verified_rows"],
                "quarantined_rows": prior["quarantined_rows"],
                "message": "already ingested (SHA256 match)"}
    try:
        xl = pd.ExcelFile(p, engine="calamine")
    except Exception as exc:
        ingest_dt = datetime.utcnow().isoformat()
        await db.execute(
            """INSERT OR REPLACE INTO excel_ingests
               (file_sha256, source_filename, broker, template, status, active,
                coverage_json, uploaded_at, ingested_at, rows_added, raw_rows,
                verified_rows, quarantined_rows, warnings_json, sheets_processed)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fhash, p.name, broker, "", "failed", 0,
                json.dumps({"message": "Workbook could not be opened."}),
                ingest_dt, ingest_dt, 0, 0, 0, 0,
                json.dumps([str(exc)[:500]]), "",
            ),
        )
        await db.commit()
        return {
            "ok": False,
            "status": "failed",
            "file_sha256": fhash,
            "broker": broker,
            "filename": p.name,
            "rows_added": 0,
            "raw_rows": 0,
            "verified_rows": 0,
            "quarantined_rows": 0,
            "warnings": [str(exc)[:500]],
            "message": "Workbook could not be opened.",
        }
    template = _detect_template(xl.sheet_names)
    ingest_dt = datetime.utcnow().isoformat()
    uploaded_at = ingest_dt

    if template == "unknown":
        coverage = {
            "sheets": xl.sheet_names,
            "anchors": {},
            "message": "No deterministic provider template matched this workbook.",
        }
        await db.execute(
            """INSERT OR REPLACE INTO excel_ingests
               (file_sha256, source_filename, broker, template, status, active,
                coverage_json, uploaded_at, ingested_at, rows_added, raw_rows,
                verified_rows, quarantined_rows, warnings_json, sheets_processed)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fhash, p.name, broker, template, "needs_template", 0,
                json.dumps(coverage), uploaded_at, ingest_dt, 0, 0, 0, 0,
                json.dumps(["No deterministic template matched; no dashboard rows written."]),
                "",
            ),
        )
        await db.commit()
        return {
            "ok": True,
            "status": "needs_template",
            "template": template,
            "skipped": False,
            "file_sha256": fhash,
            "broker": broker,
            "filename": p.name,
            "rows_added": 0,
            "raw_rows": 0,
            "verified_rows": 0,
            "quarantined_rows": 0,
            "sheets_processed": [],
            "warnings": ["No deterministic template matched; no dashboard rows written."],
            "coverage": coverage,
        }

    all_rows: list[dict] = []
    sheets_done: list[str] = []
    allowed_sheets = _template_sheet_names(template)
    for sheet in xl.sheet_names:
        if sheet not in allowed_sheets or sheet not in _TEMPLATES:
            continue
        try:
            df = pd.read_excel(p, sheet_name=sheet, engine="calamine",
                               header=None)
        except Exception as exc:
            logger.warning("Read %s failed: %s", sheet, exc)
            continue
        rows = _walk_sheet(df, sheet, broker, p.name, ingest_dt)
        if rows:
            all_rows.extend(rows)
            sheets_done.append(f"{sheet}({len(rows)})")

    coverage = _coverage(all_rows, sheets_done)
    warnings: list[str] = []
    if not all_rows:
        warnings.append("Template matched, but no mapped rows were extracted.")

    # Re-uploading the same source file should replace its prior Excel rows.
    await delete_excel_dashboard_rows(broker, source_filename=p.name)
    raw_inserted = 0
    validation = {"verified": 0, "quarantined": 0, "needs_review": 0}
    if all_rows:
        raw_inserted = await store_raw_dashboard_datapoints(
            all_rows,
            extractor=f"excel:{template}",
        )
        validation = await rebuild_verified_datapoints({r["metric"] for r in all_rows})

    verified_rows = await count_verified_dashboard_rows(broker, p.name)
    quarantined_rows = await count_quarantined_dashboard_rows(broker, p.name)
    status = "ingested" if verified_rows > 0 else "failed"
    active = 1 if status == "ingested" else 0
    if status != "ingested":
        warnings.append("No rows from this workbook passed dashboard validation.")

    if active:
        old_same_template = await db.execute_fetchall(
            """SELECT DISTINCT source_filename
               FROM excel_ingests
               WHERE broker = ?
                 AND template = ?
                 AND source_filename != ?""",
            (broker, template, p.name),
        )
        await db.execute(
            """UPDATE excel_ingests
               SET active = 0
               WHERE broker = ?
                 AND template = ?
                 AND file_sha256 != ?""",
            (broker, template, fhash),
        )
        for old in old_same_template:
            old_filename = old["source_filename"] or ""
            if old_filename:
                await delete_excel_dashboard_rows(broker, source_filename=old_filename)
        await delete_excel_dashboard_rows(
            broker,
            exclude_source_filename=p.name,
            extractor=f"excel:{template}",
        )
        validation = await rebuild_verified_datapoints({r["metric"] for r in all_rows})
        quarantined_rows = await count_quarantined_dashboard_rows(broker, p.name)

    # Audit row (REPLACE on hash so force re-runs update the count)
    await db.execute(
        """INSERT OR REPLACE INTO excel_ingests
           (file_sha256, source_filename, broker, template, status, active,
            coverage_json, uploaded_at, ingested_at, rows_added, raw_rows,
            verified_rows, quarantined_rows, warnings_json, sheets_processed)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            fhash, p.name, broker, template, status, active,
            json.dumps(coverage), uploaded_at, ingest_dt, raw_inserted,
            raw_inserted, verified_rows, quarantined_rows,
            json.dumps(warnings), ", ".join(sheets_done),
        ),
    )
    await db.commit()
    cache_jobs: list[dict] = []
    if status == "ingested":
        try:
            await mark_cache_dirty_many(("yearly", "quarterly"), f"Excel ingest {p.name}")
            cache_jobs = await start_cache_rebuild_many(("yearly", "quarterly"))
        except Exception as exc:
            logger.warning("Dashboard cache rebuild enqueue failed after Excel ingest: %s", exc)
    return {
        "ok": True, "skipped": False, "status": status,
        "template": template, "active": bool(active), "file_sha256": fhash,
        "broker": broker, "filename": p.name,
        "rows_added": raw_inserted,
        "raw_rows": raw_inserted,
        "verified_rows": verified_rows,
        "quarantined_rows": quarantined_rows,
        "sheets_processed": sheets_done,
        "warnings": warnings,
        "cache_jobs": cache_jobs,
        "coverage": coverage,
    }


async def list_ingests() -> list[dict]:
    await ensure_excel_ingests_table()
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT id, file_sha256, source_filename, broker, ingested_at,
                  template, status, active, coverage_json, uploaded_at,
                  rows_added, raw_rows, verified_rows, quarantined_rows,
                  warnings_json, sheets_processed
           FROM excel_ingests
           ORDER BY ingested_at DESC LIMIT 50""")
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        d["active"] = bool(d.get("active"))
        try:
            d["coverage"] = json.loads(d.get("coverage_json") or "{}")
        except Exception:
            d["coverage"] = {}
        try:
            d["warnings"] = json.loads(d.get("warnings_json") or "[]")
        except Exception:
            d["warnings"] = []
        out.append(d)
    return out
