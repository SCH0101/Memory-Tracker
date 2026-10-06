"""Per-chart × per-broker RAG-driven dashboard refresh.

Loops every Dashboard chart × every known broker, runs a targeted retrieval
against the shared fact pool (text + tables, no chart captions per project
rule), and asks Sonnet to extract the LATEST estimate/forecast that broker
made for that metric. Each extracted datapoint carries provenance — verbatim
source quote + filename + page — so the hover-tooltip on the dashboard
points to the exact sentence behind every number.

Cost & concurrency:
  ~30 metrics × ~10 brokers = ~300 (RAG retrieve + Sonnet extract) cells per
  full refresh. Bounded by:
    - GPU side: rerank serialized to 1 slot via the sidecar (cheap, ~50ms/call)
    - OpenAI side: capped to 3 concurrent Sonnet calls (rate-limit safety)
  Net wall-clock: ~60-120s; in-process cache reused for re-runs within 10 min.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ..db.connection import get_db
from . import broker_from_filename, normalize_broker
from .datapoints import CANONICAL_METRICS, _period_int, _value_in_quote
from .dashboard_datapoints import (
    ensure_dashboard_datapoint_tables,
    get_active_excel_brokers,
    rebuild_verified_datapoints,
    store_raw_dashboard_datapoints,
    _normalize_strict,
)
from .dashboard_cache import mark_cache_dirty, start_cache_rebuild
from .es_rag_gateway import EsRagError, get_es_rag_gateway
from .fact_pool import (
    _payload_list,
    _to_row,
    MEMORY_TICKER,
    MEMORY_NICHE_SECTOR,
    NARRATIVE_CONTENT,
    NUMERIC_CONTENT,
)  # type: ignore
from .rag_settings import (
    get_tab_sources,
    get_tab_prompt,
    render_prompt_template,
)

logger = logging.getLogger(__name__)

_DASHBOARD_OPENAI_MODEL = (
    os.getenv("DASHBOARD_OPENAI_MODEL")
    or os.getenv("OPENAI_REPORT_MODEL")
    or "gpt-6-sol"
)

# Concurrent dashboard OpenAI call ceiling keeps us under OpenAI's
# per-minute rate limit and lets the GPU rerank serialize.
_DASHBOARD_OPENAI_CONCURRENCY = int(os.getenv("DASHBOARD_OPENAI_CONCURRENCY", "3") or "3")
_DASHBOARD_ALLOWED_SOURCES = ("user_input", "broker_research", "industry_report", "tables")
_PDF_SOURCE_FAMILIES = ("broker_research", "industry_report")
_DOCUMENT_FRESHNESS_DAYS = 20
_DOCUMENT_BATCH_CHAR_BUDGET = 18000
_DOCUMENT_CHUNK_CHAR_LIMIT = 1300
_DOCUMENT_MAX_CHUNKS_PER_BATCH = 18
_MAX_AUDIT_STALE_DOCUMENTS = 120
_DASHBOARD_OPENAI_TIMEOUT_SEC = int(
    os.getenv("DASHBOARD_OPENAI_TIMEOUT_SEC")
    or os.getenv("DASHBOARD_HAIKU_TIMEOUT_SEC")
    or "120"
)
_ESRAG_SOURCES_LIMIT = int(os.getenv("DASHBOARD_ESRAG_SOURCES_LIMIT", "30") or "30")
_ESRAG_CHUNKS_PAGE_SIZE = int(os.getenv("DASHBOARD_ESRAG_CHUNKS_PAGE_SIZE", "500") or "500")
_MAX_PDF_FALLBACKS_PER_BROKER = 2
_DOCUMENT_EXTRACT_TIMEOUT_SEC = int(os.getenv("DASHBOARD_DOCUMENT_EXTRACT_TIMEOUT_SEC", "180") or "180")

# Brokers we ask each chart about. Order doesn't matter; missing brokers
# yield empty results which are skipped silently.
KNOWN_BROKERS: list[str] = [
    "GS", "JPM", "MS", "UBS", "BofA", "Citi", "Barclays", "DB",
    "Jefferies", "HSBC", "CLSA", "Nomura", "Daiwa", "Mizuho", "CICC",
    "Bernstein", "Arete", "TrendForce",
]


@dataclass(frozen=True)
class DocumentRef:
    broker: str
    filename: str
    source_date: str
    source_date_int: int
    source_type: str
    doc_type: str
    source_doc_type: str
    source_rel_path: str = ""
    title: str = ""
    chunk_count: int = 0

# Per-metric retrieval query — what we ask the RAG to surface. Tuned to
# bias the dense ANN toward analyst forecast tables.
_METRIC_QUERIES: dict[str, str] = {
    "dram_bit_demand": "DRAM bit demand forecast bn Gb annual growth",
    "dram_bit_supply": "DRAM bit supply forecast bn Gb annual",
    "dram_bit_growth": "DRAM bit growth YoY percent forecast",
    "dram_sufficiency": "DRAM sufficiency supply demand balance ratio percent",
    "dram_contract_asp": "DRAM contract ASP $/Gb forecast pricing",
    "dram_spot_asp": "DRAM spot price ASP $/unit forecast",
    "dram_inventory_weeks": "DRAM inventory weeks DIO supplier customer",
    "dram_wafer_capacity": "DRAM wafer capacity K WSPM kwpm wafer starts forecast",
    "dram_capex": "DRAM capex $bn investment spending",
    "dram_utilization": "DRAM fab utilization rate percent",
    "ddr4_contract_price": "DDR4 contract price $/unit forecast",
    "ddr5_contract_price": "DDR5 contract price $/unit forecast",
    "ddr4_spot_price": "DDR4 spot price $/unit",
    "ddr5_spot_price": "DDR5 spot price $/unit",
    "nand_bit_demand": "NAND bit demand forecast EB exabyte annual",
    "nand_bit_supply": "NAND bit supply forecast EB exabyte annual",
    "nand_bit_growth": "NAND bit growth YoY percent forecast",
    "nand_sufficiency": "NAND sufficiency supply demand balance ratio percent",
    "nand_contract_asp": "NAND contract ASP $/GB pricing forecast",
    "nand_spot_asp": "NAND spot price ASP $/unit",
    "nand_wafer_price": "NAND wafer price $/wafer",
    "nand_inventory_weeks": "NAND inventory weeks DIO",
    "nand_wafer_capacity": "NAND wafer capacity K WSPM kwpm wafer starts forecast",
    "nand_capex": "NAND capex $bn investment spending",
    "nand_utilization": "NAND fab utilization rate percent",
    "hbm_tam": "HBM TAM total addressable market $bn forecast",
    "hbm_bit_demand": "HBM bit demand GB mn forecast",
    "hbm_content_per_accelerator": "HBM GB per GPU accelerator content",
    "hbm_wafer_capacity": "HBM wafer capacity K WSPM kwpm forecast",
    "hbm_asp": "HBM ASP $/Gb price forecast",
    "memory_capex": "memory capex total $bn forecast",
    "memory_revenue": "memory revenue $bn forecast industry",
    "cloud_capex": "cloud hyperscaler capex $bn forecast",
    "server_dram_share": "server DRAM share percent forecast end-market",
    # Market share by producer — queries biased toward broker market-share tables.
    "dram_share_samsung":  "Samsung DRAM market share percent revenue bit share supplier ranking",
    "dram_share_skhynix":  "SK Hynix DRAM market share percent revenue bit share supplier ranking",
    "dram_share_micron":   "Micron DRAM market share percent revenue bit share supplier ranking",
    "dram_share_others":   "DRAM others Nanya Winbond CXMT market share percent",
    "nand_share_samsung":  "Samsung NAND market share percent revenue bit share supplier ranking",
    "nand_share_skhynix":  "SK Hynix Solidigm NAND market share percent revenue bit share",
    "nand_share_kioxia":   "Kioxia NAND market share percent revenue bit share supplier ranking",
    "nand_share_wdc":      "Western Digital SanDisk NAND market share percent revenue bit share",
    "nand_share_micron":   "Micron NAND market share percent revenue bit share supplier ranking",
    "nand_share_ymtc":     "YMTC NAND market share percent revenue bit share China",
    "nand_share_others":   "NAND others Intel market share percent",
    "hbm_share_skhynix":   "SK Hynix HBM market share percent revenue bit share supplier ranking",
    "hbm_share_samsung":   "Samsung HBM market share percent revenue bit share supplier ranking",
    "hbm_share_micron":    "Micron HBM market share percent revenue bit share supplier ranking",
}


def _dashboard_source_options() -> list[str]:
    """Dashboard refresh only uses source families approved for charted values."""
    selected = get_tab_sources("dashboard")
    out = [s for s in selected if s in _DASHBOARD_ALLOWED_SOURCES]
    return out or list(_DASHBOARD_ALLOWED_SOURCES)


def _chunk_source_type(chunk: dict) -> str:
    meta = chunk.get("metadata") or {}
    doc_type = str(chunk.get("doc_type") or meta.get("doc_type") or "").strip().lower()
    source_doc_type = str(chunk.get("source_doc_type") or meta.get("source_doc_type") or "").strip().lower()
    media_type = str(chunk.get("media_type") or meta.get("media_type") or "").strip().lower()
    content_type = str(chunk.get("content_type") or meta.get("content_type") or "").strip().lower()
    if media_type == "table" or content_type in {"table", "estimate_table", "valuation_exhibit", "chart_values"}:
        return "tables"
    if doc_type == "user_input" or source_doc_type == "user_input":
        return "user_input"
    if doc_type == "industry_report" or source_doc_type == "industry_report":
        return "industry_report"
    if doc_type == "sellside" or source_doc_type in {"broker_research", "broker_research_note"}:
        return "broker_research"
    return "broker_research"


def _chunk_meta_value(chunk: dict, key: str, default: str = ""):
    meta = chunk.get("metadata") or {}
    return chunk.get(key) or meta.get(key) or default


def _document_source_options() -> tuple[list[str], bool, bool]:
    """Return (allowed source families, include tables, include narrative).

    In the document-first flow, `tables` controls numeric/table chunks inside
    an allowed broker/industry document; it is not treated as its own provider.
    If the user selects only `tables`, scan broker/industry PDFs but include
    only numeric/table chunks.
    """
    selected = _dashboard_source_options()
    families = [s for s in selected if s in _PDF_SOURCE_FAMILIES]
    include_tables = "tables" in selected
    include_narrative = bool(families)
    if include_tables and not families:
        families = list(_PDF_SOURCE_FAMILIES)
        include_narrative = False
    return families, include_tables, include_narrative


def _date_from_date_int(value) -> str:
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    s = f"{n:08d}"
    if not re.fullmatch(r"(19|20)\d{6}", s):
        return ""
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8])).isoformat()
    except ValueError:
        return ""


def _exact_date_from_text(value: str | None) -> str:
    s = str(value or "").strip()
    m = re.search(r"\b(20\d{2}|19\d{2})[-/._](0[1-9]|1[0-2])[-/._]([0-3]\d)\b", s)
    if not m:
        m = re.search(r"\b(20\d{2})(0[1-9]|1[0-2])([0-3]\d)\b", s)
    if not m:
        return ""
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return ""


def _source_date_from_payload(meta: dict) -> str:
    """Prefer exact metadata dates; exact filename YYYYMMDD is fallback."""
    for key in ("source_date", "date", "report_date", "model_date", "parsed_date"):
        d = _exact_date_from_text(str(meta.get(key) or ""))
        if d:
            return d
    d = _date_from_date_int(meta.get("date_int"))
    if d:
        return d
    return _exact_date_from_text(str(meta.get("filename") or ""))


def _source_date_int(value: str) -> int:
    d = _exact_date_from_text(value)
    if not d:
        return 0
    return int(d.replace("-", ""))


def _freshness_reject_reason(source_date: str) -> str:
    d = _exact_date_from_text(source_date)
    if not d:
        return "missing exact source date"
    parsed = date.fromisoformat(d)
    today = datetime.now().date()
    cutoff = today - timedelta(days=_DOCUMENT_FRESHNESS_DAYS)
    if parsed > today:
        return f"future source date {d} > {today.isoformat()}"
    if parsed < cutoff:
        return f"stale source date {d} older than {_DOCUMENT_FRESHNESS_DAYS}-day cutoff {cutoff.isoformat()}"
    return ""


def _payload_memory_scoped(meta: dict) -> bool:
    ticker_values = {
        str(meta.get("ticker") or "").strip(),
        str(meta.get("primary_ticker") or "").strip(),
    }
    for key in ("tickers", "ticker_tags"):
        ticker_values.update(str(v).strip() for v in _payload_list(meta.get(key)))
    if MEMORY_TICKER in ticker_values:
        return True
    if str(meta.get("scope_key") or "").strip() == MEMORY_NICHE_SECTOR:
        return True
    if str(meta.get("primary_niche_sector") or "").strip() == MEMORY_NICHE_SECTOR:
        return True
    return MEMORY_NICHE_SECTOR in set(_payload_list(meta.get("niche_sector_tags")))


def _payload_dashboard_scoped(meta: dict) -> bool:
    """Dashboard refresh uses a hard memory_semiconductors source scope."""
    return _payload_memory_scoped(meta)


def _source_family_from_payload(meta: dict) -> str:
    doc_type = str(meta.get("doc_type") or "").strip().lower()
    source_doc_type = str(meta.get("source_doc_type") or "").strip().lower()
    source_type = str(meta.get("source_type") or meta.get("document_type") or "").strip().lower()
    tags = {doc_type, source_doc_type, source_type}
    if doc_type == "industry_report" or source_doc_type == "industry_report":
        return "industry_report"
    if doc_type == "sellside" or tags & {"broker_research", "broker_research_note", "sellside"}:
        return "broker_research"
    return ""


def _broker_from_payload(meta: dict) -> str:
    broker = normalize_broker(
        str(meta.get("broker") or meta.get("provider") or meta.get("source_broker") or "").strip()
    )
    if broker:
        return broker
    return normalize_broker(broker_from_filename(str(meta.get("filename") or "")))


def _content_allowed(meta: dict, include_tables: bool, include_narrative: bool) -> bool:
    ctype = str(meta.get("content_type") or "").strip().lower()
    media = str(meta.get("media_type") or "").strip().lower()
    numeric = {"table", "estimate_table", "valuation_exhibit"}
    narrative = set(NARRATIVE_CONTENT)
    if ctype in numeric or media == "table":
        return include_tables
    if ctype in narrative or not ctype:
        return include_narrative
    return False


def _list_payload_rows(payload) -> list[dict]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("sources", "rows", "items", "documents", "results", "data", "chunks"):
        value = payload.get(key)
        if isinstance(value, list):
            return [r for r in value if isinstance(r, dict)]
        if isinstance(value, dict):
            nested = _list_payload_rows(value)
            if nested:
                return nested
    return []


def _all_source_rows(gateway, **filters) -> list[dict]:
    """Page through ES source inventory with loop protection."""
    rows: list[dict] = []
    offset: int | None = None
    seen_offsets: set[int] = set()
    while True:
        request_filters = dict(filters)
        if offset is not None:
            request_filters["offset"] = offset
        payload = gateway.sources(**request_filters)
        rows.extend(_list_payload_rows(payload))
        next_offset = payload.get("next_offset") if isinstance(payload, dict) else None
        if next_offset is None:
            break
        try:
            next_value = int(next_offset)
        except (TypeError, ValueError) as exc:
            raise ValueError("ES sources returned an invalid next_offset") from exc
        if next_value in seen_offsets or (offset is not None and next_value <= offset):
            raise ValueError("ES sources returned a repeated next_offset")
        seen_offsets.add(next_value)
        offset = next_value
    return rows


def _source_doc_type_param(family: str) -> str:
    if family == "industry_report":
        return "industry_report"
    return "broker_research"


def _esrag_broker_query_names(broker: str) -> list[str]:
    normalized = normalize_broker(broker)
    aliases = {
        "Arete Research": ["Arete", "Arete Research"],
    }
    names = aliases.get(normalized, [normalized or broker])
    out: list[str] = []
    seen: set[str] = set()
    for name in names:
        clean = str(name or "").strip()
        if clean and clean.lower() not in seen:
            out.append(clean)
            seen.add(clean.lower())
    return out


def _source_row_filename(row: dict) -> str:
    return str(
        row.get("filename")
        or row.get("source_filename")
        or row.get("source")
        or row.get("name")
        or ""
    ).strip()


def _source_row_chunk_count(row: dict) -> int:
    for key in ("chunk_count", "chunks", "num_chunks", "count"):
        try:
            return int(row.get(key) or 0)
        except (TypeError, ValueError):
            continue
    return 0


def _sources_from_esrag_for_broker(broker: str, families: list[str]) -> tuple[list[dict], list[dict]]:
    """List latest indexed PDFs for one broker via ES RAG /sources.

    Returns eligible source rows and skipped stale/missing-date rows. This is
    authoritative because ES already deduplicates filenames and exposes source
    date tags.
    """
    eligible: list[dict] = []
    skipped: dict[tuple[str, str], dict] = {}
    gateway = get_es_rag_gateway()
    for family in families:
        for query_broker in _esrag_broker_query_names(broker):
            source_rows = _all_source_rows(
                gateway,
                broker=query_broker,
                theme="ai",
                source_doc_type=_source_doc_type_param(family),
                pdf_only=True,
                limit=_ESRAG_SOURCES_LIMIT,
            )
            for row in source_rows:
                filename = _source_row_filename(row)
                if not filename or not filename.lower().endswith(".pdf"):
                    continue
                normalized_broker = _broker_from_payload(row) or normalize_broker(broker)
                key = (normalized_broker.lower(), filename.lower())
                if not _payload_dashboard_scoped(row):
                    skipped.setdefault(key, {
                        "broker": normalized_broker,
                        "filename": filename,
                        "source_date": _source_date_from_payload(row),
                        "source_type": family,
                        "skipped_reason": "outside memory_semiconductors scope",
                    })
                    continue
                source_date = _source_date_from_payload(row)
                reject = _freshness_reject_reason(source_date)
                if reject:
                    skipped.setdefault(key, {
                        "broker": normalized_broker,
                        "filename": filename,
                        "source_date": source_date,
                        "source_type": family,
                        "skipped_reason": reject,
                    })
                    continue
                eligible.append({
                    **row,
                    "broker": normalized_broker,
                    "filename": filename,
                    "source_date": source_date,
                    "source_type": family,
                })
    return eligible, list(skipped.values())


def _discover_documents_via_esrag(
    brokers: list[str],
    families: list[str],
) -> tuple[dict[str, list[DocumentRef]], list[dict]]:
    docs_by_broker: dict[str, list[DocumentRef]] = {}
    skipped: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for broker in brokers:
        normalized = normalize_broker(broker)
        if not normalized:
            continue
        rows, skipped_rows = _sources_from_esrag_for_broker(normalized, families)
        skipped.extend(skipped_rows)
        for row in rows:
            filename = _source_row_filename(row)
            source_date = _source_date_from_payload(row) or str(row.get("source_date") or "")
            broker_name = normalize_broker(row.get("broker") or normalized)
            key = (broker_name.lower(), filename.lower())
            if not filename or key in seen:
                continue
            seen.add(key)
            family = str(row.get("source_type") or "broker_research")
            docs_by_broker.setdefault(broker_name, []).append(DocumentRef(
                broker=broker_name,
                filename=filename,
                source_date=source_date,
                source_date_int=_source_date_int(source_date),
                source_type=family,
                doc_type=str(row.get("doc_type") or "").strip(),
                source_doc_type=str(row.get("source_doc_type") or _source_doc_type_param(family)).strip(),
                source_rel_path=str(row.get("source_rel_path") or "").strip(),
                title=str(row.get("report_title") or row.get("title") or "").strip(),
                chunk_count=_source_row_chunk_count(row),
            ))
    for broker, docs in docs_by_broker.items():
        docs.sort(key=lambda d: (d.source_date_int, d.filename), reverse=True)
    return docs_by_broker, skipped


async def _discover_documents(
    brokers: list[str],
    source_options: list[str],
) -> tuple[dict[str, list[DocumentRef]], list[dict]]:
    families, _include_tables, _include_narrative = _document_source_options()
    if not families:
        return {}, []
    broker_set = {normalize_broker(b).lower() for b in brokers if normalize_broker(b)}
    if not broker_set:
        return {}, []
    es_docs, es_skipped = await asyncio.to_thread(_discover_documents_via_esrag, brokers, families)
    return es_docs, es_skipped


def _chunk_payload_text(row: dict, meta: dict) -> str:
    return str(
        row.get("text")
        or row.get("content")
        or row.get("chunk_text")
        or meta.get("_text")
        or meta.get("text")
        or ""
    )


def _chunks_from_esrag_for_filename(filename: str) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    gateway = get_es_rag_gateway()
    while True:
        payload = gateway.chunks(
            filename=filename,
            include_text=True,
            limit=_ESRAG_CHUNKS_PAGE_SIZE,
            offset=offset,
        )
        page_rows = _list_payload_rows(payload)
        if not page_rows:
            break
        for row in page_rows:
            meta = dict(row.get("metadata") or row.get("payload") or row)
            text = _chunk_payload_text(row, meta)
            if text and "_text" not in meta:
                meta["_text"] = text
            meta.setdefault("filename", filename)
            rows.append(meta)
        next_offset = payload.get("next_offset") if isinstance(payload, dict) else None
        if next_offset is None:
            break
        offset = int(next_offset)
    return rows


async def _load_document_chunks(ref: DocumentRef, source_options: list[str]) -> list[dict]:
    _families, include_tables, include_narrative = _document_source_options()
    payloads = await asyncio.to_thread(_chunks_from_esrag_for_filename, ref.filename)
    rows: list[dict] = []
    for meta in payloads:
        if not _content_allowed(meta, include_tables, include_narrative):
            continue
        text = meta.get("_text") or meta.get("text") or ""
        if not str(text).strip():
            continue
        row = _to_row(str(text), meta, 0.0)
        row["broker"] = ref.broker
        row["date"] = ref.source_date
        row["filename"] = ref.filename
        row["source_type"] = ref.source_type
        row["metadata"] = meta
        try:
            row["chunk_index"] = int(meta.get("chunk_index") or meta.get("chunk_id") or 0)
        except Exception:
            row["chunk_index"] = 0
        rows.append(row)
    rows.sort(key=lambda r: (
        int(r.get("page") or 0),
        int(r.get("chunk_index") or 0),
        str(r.get("filename") or ""),
    ))
    return rows


async def ensure_document_refresh_audit_table() -> None:
    db = await get_db()
    await db.execute(
        """CREATE TABLE IF NOT EXISTS dashboard_refresh_document_audit (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               job_id TEXT NOT NULL DEFAULT '',
               broker TEXT NOT NULL DEFAULT '',
               filename TEXT NOT NULL DEFAULT '',
               source_date TEXT NOT NULL DEFAULT '',
               rank INTEGER NOT NULL DEFAULT 0,
               status TEXT NOT NULL DEFAULT '',
               extracted_rows INTEGER NOT NULL DEFAULT 0,
               validated_rows INTEGER NOT NULL DEFAULT 0,
               missing_metrics_json TEXT NOT NULL DEFAULT '[]',
               skipped_reason TEXT NOT NULL DEFAULT '',
               created_at TEXT NOT NULL,
               updated_at TEXT NOT NULL,
               UNIQUE(job_id, broker, filename, source_date, status, rank)
           )"""
    )
    await db.execute(
        """CREATE INDEX IF NOT EXISTS idx_dashboard_doc_audit_job
           ON dashboard_refresh_document_audit(job_id, broker, rank)"""
    )
    await db.execute(
        """CREATE INDEX IF NOT EXISTS idx_dashboard_doc_audit_broker_date
           ON dashboard_refresh_document_audit(broker, source_date)"""
    )
    await db.commit()


async def _record_document_audit(
    *,
    job_id: str,
    broker: str,
    filename: str,
    source_date: str,
    rank: int,
    status: str,
    extracted_rows: int = 0,
    validated_rows: int = 0,
    missing_metrics: set[str] | list[str] | None = None,
    skipped_reason: str = "",
) -> None:
    await ensure_document_refresh_audit_table()
    db = await get_db()
    now = datetime.now().isoformat(timespec="seconds")
    missing_json = json.dumps(sorted(missing_metrics or []))
    await db.execute(
        """INSERT OR REPLACE INTO dashboard_refresh_document_audit
           (job_id, broker, filename, source_date, rank, status, extracted_rows,
            validated_rows, missing_metrics_json, skipped_reason, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(
               (SELECT created_at FROM dashboard_refresh_document_audit
                WHERE job_id=? AND broker=? AND filename=? AND source_date=? AND status=? AND rank=?),
               ?
           ), ?)""",
        (
            job_id, normalize_broker(broker), filename, source_date, int(rank), status,
            int(extracted_rows or 0), int(validated_rows or 0), missing_json,
            skipped_reason or "", job_id, normalize_broker(broker), filename,
            source_date, status, int(rank), now, now,
        ),
    )
    await db.commit()


async def _refresh_audit_validated_counts(job_id: str) -> None:
    await ensure_document_refresh_audit_table()
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT id, broker, filename
           FROM dashboard_refresh_document_audit
           WHERE job_id=? AND filename!=''""",
        (job_id,),
    )
    for r in rows:
        cur = await db.execute(
            """SELECT COUNT(*) AS n
               FROM dashboard_datapoints_verified
               WHERE batch_id=? AND source_broker=? AND source_filename=?""",
            (job_id, r["broker"], r["filename"]),
        )
        count_row = await cur.fetchone()
        await db.execute(
            "UPDATE dashboard_refresh_document_audit SET validated_rows=?, updated_at=? WHERE id=?",
            (int(count_row["n"] or 0) if count_row else 0,
             datetime.now().isoformat(timespec="seconds"), r["id"]),
        )
    await db.commit()


async def get_document_refresh_audit(limit: int = 200, job_id: str | None = None) -> list[dict]:
    await ensure_document_refresh_audit_table()
    db = await get_db()
    if job_id:
        rows = await db.execute_fetchall(
            """SELECT * FROM dashboard_refresh_document_audit
               WHERE job_id=?
               ORDER BY broker, rank, id
               LIMIT ?""",
            (job_id, max(1, min(int(limit or 200), 1000))),
        )
    else:
        rows = await db.execute_fetchall(
            """SELECT * FROM dashboard_refresh_document_audit
               ORDER BY id DESC
               LIMIT ?""",
            (max(1, min(int(limit or 200), 1000)),),
        )
    return [dict(r) for r in rows]


# In-process job state. Keyed by job_id; cleared by /jobs/cleanup or after 1h.
_jobs: dict[str, dict] = {}
_jobs_lock = asyncio.Lock()


# ---------- user_input chunk cache (per-refresh-job) -----------------------
# Pulled once at job start through the ES sources/chunks API.
# Each chunk is mapped to its `broker` metadata + raw text once, so per-cell
# matching is a cheap in-memory scan instead of an extra retrieve per broker.
_user_input_cache: list[dict] | None = None
_user_input_cache_ts: float = 0.0
_USER_INPUT_TTL_SEC = 15 * 60


def _load_user_input_chunks() -> list[dict]:
    """Load every user_input chunk from ES RAG once
    per job and cache for the TTL. Returns a list of dicts with the same
    shape `_extract_cell` expects from `retrieve_for_section`.

    Read-only against the shared service; gateway failures propagate so callers
    cannot mistake an unavailable corpus for an empty one.
    """
    global _user_input_cache, _user_input_cache_ts
    now = time.time()
    if _user_input_cache is not None and (now - _user_input_cache_ts) < _USER_INPUT_TTL_SEC:
        return _user_input_cache
    gateway = get_es_rag_gateway()
    out: list[dict] = []
    source_rows = _all_source_rows(
        gateway, doc_type="user_input", theme="ai", limit=200
    )
    for source in source_rows:
        filename = _source_row_filename(source)
        if not filename:
            continue
        offset = 0
        while True:
            chunk_payload = gateway.chunks(
                filename=filename,
                doc_type="user_input",
                include_text=True,
                limit=_ESRAG_CHUNKS_PAGE_SIZE,
                offset=offset,
            )
            for row in _list_payload_rows(chunk_payload):
                meta = dict(row.get("metadata") or row.get("payload") or row)
                text = _chunk_payload_text(row, meta)
                niches = _payload_list(meta.get("niche_sector_tags"))
                sectorish = {
                    str(meta.get("sector") or ""),
                    str(meta.get("industry") or ""),
                    str(meta.get("niche_sector") or ""),
                    str(meta.get("primary_niche_sector") or ""),
                }
                if MEMORY_NICHE_SECTOR not in niches and MEMORY_NICHE_SECTOR not in sectorish:
                    continue
                if not text.strip():
                    continue
                out.append({
                    "text": text,
                    "broker": str(meta.get("broker") or "").strip(),
                    "date": str(meta.get("date") or meta.get("date_pertains_to") or "").strip(),
                    "filename": str(meta.get("title") or meta.get("filename") or filename).strip(),
                    "page": int(meta.get("page") or meta.get("page_number") or 0),
                    "score": 0.0,
                    "metadata": meta,
                })
            next_offset = (
                chunk_payload.get("next_offset")
                if isinstance(chunk_payload, dict)
                else None
            )
            if next_offset is None:
                break
            offset = int(next_offset)
    _user_input_cache = out
    _user_input_cache_ts = now
    logger.info("user_input chunk cache loaded through ES API: %d chunks", len(out))
    return out

def _user_input_for_broker(broker: str) -> list[dict]:
    """Return cached user_input chunks where `broker` is either the metadata
    tag or appears in the chunk text (case-insensitive). Empty list if cache
    is empty or no match."""
    cache = _load_user_input_chunks()
    if not cache:
        return []
    target = broker.strip().lower()
    if not target:
        return []
    pat = re.compile(rf"\b{re.escape(broker)}\b", re.IGNORECASE)
    out: list[dict] = []
    for c in cache:
        meta_broker = (c.get("broker") or "").lower()
        if meta_broker == target or pat.search(c.get("text") or ""):
            # Normalize so downstream broker-equality filter passes.
            row = dict(c)
            row["broker"] = broker
            out.append(row)
    return out


def _new_job() -> str:
    import uuid
    return uuid.uuid4().hex[:12]


def get_job(job_id: str) -> dict | None:
    return _jobs.get(job_id)


def list_jobs() -> list[dict]:
    """Return jobs sorted newest-first; trim to last 20 to bound memory."""
    items = sorted(_jobs.values(), key=lambda j: j.get("started_at", ""), reverse=True)
    return items[:20]


def _period_frequency(period: str) -> str:
    p = (period or "").strip().upper()
    if "Q" in p:
        return "quarterly"
    if "-" in p:
        return "monthly"
    return "yearly"


def _frequency_prompt_rule(frequency: str) -> str:
    if frequency == "quarterly":
        return (
            "- Time grain: QUARTERLY ONLY. Extract rows stated as quarters "
            "(e.g. 1Q26, 2Q26, 2026Q1) and normalize them to YYYYQn. "
            "Do not emit annual/yearly rows or monthly rows."
        )
    return (
        "- Time grain: YEARLY ONLY. Extract annual / calendar-year / fiscal-year "
        "rows only (e.g. 2026, CY26, FY26, 2027E) and normalize them to YYYY. "
        "Do not emit quarterly or monthly rows."
    )


def _build_cell_prompt(metric: str, broker: str, chunks: list[dict], frequency: str) -> str:
    """Build a OpenAI prompt to extract THIS broker's LATEST forecast curve for
    ONE metric. Excerpts are pre-filtered to the broker's most recent 1-2
    report dates by the caller, so 'latest' is enforced by SOURCE SELECTION;
    OpenAI just needs to extract whatever values these latest excerpts state.

    Mirrors the model.py cell prompt but asks for the full multi-year series."""
    canon_unit, segment = CANONICAL_METRICS.get(metric, ("?", "?"))
    # Show which dates the excerpts are from so the LLM understands these are
    # the broker's MOST RECENT views.
    dates = sorted({(c.get('date') or '').strip() for c in chunks if c.get('date')}, reverse=True)
    date_hint = ", ".join(dates) if dates else "unknown date"
    ctx = "\n\n".join(
        f"[S{i + 1} | {c.get('broker', '?')} {c.get('date', '?')} | "
        f"p.{c.get('page', '?')}]\n{(c.get('text') or '')[:1100]}"
        for i, c in enumerate(chunks)
    )
    prompt_override = get_tab_prompt("dashboard")
    if prompt_override:
        return render_prompt_template(prompt_override, {
            "metric": metric,
            "broker": broker,
            "canon_unit": canon_unit,
            "segment": segment,
            "date_hint": date_hint,
            "ctx": ctx,
            "frequency": frequency,
            "frequency_rule": _frequency_prompt_rule(frequency),
        })
    return f"""You are extracting {broker}'s LATEST published view on ONE metric.

The excerpts below are from {broker}'s most recent report(s) covering
"{metric}" - date(s): {date_hint}. Extract every (period, value) {broker}
states in these excerpts. Do NOT invent values from other reports or your
own knowledge - only what these excerpts say.

METRIC: {metric} (canonical unit: {canon_unit}, segment: {segment})
BROKER: {broker}

EXCERPTS (these ARE the broker's latest view - extract from them as-is):
{ctx}

RULES:
- Output one row per (period, value) the excerpts STATE for this metric.
  If the excerpts show a multi-year forecast table (e.g. 2024-2028), output
  ALL years - don't truncate to "the latest year". The CHART is a time
  series; we need every period the broker forecasts.
{_frequency_prompt_rule(frequency)}
- Periods: years like "2026" / "2027e" -> "2026", "2027". Quarters like
  "2Q26" -> "2026Q2". Months like "Jun-26" -> "2026-06".
- "value": number only (e.g. 1.52, 23.7, 106).
- "unit": exactly as printed in the excerpt (e.g. "%", "$/Gb", "bn Gb").
- Do not rescale the printed number. If a table cell says "48%" return
  value 48 and unit "%", not 0.48. If the printed unit is bps, cents, mn,
  bn, GB, Gb, EB, or 8Gb-eq, keep that printed scale in "unit"; the backend
  normalizer will convert it deterministically.
- If the row/header unit is not visible in the excerpt, skip the row rather
  than guessing. Unit guesses create 10x/100x chart errors.
- "quote": the EXACT text fragment (max 120 chars) containing the value,
  copied verbatim from the excerpts including digits as printed. If you
  can't find a verbatim quote with the number, DO NOT emit that row.
- "subject_entity": "Industry Total" unless the value is specifically about
  a producer/company such as CXMT, YMTC, Samsung, SK Hynix, or Micron.
- "source": which excerpt id (S1, S2, ...) supports each row.
- Skip rows you'd have to guess. Empty array is fine if nothing matches.

STRICT JSON only (no markdown fence):
{{"datapoints": [{{"subject_entity": "Industry Total", "period": "...", "value": 0.0, "unit": "...", "quote": "...", "source": "S1"}}]}}"""


async def _call_dashboard_claude_json(prompt: str) -> dict:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return {}
    from .openai_research import AsyncResearchClient
    client = AsyncResearchClient(api_key=api_key)

    async def _request_text() -> str:
        async with client.messages.stream(
            model=_DASHBOARD_OPENAI_MODEL, max_tokens=3000, temperature=0,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            resp = await stream.get_final_message()
        return "".join(getattr(b, "text", "") for b in resp.content).strip()

    try:
        text = await asyncio.wait_for(_request_text(), timeout=_DASHBOARD_OPENAI_TIMEOUT_SEC)
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                try:
                    return json.loads(m.group(0))
                except json.JSONDecodeError:
                    pass
            logger.warning("Dashboard extraction skipped malformed OpenAI JSON: %s", str(exc)[:220])
            return {}
    except asyncio.TimeoutError:
        logger.warning(
            "Dashboard extraction OpenAI call timed out after %ss",
            _DASHBOARD_OPENAI_TIMEOUT_SEC,
        )
        return {}
    except Exception:
        logger.exception("Dashboard extraction OpenAI call failed")
        raise


_call_haiku_json = _call_dashboard_claude_json


def _metric_catalog(metrics: list[str]) -> str:
    lines = []
    for metric in metrics:
        canon_unit, segment = CANONICAL_METRICS.get(metric, ("", ""))
        label = metric.replace("_", " ")
        lines.append(f"- {metric} | {label} | canonical unit: {canon_unit} | segment: {segment}")
    return "\n".join(lines)


def _chunk_batches(chunks: list[dict]) -> list[list[dict]]:
    batches: list[list[dict]] = []
    cur: list[dict] = []
    chars = 0
    for chunk in chunks:
        text = str(chunk.get("text") or "")
        if not text.strip():
            continue
        cost = min(len(text), _DOCUMENT_CHUNK_CHAR_LIMIT) + 180
        if cur and (
            chars + cost > _DOCUMENT_BATCH_CHAR_BUDGET
            or len(cur) >= _DOCUMENT_MAX_CHUNKS_PER_BATCH
        ):
            batches.append(cur)
            cur = []
            chars = 0
        cur.append(chunk)
        chars += cost
    if cur:
        batches.append(cur)
    return batches


def _build_document_prompt(
    *,
    broker: str,
    ref: DocumentRef,
    chunks: list[dict],
    metrics: list[str],
    frequency: str,
) -> str:
    ctx = "\n\n".join(
        f"[S{i + 1} | {broker} | {ref.source_date} | p.{c.get('page', '?')} | "
        f"{c.get('content_type') or c.get('media_type') or 'chunk'}]\n"
        f"{(c.get('text') or '')[:_DOCUMENT_CHUNK_CHAR_LIMIT]}"
        for i, c in enumerate(chunks)
    )
    metric_list = _metric_catalog(metrics)
    prompt_override = get_tab_prompt("dashboard")
    if prompt_override:
        return render_prompt_template(prompt_override, {
            "metric": ", ".join(metrics),
            "metrics": metric_list,
            "broker": broker,
            "canon_unit": "see metric catalog",
            "segment": "see metric catalog",
            "date_hint": ref.source_date,
            "doc_filename": ref.filename,
            "ctx": ctx,
            "frequency": frequency,
            "frequency_rule": _frequency_prompt_rule(frequency),
        })
    return f"""You are extracting dashboard datapoints from ONE broker/provider report.

SOURCE DOCUMENT:
- Broker/provider: {broker}
- Filename: {ref.filename}
- Source date: {ref.source_date}

Extract any rows stated in the excerpts that match the dashboard metrics below.
Do not use outside knowledge. Do not mix in any other report.

METRIC CATALOG:
{metric_list}

EXCERPTS FROM THIS ONE DOCUMENT:
{ctx}

RULES:
- Output only datapoints for metric keys exactly listed in METRIC CATALOG.
{_frequency_prompt_rule(frequency)}
- Extract all periods shown for a metric in the excerpts, not just the latest period.
- Keep the printed value scale and printed unit exactly as shown. Do not rescale.
- If a row/header unit is not visible, skip the datapoint rather than guessing.
- The quote must be copied verbatim from an excerpt and must contain the printed number.
- subject_entity is "Industry Total" unless the datapoint is specifically about a producer/company such as CXMT, YMTC, Samsung, SK Hynix, Kioxia, Micron, WDC/SanDisk, or JHICC.
- source must be the supporting excerpt id, e.g. "S1".
- Empty array is fine if the document excerpts do not state matching datapoints.

STRICT JSON only:
{{"datapoints":[{{"metric":"dram_bit_demand","subject_entity":"Industry Total","period":"2027","value":0.0,"unit":"bn Gb","quote":"exact text containing the number","source":"S1"}}]}}"""


def _row_source_page(chunk: dict) -> int:
    try:
        return int(chunk.get("page") or 0)
    except (TypeError, ValueError):
        return 0


def _datapoint_valid_for_fallback(dp: dict) -> bool:
    try:
        ok, _value, _unit, _note = _normalize_strict(
            str(dp.get("metric") or ""),
            float(dp.get("raw_value") if dp.get("raw_value") is not None else dp.get("value")),
            str(dp.get("raw_unit") or dp.get("unit") or ""),
            "dashboard_document_refresh",
        )
    except Exception:
        return False
    if not ok:
        return False
    return not _freshness_reject_reason(str(dp.get("source_date") or ""))


async def _extract_document(
    *,
    broker: str,
    ref: DocumentRef,
    chunks: list[dict],
    metrics: list[str],
    frequency: str,
    batch_id: str,
    sem: asyncio.Semaphore,
) -> list[dict]:
    if not chunks or not metrics:
        return []
    out: list[dict] = []
    seen: set[tuple] = set()
    for batch in _chunk_batches(chunks):
        async with sem:
            data = await _call_dashboard_claude_json(_build_document_prompt(
                broker=broker,
                ref=ref,
                chunks=batch,
                metrics=metrics,
                frequency=frequency,
            ))
        if not data:
            continue
        source_map = {f"S{i + 1}": c for i, c in enumerate(batch)}
        for raw in data.get("datapoints") or []:
            metric = str(raw.get("metric") or "").strip()
            if metric not in metrics or metric not in CANONICAL_METRICS:
                continue
            period = str(raw.get("period") or "").strip()
            if not period or _period_frequency(period) != frequency:
                continue
            try:
                raw_val = float(raw.get("value"))
            except (TypeError, ValueError):
                continue
            quote = str(raw.get("quote") or "").strip()[:300]
            if not quote or not _value_in_quote(raw_val, quote):
                continue
            raw_unit = str(raw.get("unit") or "").strip()
            if not raw_unit:
                continue
            pint = _period_int(period)
            if pint == 0:
                continue
            src_id = str(raw.get("source") or "").strip()
            src = source_map.get(src_id)
            if src is None:
                continue
            meta = src.get("metadata") or {}
            key = (
                metric,
                str(raw.get("subject_entity") or "Industry Total").strip(),
                period,
                raw_val,
                raw_unit,
                quote,
                _row_source_page(src),
            )
            if key in seen:
                continue
            seen.add(key)
            canon_unit, segment = CANONICAL_METRICS[metric]
            out.append({
                "segment": segment,
                "metric": metric,
                "period": period,
                "period_int": pint,
                "raw_value": raw_val,
                "value": raw_val,
                "unit": raw_unit,
                "raw_unit": raw_unit,
                "broker": broker,
                "source_broker": broker,
                "source_type": ref.source_type,
                "subject_entity": str(raw.get("subject_entity") or "Industry Total").strip(),
                "source_filename": ref.filename,
                "source_date": ref.source_date,
                "source_page": _row_source_page(src),
                "source_quote": quote,
                "doc_type": ref.doc_type or str(meta.get("doc_type") or ""),
                "source_doc_type": ref.source_doc_type or str(meta.get("source_doc_type") or ""),
                "media_type": str(meta.get("media_type") or src.get("media_type") or ""),
                "batch_id": batch_id,
                "canonical_unit": canon_unit,
            })
    return out


async def _extract_cell(metric: str, broker: str, sem: asyncio.Semaphore, frequency: str,
                        batch_id: str = "") -> list[dict]:
    """Retrieve + extract one (metric, broker) cell, biased to the broker's
    LATEST report(s) on this metric. Returns datapoints ready for INSERT.

    "Latest per broker" means: find the most recent report(s) by `source_date`
    where this broker discusses this metric, and extract the multi-period
    forecast from THERE — not cherry-picked across the broker's entire
    historical coverage. We widen the retrieval (k=20, top_n=10) so even
    when the latest report is RANKED LOWER by relevance than older reports,
    it still surfaces, then keep only chunks from the top-2 most-recent
    source_dates for this broker before feeding OpenAI."""
    async with sem:
        query = _METRIC_QUERIES.get(metric, metric)
        start_year = datetime.now().year
        retrieval = get_retrieval_settings()
        chunks_get = int(retrieval.get("chunks_get") or 24)
        rerank_candidates = get_rerank_candidates(retrieval, 12)
        source_options = _dashboard_source_options()
        # Wide retrieval — give the date filter room to find the latest report.
        chunks: list[dict] = []
        if "tables" in source_options:
            chunks.extend(await retrieve_tables(
                f"latest {broker} broker research memory semiconductors table {query} forecast from {start_year}",
                k=chunks_get,
                top_n=chunks_get,
                rerank_candidates=rerank_candidates,
                source_options=source_options,
                scope="memory",
            ))
        text_source_options = [s for s in source_options if s != "tables"]
        if text_source_options:
            chunks.extend(await retrieve_for_section(
                f"latest {broker} broker research memory semiconductors {query} forecast from {start_year}",
                k=chunks_get,
                top_n=chunks_get,
                rerank_candidates=rerank_candidates,
                source_options=text_source_options,
                scope="memory",
            ))
        # Hard broker filter (retrieve_for_section's parser already adds it,
        # but defensive equality check here too in case the planner relaxed).
        chunks = [c for c in chunks if (c.get("broker") or "").lower() == broker.lower()]

        # ── user_input fallback ───────────────────────────────────────────
        # The primary retrieval gates on broker payload — user_input notes
        # whose broker is only mentioned in text (not tagged in metadata)
        # are dropped. Add them back here from the in-process cache so the
        # dashboard reflects pasted analyst notes too.
        user_input_extras = _user_input_for_broker(broker) if "user_input" in source_options else []
        if user_input_extras:
            # Only include the user_input note if it discusses the metric.
            # Light keyword check on the canonical metric key + segment so
            # we don't dilute the cell with unrelated notes.
            metric_terms = [t for t in re.split(r"[_\s]+", metric) if len(t) > 2]
            for c in user_input_extras:
                tx = (c.get("text") or "").lower()
                if any(t in tx for t in metric_terms):
                    chunks.append(c)
            if user_input_extras:
                logger.debug("(%s × %s) +%d user_input candidates",
                             metric, broker,
                             sum(1 for c in user_input_extras
                                 if any(t in (c.get("text") or "").lower()
                                        for t in metric_terms)))

        if not chunks:
            return []
        # KEY: bias to the broker's LATEST report on this metric. Group by
        # source_date desc, keep only chunks from the 2 most-recent dates.
        # 2 (not 1) so a brief May update + detailed April table both surface.
        dated_chunks = [c for c in chunks if (c.get("date") or "").strip()]
        if dated_chunks:
            sorted_dates = sorted({c["date"] for c in dated_chunks}, reverse=True)
            keep_dates = set(sorted_dates[:2])
            chunks = [c for c in dated_chunks if c["date"] in keep_dates]
            logger.debug("(%s × %s) → latest report dates kept: %s",
                         metric, broker, sorted(keep_dates, reverse=True))
        data = await _call_dashboard_claude_json(_build_cell_prompt(metric, broker, chunks, frequency))
        if not data:
            return []
        canon_unit, segment = CANONICAL_METRICS.get(metric, ("?", "?"))
        source_map = {f"S{i + 1}": c for i, c in enumerate(chunks)}
        out: list[dict] = []
        for raw in (data.get("datapoints") or []):
            try:
                period = str(raw.get("period") or "").strip()
                raw_val = float(raw.get("value"))
            except (TypeError, ValueError):
                continue
            if not period:
                continue
            raw_unit = str(raw.get("unit") or "").strip()
            quote = str(raw.get("quote") or "").strip()[:300]
            # Mechanical citation gate: the value must appear in the quote.
            if not _value_in_quote(raw_val, quote):
                continue
            pint = _period_int(period)
            if pint == 0:
                continue
            if _period_frequency(period) != frequency:
                continue
            src_id = str(raw.get("source") or "").strip()
            src = source_map.get(src_id) or chunks[0]
            out.append({
                "segment": segment,
                "metric": metric,
                "period": period,
                "period_int": pint,
                "raw_value": raw_val,
                "value": raw_val,
                "unit": raw_unit,
                "raw_unit": raw_unit,
                "broker": broker,
                "source_broker": broker,
                "source_type": _chunk_source_type(src),
                "subject_entity": str(raw.get("subject_entity") or "").strip(),
                "source_filename": _chunk_meta_value(src, "filename", "") or "",
                "source_date": _chunk_meta_value(src, "date", "") or "",
                "source_page": int(_chunk_meta_value(src, "page", 0) or 0),
                "source_quote": quote,
                "doc_type": _chunk_meta_value(src, "doc_type", "") or "",
                "source_doc_type": _chunk_meta_value(src, "source_doc_type", "") or "",
                "media_type": _chunk_meta_value(src, "media_type", "") or "",
                "batch_id": batch_id,
            })
        # Keep only the LATEST PER PERIOD (user said "latest estimate") —
        # multiple chunks from the same broker for the same period collapse
        # to the most-recent-source-date one.
        by_period: dict[str, dict] = {}
        for dp in out:
            key = dp["period"]
            cur = by_period.get(key)
            if cur is None or (dp["source_date"] or "") > (cur["source_date"] or ""):
                by_period[key] = dp
        return list(by_period.values())


async def _persist(
    datapoints: list[dict],
    replace_metric_broker: tuple[str, str] | None,
    frequency: str,
) -> int:
    """Replace this (metric, broker) tuple's raw candidates atomically.

    The dashboard never plots these raw rows directly. They are promoted into
    dashboard_datapoints_verified only after strict metric/unit validation.
    """
    # Empty extraction is a no-op. A sparse/failed RAG pass must not erase the
    # last verified time series currently shown on the dashboard.
    if not datapoints:
        return 0
    return await store_raw_dashboard_datapoints(
        datapoints,
        replace_metric_broker=replace_metric_broker,
        replace_frequency=frequency,
        extractor="dashboard_rag_refresh",
    )


async def _persist_document_datapoints(datapoints: list[dict]) -> int:
    if not datapoints:
        return 0
    return await store_raw_dashboard_datapoints(
        datapoints,
        replace_metric_broker=None,
        replace_frequency=None,
        extractor="dashboard_document_refresh",
    )


async def _run_metric_first_job_legacy(job_id: str, metrics: list[str], brokers: list[str], frequency: str) -> None:
    """Top-level job loop: fan out (metric × broker), respect concurrency
    ceiling, persist incrementally, update job progress."""
    await ensure_dashboard_datapoint_tables()
    # Invalidate the user_input cache so notes pasted since the last refresh
    # surface in this job. Reloaded lazily on the first _user_input_for_broker.
    global _user_input_cache, _user_input_cache_ts
    _user_input_cache = None
    _user_input_cache_ts = 0.0
    sem = asyncio.Semaphore(_DASHBOARD_OPENAI_CONCURRENCY)
    cells = [(m, b) for m in metrics for b in brokers]
    total = len(cells)
    inserted = 0
    completed = 0
    failed = 0
    errors: list[dict] = []

    async with _jobs_lock:
        _jobs[job_id].update({"total": total, "status": "running", "errors": []})

    async def _one(m: str, b: str) -> None:
        nonlocal inserted, completed, failed
        try:
            dps = await _extract_cell(m, b, sem, frequency, batch_id=job_id)
            n = await _persist(dps, replace_metric_broker=(m, b), frequency=frequency)
            inserted += n
        except Exception as exc:
            failed += 1
            errors.append({"metric": m, "broker": b, "error": str(exc)[:500]})
            logger.exception("Refresh cell failed for %s x %s", m, b)
            logger.warning("Refresh cell failed for %s × %s: %s", m, b, exc)
        finally:
            completed += 1
            async with _jobs_lock:
                _jobs[job_id].update({
                    "completed": completed, "inserted": inserted, "failed": failed,
                    "errors": errors[-20:],
                })

    await asyncio.gather(*(_one(m, b) for m, b in cells))

    validation = {}
    if inserted:
        async with _jobs_lock:
            _jobs[job_id].update({"status": "validating"})
        try:
            validation = await rebuild_verified_datapoints(set(metrics))
        except Exception as exc:
            logger.exception("Dashboard terminal validation failed")
            validation = {"error": str(exc)}
            errors.append({"metric": "*", "broker": "*", "error": f"validation failed: {exc}"[:500]})

    cache_job = {}
    if inserted and not validation.get("error"):
        try:
            async with _jobs_lock:
                _jobs[job_id].update({"status": "cache_rebuilding"})
            await mark_cache_dirty(frequency, f"dashboard RAG refresh {job_id}")
            cache_job = await start_cache_rebuild(frequency)
        except Exception as exc:
            logger.exception("Dashboard cache rebuild enqueue failed")
            errors.append({"metric": "*", "broker": "*", "error": f"cache enqueue failed: {exc}"[:500]})

    async with _jobs_lock:
        _jobs[job_id].update({
            "status": "done",
            "validation": validation,
            "cache_job": cache_job,
            "cache_job_id": cache_job.get("job_id", ""),
            "errors": errors[-20:],
            "finished_at": datetime.now().isoformat(timespec="seconds"),
        })


async def _kick_off_metric_first_refresh_legacy(metrics: list[str] | None = None,
                                                brokers: list[str] | None = None,
                                                frequency: str = "yearly") -> str:
    """Legacy metric-first refresh job. Returns a job_id the caller polls.

    `metrics=None` → ALL canonical metrics; `brokers=None` → KNOWN_BROKERS."""
    await ensure_dashboard_datapoint_tables()
    frequency = "quarterly" if str(frequency).lower() == "quarterly" else "yearly"
    job_id = _new_job()
    mlist = metrics or list(CANONICAL_METRICS.keys())
    requested_brokers = brokers or list(KNOWN_BROKERS)
    blist = list(requested_brokers)
    active_excel_keys = {x.lower() for x in await get_active_excel_brokers()}
    active_excel = [b for b in requested_brokers if b.lower() in active_excel_keys]
    async with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "status": "queued",
            "total": len(mlist) * len(blist),
            "completed": 0,
            "inserted": 0,
            "failed": 0,
            "errors": [],
            "metrics": mlist,
            "brokers": blist,
            "frequency": frequency,
            "active_excel_brokers": active_excel,
            "skipped_excel_brokers": [],
            "allowed_sources": _dashboard_source_options(),
            "excel_override_policy": "allowed source date must be newer than active Excel model date",
            "started_at": datetime.now().isoformat(timespec="seconds"),
        }
    # Fire and forget — the asyncio loop picks it up.
    asyncio.create_task(_run_metric_first_job_legacy(job_id, mlist, blist, frequency))
    # Trim old jobs (keep 20 most recent).
    if len(_jobs) > 40:
        old = sorted(_jobs.keys(), key=lambda k: _jobs[k].get("started_at", ""))
        for k in old[:-20]:
            _jobs.pop(k, None)
    return job_id


# ---------------------------------------------------------------------------
# Active broker-document-first refresh
# ---------------------------------------------------------------------------

async def _run_job(job_id: str, metrics: list[str], brokers: list[str], frequency: str) -> None:
    """Refresh Dashboard candidates by reading recent broker PDFs as documents.

    Active Excel brokers are skipped. For every other broker, dated user input
    is extracted first, then recent PDFs are scanned newest-first. A PDF owns a
    metric once it yields at least one valid datapoint, so older PDFs only fill
    metrics the fresher source missed entirely.
    """
    await ensure_dashboard_datapoint_tables()
    await ensure_document_refresh_audit_table()

    global _user_input_cache, _user_input_cache_ts
    _user_input_cache = None
    _user_input_cache_ts = 0.0

    source_options = _dashboard_source_options()
    active_excel_keys = {normalize_broker(x).lower() for x in await get_active_excel_brokers()}
    skipped_excel = [normalize_broker(b) for b in brokers if normalize_broker(b).lower() in active_excel_keys]
    work_brokers = [
        normalize_broker(b)
        for b in brokers
        if normalize_broker(b) and normalize_broker(b).lower() not in active_excel_keys
    ]

    sem = asyncio.Semaphore(_DASHBOARD_OPENAI_CONCURRENCY)
    inserted = 0
    completed = 0
    failed = 0
    scanned_documents = 0
    errors: list[dict] = []

    async with _jobs_lock:
        _jobs[job_id].update({
            "status": "running",
            "strategy": "broker_document_first",
            "total": len(work_brokers),
            "current": {"stage": "discovering_documents"},
        })
    docs_by_broker, skipped_docs = await _discover_documents(work_brokers, source_options)

    for broker in skipped_excel:
        await _record_document_audit(
            job_id=job_id,
            broker=broker,
            filename="",
            source_date="",
            rank=0,
            status="skipped_excel",
            missing_metrics=set(metrics),
            skipped_reason="active Excel broker skipped by automatic PDF refresh",
        )
    for row in skipped_docs[:_MAX_AUDIT_STALE_DOCUMENTS]:
        await _record_document_audit(
            job_id=job_id,
            broker=row.get("broker") or "",
            filename=row.get("filename") or "",
            source_date=row.get("source_date") or "",
            rank=0,
            status="skipped_stale_or_invalid_date",
            missing_metrics=set(metrics),
            skipped_reason=row.get("skipped_reason") or "",
        )

    async with _jobs_lock:
        _jobs[job_id].update({
            "status": "running",
            "strategy": "broker_document_first",
            "total": len(work_brokers),
            "completed": 0,
            "inserted": 0,
            "failed": 0,
            "errors": [],
            "skipped_excel_brokers": skipped_excel,
            "stale_documents_count": len(skipped_docs),
            "stale_documents": skipped_docs[:20],
            "scanned_documents": 0,
            "max_pdf_fallbacks": _MAX_PDF_FALLBACKS_PER_BROKER,
        })

    async def _extract_user_inputs_for_broker(broker: str) -> int:
        if "user_input" not in source_options:
            return 0
        count = 0
        chunks = _user_input_for_broker(broker)
        for idx, chunk in enumerate(chunks, start=1):
            meta = chunk.get("metadata") or {}
            source_date = _exact_date_from_text(chunk.get("date")) or _source_date_from_payload(meta)
            ref = DocumentRef(
                broker=broker,
                filename=str(chunk.get("filename") or meta.get("title") or "user_input").strip(),
                source_date=source_date,
                source_date_int=_source_date_int(source_date),
                source_type="user_input",
                doc_type="user_input",
                source_doc_type="user_input",
                chunk_count=1,
            )
            try:
                dps = await _extract_document(
                    broker=broker,
                    ref=ref,
                    chunks=[chunk],
                    metrics=metrics,
                    frequency=frequency,
                    batch_id=job_id,
                    sem=sem,
                )
                n = await _persist_document_datapoints(dps)
                count += n
                await _record_document_audit(
                    job_id=job_id,
                    broker=broker,
                    filename=ref.filename,
                    source_date=ref.source_date,
                    rank=idx,
                    status="user_input_extracted" if n else "user_input_no_datapoints",
                    extracted_rows=n,
                    missing_metrics=[],
                    skipped_reason="" if n else "no matching dashboard datapoints extracted",
                )
            except Exception as exc:
                errors.append({"broker": broker, "filename": ref.filename, "error": str(exc)[:500]})
                await _record_document_audit(
                    job_id=job_id,
                    broker=broker,
                    filename=ref.filename,
                    source_date=ref.source_date,
                    rank=idx,
                    status="user_input_error",
                    skipped_reason=str(exc)[:500],
                )
        return count

    for broker in work_brokers:
        try:
            async with _jobs_lock:
                _jobs[job_id].update({"current": {"broker": broker, "stage": "user_input"}})
            inserted += await _extract_user_inputs_for_broker(broker)

            missing_metrics = set(metrics)
            docs = docs_by_broker.get(broker, [])
            if not docs:
                await _record_document_audit(
                    job_id=job_id,
                    broker=broker,
                    filename="",
                    source_date="",
                    rank=0,
                    status="no_eligible_pdf",
                    missing_metrics=missing_metrics,
                    skipped_reason=f"no fresh PDF within {_DOCUMENT_FRESHNESS_DAYS} days",
                )

            for rank, ref in enumerate(docs[:_MAX_PDF_FALLBACKS_PER_BROKER], start=1):
                if not missing_metrics:
                    break
                async with _jobs_lock:
                    _jobs[job_id].update({
                        "current": {
                            "broker": broker,
                            "filename": ref.filename,
                            "source_date": ref.source_date,
                            "rank": rank,
                        }
                    })
                chunks = await _load_document_chunks(ref, source_options)
                scanned_documents += 1
                if not chunks:
                    await _record_document_audit(
                        job_id=job_id,
                        broker=broker,
                        filename=ref.filename,
                        source_date=ref.source_date,
                        rank=rank,
                        status="no_chunks",
                        missing_metrics=missing_metrics,
                        skipped_reason="no eligible narrative/table chunks for selected source settings",
                    )
                    continue
                try:
                    dps = await asyncio.wait_for(
                        _extract_document(
                            broker=broker,
                            ref=ref,
                            chunks=chunks,
                            metrics=sorted(missing_metrics),
                            frequency=frequency,
                            batch_id=job_id,
                            sem=sem,
                        ),
                        timeout=_DOCUMENT_EXTRACT_TIMEOUT_SEC,
                    )
                except asyncio.TimeoutError:
                    errors.append({
                        "broker": broker,
                        "filename": ref.filename,
                        "error": f"document extraction timed out after {_DOCUMENT_EXTRACT_TIMEOUT_SEC}s",
                    })
                    await _record_document_audit(
                        job_id=job_id,
                        broker=broker,
                        filename=ref.filename,
                        source_date=ref.source_date,
                        rank=rank,
                        status="document_timeout",
                        missing_metrics=missing_metrics,
                        skipped_reason=f"document extraction timed out after {_DOCUMENT_EXTRACT_TIMEOUT_SEC}s",
                    )
                    continue
                except Exception as exc:
                    errors.append({"broker": broker, "filename": ref.filename, "error": str(exc)[:500]})
                    await _record_document_audit(
                        job_id=job_id,
                        broker=broker,
                        filename=ref.filename,
                        source_date=ref.source_date,
                        rank=rank,
                        status="document_error",
                        missing_metrics=missing_metrics,
                        skipped_reason=str(exc)[:500],
                    )
                    continue
                valid_metrics = {dp["metric"] for dp in dps if _datapoint_valid_for_fallback(dp)}
                n = await _persist_document_datapoints(dps)
                inserted += n
                missing_metrics -= valid_metrics
                await _record_document_audit(
                    job_id=job_id,
                    broker=broker,
                    filename=ref.filename,
                    source_date=ref.source_date,
                    rank=rank,
                    status="extracted" if n else "no_datapoints",
                    extracted_rows=n,
                    missing_metrics=missing_metrics,
                    skipped_reason="" if n else "no matching dashboard datapoints extracted",
                )
                async with _jobs_lock:
                    _jobs[job_id].update({
                        "inserted": inserted,
                        "scanned_documents": scanned_documents,
                        "missing_metrics_by_current_broker": sorted(missing_metrics),
                    })
        except Exception as exc:
            failed += 1
            errors.append({"broker": broker, "error": str(exc)[:500]})
            logger.exception("Document-first refresh failed for broker %s", broker)
        finally:
            completed += 1
            async with _jobs_lock:
                _jobs[job_id].update({
                    "completed": completed,
                    "inserted": inserted,
                    "failed": failed,
                    "scanned_documents": scanned_documents,
                    "errors": errors[-20:],
                })

    validation = {}
    async with _jobs_lock:
        _jobs[job_id].update({"status": "validating", "current": {"stage": "validating"}})
    try:
        validation = await rebuild_verified_datapoints(set(metrics))
        await _refresh_audit_validated_counts(job_id)
    except Exception as exc:
        logger.exception("Dashboard terminal validation failed")
        validation = {"error": str(exc)}
        errors.append({"metric": "*", "broker": "*", "error": f"validation failed: {exc}"[:500]})

    cache_job = {}
    if not validation.get("error"):
        try:
            async with _jobs_lock:
                _jobs[job_id].update({"status": "cache_rebuilding", "current": {"stage": "cache_rebuilding"}})
            await mark_cache_dirty(frequency, f"dashboard document refresh {job_id}")
            cache_job = await start_cache_rebuild(frequency)
        except Exception as exc:
            logger.exception("Dashboard cache rebuild enqueue failed")
            errors.append({"metric": "*", "broker": "*", "error": f"cache enqueue failed: {exc}"[:500]})

    async with _jobs_lock:
        _jobs[job_id].update({
            "status": "done",
            "validation": validation,
            "cache_job": cache_job,
            "cache_job_id": cache_job.get("job_id", ""),
            "errors": errors[-20:],
            "current": None,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
        })


async def _run_job_guarded(
    job_id: str,
    metrics: list[str],
    brokers: list[str],
    frequency: str,
) -> None:
    """Ensure every background refresh reaches a terminal job state."""
    try:
        await _run_job(job_id, metrics, brokers, frequency)
    except Exception as exc:
        operation = exc.operation if isinstance(exc, EsRagError) else "refresh"
        if isinstance(exc, EsRagError):
            logger.error(
                "Dashboard refresh job %s failed: operation=%s status=%s",
                job_id,
                operation,
                exc.status_code,
            )
        else:
            logger.exception("Dashboard refresh job %s failed", job_id)
        async with _jobs_lock:
            if job_id in _jobs:
                _jobs[job_id].update({
                    "status": "failed",
                    "failed": max(1, int(_jobs[job_id].get("failed") or 0)),
                    "current": None,
                    "errors": [{
                        "operation": operation,
                        "error": "ES RAG service unavailable"
                        if isinstance(exc, EsRagError)
                        else "Dashboard refresh failed",
                    }],
                    "finished_at": datetime.now().isoformat(timespec="seconds"),
                })


async def kick_off_refresh(metrics: list[str] | None = None,
                           brokers: list[str] | None = None,
                           frequency: str = "yearly") -> str:
    """Start the broker-document-first Dashboard refresh job."""
    await ensure_dashboard_datapoint_tables()
    await ensure_document_refresh_audit_table()
    frequency = "quarterly" if str(frequency).lower() == "quarterly" else "yearly"
    job_id = _new_job()
    mlist = metrics or list(CANONICAL_METRICS.keys())

    seen: set[str] = set()
    blist: list[str] = []
    for broker in (brokers or list(KNOWN_BROKERS)):
        nb = normalize_broker(broker)
        if nb and nb.lower() not in seen:
            blist.append(nb)
            seen.add(nb.lower())

    active_excel_keys = {normalize_broker(x).lower() for x in await get_active_excel_brokers()}
    active_excel = [b for b in blist if b.lower() in active_excel_keys]
    work_brokers = [b for b in blist if b.lower() not in active_excel_keys]

    async with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "status": "queued",
            "strategy": "broker_document_first",
            "total": len(work_brokers),
            "completed": 0,
            "inserted": 0,
            "failed": 0,
            "errors": [],
            "metrics": mlist,
            "brokers": blist,
            "frequency": frequency,
            "active_excel_brokers": active_excel,
            "skipped_excel_brokers": active_excel,
            "allowed_sources": _dashboard_source_options(),
            "freshness_days": _DOCUMENT_FRESHNESS_DAYS,
            "max_pdf_fallbacks": _MAX_PDF_FALLBACKS_PER_BROKER,
            "excel_override_policy": "active Excel brokers skipped by automatic broker-PDF refresh",
            "started_at": datetime.now().isoformat(timespec="seconds"),
        }

    asyncio.create_task(_run_job_guarded(job_id, mlist, blist, frequency))
    if len(_jobs) > 40:
        old = sorted(_jobs.keys(), key=lambda k: _jobs[k].get("started_at", ""))
        for k in old[:-20]:
            _jobs.pop(k, None)
    return job_id
