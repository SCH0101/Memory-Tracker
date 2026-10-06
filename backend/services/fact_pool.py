"""Memory Tracker's domain adapter for the external ES RAG service.

- index_pdf_to_qdrant(): compatibility name for indexing extracted PDF text
- index_chart_captions(): index chart-snip captions as retrievable [CHART] chunks
- retrieve_for_section(): delegate text/table retrieval to ES RAG's /api/rag/query
  contract without adding a default theme gate

Memory Chart does not maintain its own retrieval fallback, vector-store client,
or reranker. All transport is owned by ``es_rag_gateway``.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import uuid
from pathlib import Path

from . import normalize_broker
from .es_rag_gateway import get_es_rag_gateway

logger = logging.getLogger(__name__)

import os as _os

MEMORY_TICKER = "MEMORY"
MEMORY_NICHE_SECTOR = "memory_semiconductors"  # legacy sector tag used by older indexed docs
RAG_CHART_INVENTORY_MAX_UNCAPPED = int(_os.getenv("RAG_CHART_INVENTORY_MAX_UNCAPPED", "2000"))

# ---------------------------------------------------------------------------
# Indexing
# ---------------------------------------------------------------------------

# Bumped on every successful write into the shared fact pool. Lets the
# /api/corpus-stats endpoint report a "last write" timestamp without
# querying the remote corpus. The frontend uses it for updated-data banners.
_last_index_ts: float = 0.0


def _bump_last_index_ts() -> None:
    global _last_index_ts
    import time as _t
    _last_index_ts = _t.time()


def index_excel_model(
    path: str | Path,
    *,
    provider: str = "Memory Tracker Project",
    broker: str = "Memory Tracker Project",
    force: bool = True,
) -> dict:
    """Index an Excel workbook into ES RAG as industry-scope table evidence."""
    p = Path(path)
    client = get_es_rag_gateway()
    result = client.index_excel(
        p,
        ticker="",
        source_rel_path=str(p),
        source="excel_model",
        provider=provider,
        broker=broker,
        scope_type="industry",
        primary_industry="semiconductors",
        primary_niche_sector=MEMORY_NICHE_SECTOR,
        force=force,
    )
    if result.get("ok") is True and not result.get("skipped"):
        _bump_last_index_ts()
    return result


def get_last_index_ts() -> float:
    """Unix timestamp of the most recent successful write into the fact pool
    (or 0.0 if nothing has been indexed by this process). Used by
    /api/corpus-stats. Process-local — restarting the server resets it."""
    return _last_index_ts


def _index_pdf_sync(text: str, filename: str, broker: str, date: str,
                    force: bool = False) -> int:
    client = get_es_rag_gateway()
    res = client.index_text(
        text, filename=filename, ticker=MEMORY_TICKER,
        broker=broker or "", date=date or "",
        report_type="broker_research",
        force=force,
    )
    n = int(res.get("chunks") or 0)
    if res.get("skipped"):
        logger.info("Already indexed via ES RAG API: %s", filename)
    else:
        logger.info("Indexed %s via ES RAG API: %d chunks (force=%s)", filename, n, force)
        if n:
            _bump_last_index_ts()
    return n



async def index_pdf_to_qdrant(text: str, filename: str, broker: str = "",
                              date: str = "", force: bool = False) -> int:
    """Index extracted PDF text through the external ES RAG API.

    Page boundaries must be marked with <<PAGE:n>> so the ES chunker
    stamps the right page_number on every chunk. `force=True` bypasses the
    document dedup guard so a re-upload replaces existing chunks."""
    if not text or not text.strip():
        return 0
    return await asyncio.to_thread(_index_pdf_sync, text, filename, broker, date, force)


def _index_captions_sync(captions: list[dict], filename: str, broker: str, date: str) -> int:
    if not captions:
        return 0
    # Preferred: HTTP /api/rag/index-chart-captions.
    client = get_es_rag_gateway()
    if client is not None:
        try:
            res = client.index_chart_captions(
                captions, filename=filename, ticker=MEMORY_TICKER,
                broker=broker or "", date=date or "",
            )
            n = int(res.get("chunks") or 0)
            if n:
                _bump_last_index_ts()
            return n
        except Exception:
            raise


async def index_chart_captions(captions: list[dict], filename: str,
                               broker: str = "", date: str = "") -> int:
    """Index chart-snip captions as [CHART] chunks (media_type=chart) so the
    snipped exhibits are retrievable by text query alongside the PDF text.

    captions = [{"caption": str, "page": int}, ...]. Idempotent (deterministic
    chunk ids) — safe to call on re-process.
    """
    return await asyncio.to_thread(_index_captions_sync, captions, filename, broker, date)


# ---------------------------------------------------------------------------
# Retrieval (read-only path — all tabs)
# ---------------------------------------------------------------------------

def _date_int_of(meta: dict) -> int:
    try:
        return int(meta.get("date_int") or 0)
    except (TypeError, ValueError):
        return 0


def _date_from_filename(filename: str) -> str:
    """Best-effort date label from source filename when payload metadata is
    clearly wrong. Returns YYYY-MM-DD for 8-digit tokens, YYYY-MM for 6-digit
    tokens, or empty string."""
    m = re.search(r"(?<!\d)(20\d{2})[-_]?(\d{2})[-_]?(\d{2})(?!\d)", filename or "")
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"(?<!\d)(20\d{2})(\d{2})(?!\d)", filename or "")
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return ""


def _safe_date_label(meta: dict) -> str:
    """Date string shown to LLM/UI. If metadata is future-dated, suppress it
    and prefer the source filename token; this prevents impossible citations
    such as an already-ingested 2026-07-01 report on 2026-06-25."""
    raw = meta.get("date", "") or meta.get("quarter", "") or ""
    di = _date_int_of(meta)
    if di:
        from datetime import date as _date
        today_int = _date.today().year * 10000 + _date.today().month * 100 + _date.today().day
        if di > today_int:
            return _date_from_filename(meta.get("filename", "")) or ""
    return raw


_MEMORY_COMPANY_LABELS = {
    "000660",
    "cxmt", "hynix", "kioxia", "micron", "nanya", "samsung", "sandisk",
    "sk hynix", "skhynix", "sk hynix / solidigm", "solidigm", "wdc",
    "western digital", "winbond", "ymtc",
}

_NON_BROKER_LABELS = {
    "ai", "memory", "semis", "semiconductor", "semiconductors",
    "hardware", "reuters", "marketviews",
}

_FILENAME_PROVIDER_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"\barete(?:\s+research)?\b", re.I), "Arete"),
    (re.compile(r"\bbernstein\b", re.I), "Bernstein"),
    (re.compile(r"\bmorgan\s+stanley\b", re.I), "MS"),
    (re.compile(r"\bms\b", re.I), "MS"),
    (re.compile(r"\bj\.?\s*p\.?\s*morgan\b|\bjpm(?:organ)?\b", re.I), "JPM"),
    (re.compile(r"\bbofa\b|\bbank\s+of\s+america\b", re.I), "BofA"),
    (re.compile(r"\bgoldman\s+sachs\b|\bgs\b", re.I), "GS"),
    (re.compile(r"\bciti(?:group)?\b", re.I), "Citi"),
    (re.compile(r"\bubs\b", re.I), "UBS"),
    (re.compile(r"\bjefferies\b", re.I), "Jefferies"),
    (re.compile(r"\bbarclays\b", re.I), "Barclays"),
    (re.compile(r"\bdeutsche\s+bank\b|\bdb\b", re.I), "DB"),
    (re.compile(r"\bhsbc\b", re.I), "HSBC"),
    (re.compile(r"\bclsa\b", re.I), "CLSA"),
    (re.compile(r"\bnomura\b", re.I), "Nomura"),
    (re.compile(r"\bdaiwa\b", re.I), "Daiwa"),
    (re.compile(r"\bmizuho\b", re.I), "Mizuho"),
    (re.compile(r"\bcicc\b", re.I), "CICC"),
    (re.compile(r"\btrendforce\b", re.I), "TrendForce"),
)


def _norm_label(label: str) -> str:
    return re.sub(r"[\s_\-]+", " ", (label or "").strip().lower())


def _is_company_label(label: str) -> bool:
    return _norm_label(label) in _MEMORY_COMPANY_LABELS


def _is_broker_label_candidate(label: str) -> bool:
    clean = (label or "").strip()
    if not clean or clean.upper() == MEMORY_TICKER:
        return False
    if _is_company_label(clean) or _norm_label(clean) in _NON_BROKER_LABELS:
        return False
    # Bloomberg / Reuters-style ticker fragments can leak into ES broker slots.
    if re.fullmatch(r"\d{4,6}(?:\s+[A-Z]{1,3})?", clean):
        return False
    return True


def _provider_from_filename(filename: str) -> str:
    fn = filename or ""
    # Most memory PDFs follow "20260625 [Arete] ..." or "[Bernstein] ...".
    m = re.search(r"\[([^\]]{1,30})\]", fn)
    if m:
        token = m.group(1).strip()
        if _is_broker_label_candidate(token):
            return token
    for pat, provider in _FILENAME_PROVIDER_PATTERNS:
        if pat.search(fn):
            return provider
    return ""


def _derive_source_label(meta: dict) -> str:
    """Best-effort human source label. Chart-caption chunks from ES RAG's
    Docling extraction can carry the company/ticker in the broker slot. Prefer
    a real provider; parse the PDF filename before falling back to ticker-like
    labels so an Arete Kioxia report does not display broker='Kioxia'."""
    label = (meta.get("broker") or "").strip()
    if _is_broker_label_candidate(label):
        return normalize_broker(label)
    fn_provider = _provider_from_filename(meta.get("filename") or "")
    if fn_provider:
        return normalize_broker(fn_provider)
    tk = (meta.get("ticker") or "").strip()
    if _is_broker_label_candidate(tk):
        return normalize_broker(tk)
    fallback = label or tk or ""
    return normalize_broker(fallback) if _is_broker_label_candidate(fallback) else ""


def _page_from_chart_relpath(relpath: str) -> int | None:
    m = re.search(r"(?:_chart_p|_p)(\d+)_", relpath or "", re.IGNORECASE)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


# Upstream chunk annotations that are pure noise inside an LLM prompt.
# [TABLE]/[CHART]/[CHART_DATA] are deliberately KEPT — they're useful structure
# hints that tell the model the following block is tabular / chart-derived.
_WATERMARK_RE = re.compile(
    r"(?im)^.*(?:for the exclusive use of|intended (?:exclusively|solely) for|"
    r"this report is intended (?:exclusively|solely) for)\s*:?\s*[\w.%+-]+\s*@\s*[\w.-]+.*$\n?")
_TABLE_META_RE = re.compile(r"\[TABLE_META\s*\{.*?\}\]", re.DOTALL)
_PARENT_CTX_RE = re.compile(r"\[PARENT_CONTEXT\]\s*")
_PAGE_MARK_RE = re.compile(r"<<PAGE:\d+>>|\[PAGE\s+\d+\]")
_WS_RE = re.compile(r"[ \t]{3,}")


def _clean_chunk_text(text: str) -> str:
    """Strip upstream markers and compliance watermarks so retrieved text
    reads cleanly in an LLM prompt. The shared collection's chunks carry
    [PARENT_CONTEXT] prefixes, inline [TABLE_META {...}] JSON, <<PAGE:n>>
    markers, and per-page 'For the exclusive use of <email>' watermarks — all
    noise that wastes context and can leak into generated output."""
    if not text:
        return ""
    text = _WATERMARK_RE.sub("", text)
    text = _TABLE_META_RE.sub("", text)
    text = _PARENT_CTX_RE.sub("", text)
    text = _PAGE_MARK_RE.sub("", text)
    text = _WS_RE.sub("  ", text)
    return text.strip()


def _to_row(text: str, meta: dict, score: float) -> dict:
    page = meta.get("page_number")
    if page in (None, ""):
        page = meta.get("page")
    try:
        page = int(page) if page not in (None, "") else None
    except (TypeError, ValueError):
        page = None
    return {
        "text": _clean_chunk_text(text),
        "broker": _derive_source_label(meta),
        "date": _safe_date_label(meta),
        "date_int": _date_int_of(meta),
        "filename": meta.get("filename", ""),
        "page": page,
        "content_type": meta.get("content_type", ""),
        "doc_type": meta.get("doc_type", ""),
        "source_doc_type": meta.get("source_doc_type", ""),
        "source_rel_path": meta.get("source_rel_path", ""),
        "citation_url": meta.get("citation_url", ""),
        "citation_markdown": meta.get("citation_markdown", ""),
        "media_type": meta.get("media_type", ""),
        "primary_ticker": meta.get("primary_ticker", ""),
        "primary_company": meta.get("primary_company", ""),
        "primary_industry": meta.get("primary_industry", ""),
        "primary_niche_sector": meta.get("primary_niche_sector", ""),
        "industry_tags": _payload_list(meta.get("industry_tags")),
        "niche_sector_tags": _payload_list(meta.get("niche_sector_tags")),
        "subindustry_tags": _payload_list(meta.get("subindustry_tags")),
        "kpi_tags": _payload_list(meta.get("kpi_tags")),
        "theme_tags": _payload_list(meta.get("theme_tags")),
        "catalyst_tags": _payload_list(meta.get("catalyst_tags")),
        "topic_tags": _payload_list(meta.get("topic_tags")),
        "company_tags": _payload_list(meta.get("company_tags")),
        "period_tags": _payload_list(meta.get("period_tags")),
        "geo_tags": _payload_list(meta.get("geo_tags")),
        "unit_tags": _payload_list(meta.get("unit_tags")),
        "score": float(score),
    }


# Content-type buckets supported by the ES RAG contract.
NUMERIC_CONTENT = ("table", "estimate_table", "valuation_exhibit", "chart_values")
CHART_CONTENT = ("chart",)
NARRATIVE_CONTENT = ("narrative",)

# Default for any retrieval that feeds an LLM: prose narrative + tabular content
# (incl. chart_values, which are the vision-extracted *numbers* underlying a
# chart, in text/table form). Bare chart-caption chunks (media_type=chart)
# are display-only supporting evidence and are NOT included here — Overview /
# Model / Chat must source figures from words and tables, never from a chart
# caption or estimate string. To retrieve charts for DISPLAY, call
# retrieve_rag_charts() / retrieve_charts() which explicitly pass CHART_CONTENT.
PRIMARY_CONTENT = NARRATIVE_CONTENT + NUMERIC_CONTENT
SOURCE_OPTIONS = (
    "broker_research",
    "expert_call",
    "company_presentation",
    "industry_report",
    "earnings_transcript",
    "tables",
    "user_input",
)
_SOURCE_ALIASES = {
    "broker": "broker_research",
    "sellside": "broker_research",
    "broker_research_note": "broker_research",
    "presentation": "company_presentation",
    "company_presentations": "company_presentation",
    "industry_reports": "industry_report",
    "transcript": "earnings_transcript",
    "earnings_call_transcript": "earnings_transcript",
}
_BROKER_SOURCE_TYPES = {
    "broker_research",
    "broker_research_note",
}
_EXPERT_SOURCE_TYPES = {"expert_call", "expert_call_transcript"}
_COMPANY_PRESENTATION_SOURCE_TYPES = {"company_presentation"}
_INDUSTRY_REPORT_SOURCE_TYPES = {"industry_report"}
_TRANSCRIPT_SOURCE_TYPES = {
    "earnings_call_transcript",
    "earnings_transcript",
}
_SOURCE_FAMILY_OPTIONS = {
    "broker_research",
    "expert_call",
    "company_presentation",
    "industry_report",
    "earnings_transcript",
    "user_input",
}


def _memory_scope_query(query: str) -> str:
    """Normalize a Memory Chart query without adding theme or sector gates.

    Gallery and Ask Claude apply their AI theme explicitly. Shared retrieval is
    neutral so Overview, Dashboard, and Model are controlled only by their
    prompts plus optional metadata/source settings.
    """
    return (query or "").strip()


def _normalize_sources(source_options: tuple[str, ...] | list[str] | None) -> set[str]:
    if source_options is None:
        return set(SOURCE_OPTIONS)
    out: set[str] = set()
    for source in source_options:
        key = str(source or "").strip()
        key = _SOURCE_ALIASES.get(key, key)
        if key in SOURCE_OPTIONS:
            out.add(key)
    return out


def _source_filter_for_options(sources: set[str]) -> str:
    if "broker_research" in sources and sources <= {"broker_research", "tables"}:
        return "sellside"
    if "earnings_transcript" in sources and sources <= {"earnings_transcript", "tables"}:
        return "transcripts"
    return "all"


def _user_input_for_options(sources: set[str]) -> bool:
    return "user_input" in sources


def _content_types_for_sources(
    base: tuple[str, ...],
    sources: set[str],
) -> tuple[str, ...]:
    base_set = set(base)
    out: set[str] = set()
    if "tables" in sources:
        out.update(base_set & set(NUMERIC_CONTENT))
    if sources & _SOURCE_FAMILY_OPTIONS:
        out.update(base_set & set(NARRATIVE_CONTENT))
    if base_set & set(CHART_CONTENT):
        out.update(base_set & set(CHART_CONTENT))
    return tuple(x for x in base if x in out)


def _source_family_match(meta: dict, sources: set[str]) -> bool:
    doc_type = str(meta.get("doc_type") or "").strip().lower()
    source_doc_type = str(meta.get("source_doc_type") or "").strip().lower()
    source_doc_subtype = str(meta.get("source_doc_subtype") or "").strip().lower()
    report_type = str(meta.get("report_type") or "").strip().lower()
    source_type = str(meta.get("source_type") or meta.get("document_type") or "").strip().lower()
    type_tags = {doc_type, source_doc_type, source_doc_subtype, report_type, source_type}

    if doc_type == "sellside" and source_doc_type in _BROKER_SOURCE_TYPES:
        return "broker_research" in sources
    if doc_type == "expert_call" and source_doc_type in _EXPERT_SOURCE_TYPES:
        return "expert_call" in sources
    if doc_type == "company_presentation" and source_doc_type in _COMPANY_PRESENTATION_SOURCE_TYPES:
        return "company_presentation" in sources
    if doc_type == "industry_report" and source_doc_type in _INDUSTRY_REPORT_SOURCE_TYPES:
        return "industry_report" in sources
    if doc_type == "transcript" and source_doc_type in _TRANSCRIPT_SOURCE_TYPES:
        return "earnings_transcript" in sources
    if doc_type == "user_input" or source_doc_type == "user_input":
        return "user_input" in sources

    # Backward-compatible fallback for partially stamped older chunks.
    if type_tags & _EXPERT_SOURCE_TYPES:
        return "expert_call" in sources
    if type_tags & _COMPANY_PRESENTATION_SOURCE_TYPES:
        return "company_presentation" in sources
    if type_tags & _INDUSTRY_REPORT_SOURCE_TYPES:
        return "industry_report" in sources
    if type_tags & _TRANSCRIPT_SOURCE_TYPES:
        return "earnings_transcript" in sources
    if type_tags & _BROKER_SOURCE_TYPES:
        return "broker_research" in sources
    return False


def _source_option_match(meta: dict, sources: set[str]) -> bool:
    ctype = str(meta.get("content_type") or "").strip().lower()
    media = str(meta.get("media_type") or "").strip().lower()
    is_table = media == "table" or ctype in set(NUMERIC_CONTENT)
    if is_table:
        if "tables" not in sources:
            return False
        family_sources = sources & _SOURCE_FAMILY_OPTIONS
        return True if not family_sources else _source_family_match(meta, sources)
    return _source_family_match(meta, sources)


async def retrieve_for_section(query: str, k: int = 15, top_n: int = 6,
                               scope: str = "memory",
                               content_types: tuple[str, ...] | None = None,
                               rerank_candidates: int | None = None,
                               source_options: tuple[str, ...] | list[str] | None = None,
                               metadata_filters: dict | None = None) -> list[dict]:
    """Retrieve ES RAG chunks for Memory Chart.

    Text/table retrieval goes through ES RAG's /api/rag/query behavior. Memory
    Chart does not parse, expand, rerank, relax, or fall back locally. The
    shared memory scope intentionally does not hard-filter to the AI theme or
    the legacy memory_semiconductors niche; industry/company/source narrowing
    should come from the user's query and the configured source controls.
    """
    if content_types is None:
        content_types = PRIMARY_CONTENT
    sources = _normalize_sources(source_options)
    content_types = _content_types_for_sources(content_types, sources)
    if not content_types or not sources:
        return []
    keep_n = max(1, int(top_n or k or 1))
    client = get_es_rag_gateway()
    q = _memory_scope_query(query) if scope == "memory" else (query or "").strip()
    query_kwargs = {
        "ticker": "",
        "source_filter": _source_filter_for_options(sources),
        "n_results": max(1, min(20, max(k, keep_n))),
        "user_input": _user_input_for_options(sources),
        "rerank_candidates": rerank_candidates,
    }
    if metadata_filters:
        query_kwargs["metadata_filters"] = metadata_filters
    res = await asyncio.to_thread(client.query, q, **query_kwargs)
    hits = list((res or {}).get("hits") or [])
    allowed = set(content_types or ())
    rows: list[dict] = []
    for h in hits:
        ctype = str(h.get("content_type") or "")
        media = str(h.get("media_type") or "")
        if not _source_option_match(h, sources):
            continue
        if allowed:
            if ctype and ctype not in allowed:
                continue
            if not ctype and media == "chart" and "chart" not in allowed:
                continue
        rows.append(_to_row(h.get("text", ""), h, 0.0))
        if len(rows) >= keep_n:
            break
    return rows


async def retrieve_tables(query: str, k: int = 20, top_n: int = 6,
                          scope: str = "memory",
                          rerank_candidates: int | None = None,
                          source_options: tuple[str, ...] | list[str] | None = None) -> list[dict]:
    """Numeric-only retrieval — pulls [TABLE] / [CHART_DATA] / estimate_table
    chunks from the ES RAG table corpus. Use when the
    caller needs a specific line item (e.g. "DRAM bit demand 2026") instead
    of narrative commentary. Each row carries table_extraction_method +
    table_quality_score in metadata for downstream filtering."""
    return await retrieve_for_section(
        query, k=k, top_n=top_n, scope=scope,
        content_types=NUMERIC_CONTENT,
        rerank_candidates=rerank_candidates,
        source_options=source_options,
    )


async def retrieve_charts(query: str, k: int = 15, top_n: int = 6,
                          scope: str = "memory",
                          rerank_candidates: int | None = None,
                          source_options: tuple[str, ...] | list[str] | None = None) -> list[dict]:
    """Chart-caption-only retrieval — pulls media_type=chart chunks (both
    Docling figure captions from ES-indexed PDFs and memory-site snipped
    captions). Each result's metadata.filename + page_number locates the
    original chart image in the source PDF / memory_chart/data/."""
    return await retrieve_for_section(
        query, k=k, top_n=top_n, scope=scope,
        content_types=CHART_CONTENT,
        rerank_candidates=rerank_candidates,
        source_options=source_options,
    )


# ---------------------------------------------------------------------------
# Displayable RAG charts (with a resolvable image URL)
# ---------------------------------------------------------------------------
# retrieve_charts() above returns media_type=chart *caption* chunks with no
# image pointer. The ~3k reindexed sellside charts that actually carry a JPG are
# stored as media_type=chart with a `chart_file` and API-served image URL.
# Memory rewrites that URL to its authenticated browser-safe proxy.

def _rag_chart_relpath(path: str) -> str:
    """Convert an ES chart-asset URL to the relpath served by Memory."""
    clean = str(path or "").replace("\\", "/")
    for prefix in ("/api/analysis/chart-asset/", "/api/charts/rag-asset/"):
        if prefix in clean:
            return clean.split(prefix, 1)[1].lstrip("/")
    return ""


def rag_chart_relpath_from_payload(filename: str = "", chart_file: str = "",
                                   path: str = "") -> str:
    """Resolve ES chart metadata to a relpath usable by the asset proxy."""
    if path:
        rel = _rag_chart_relpath(path)
        if rel:
            return rel
    return str(chart_file or "").replace("\\", "/").lstrip("/")


_RAG_CHART_SKIP_RE = re.compile(
    r"^\s*(?:\[CHART(?:_DATA)?\]\s*)?\[SKIP:([^\]]+)\]",
    re.IGNORECASE,
)


def rag_chart_skip_reason(payload: dict | None = None, text: str = "") -> str:
    """Return a reason when an ES RAG chart is intentionally non-displayable.

    Equity Swarm's chart curator persists rejected chart candidates as chunks
    like `[CHART] [SKIP:stock_price_history]`. They are useful as audit/cache
    records, but Memory Chart should not display them in Gallery, Overview chart
    citations, or semantic chart search.
    """
    payload = payload or {}
    for key in ("skip_reason", "reject_reason", "rejection_reason", "chart_skip_reason"):
        val = payload.get(key)
        if val:
            return str(val)
    if payload.get("rejected") is True or payload.get("displayable") is False:
        return "rejected"
    for cand in (
        text,
        payload.get("_text", ""),
        payload.get("text", ""),
        payload.get("caption", ""),
        payload.get("chart_type", ""),
        payload.get("section", ""),
    ):
        m = _RAG_CHART_SKIP_RE.match(str(cand or ""))
        if m:
            return m.group(1).strip() or "skip"
    caption = _clean_chart_caption(
        text or payload.get("caption", "") or payload.get("_text", "") or payload.get("text", "")
    )
    return ""


def _clean_chart_caption(text: str) -> str:
    cap = re.sub(r"^\s*(?:\[CHART(?:_DATA)?\]\s*)+", "", (text or "")).strip()
    m = re.search(r"TITLE:\s*(.+)", cap)
    if m:
        return m.group(1).strip()[:200]
    first = next((ln.strip() for ln in cap.splitlines() if ln.strip()), "")
    return first[:200]


def _payload_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    return [p.strip() for p in re.split(r"[,;|]", str(value)) if p.strip()]


_MEMORY_CHART_TEXT_RE = re.compile(
    r"\b(?:"
    r"memory|dram|nand|hbm|hbm3e?|hbm4|ddr4|ddr5|lpddr|gddr|"
    r"flash|ssd|ufs|emmc|wafer|wspm|kwspm|"
    r"bit\s+(?:demand|supply|growth|shipment|output)|"
    r"contract\s+(?:price|pricing)|spot\s+(?:price|pricing)|"
    r"asp|pricing|price|supply|demand|capacity|shipment|shipments|"
    r"inventory|revenue|die|stack|stacks|lta|long\s+term\s+agreement"
    r")\b",
    re.IGNORECASE,
)

_AI_INFRA_CHART_TEXT_RE = re.compile(
    r"\b(?:"
    r"data\s*cent(?:er|re)|datacent(?:er|re)|dc\s+(?:build|capex|power|provisioning|capacity)|"
    r"gw\s+(?:build|capacity|deployment|provisioning|demand)|gigawatt|power\s+(?:demand|capacity|provisioning)|"
    r"hyperscaler|cloud\s+(?:capex|spend|spending|investment)|ai\s+(?:capex|infrastructure|factory|cluster|server|rack|compute)|"
    r"accelerated\s+comput(?:e|ing)|gpu\s+(?:server|cluster|rack|capex)|"
    r"roic|return\s+on\s+invested\s+capital|returns?\s+on\s+(?:capital|investment)|"
    r"fcf|free\s+cash\s+flow|capex\s+intensity|capital\s+intensity"
    r")\b",
    re.IGNORECASE,
)

_AI_THEME_TEXT_RE = re.compile(
    r"\b(?:ai|artificial\s+intelligence|genai|generative\s+ai|machine\s+learning|llm|"
    r"gpu|accelerator|accelerated\s+comput(?:e|ing)|hyperscaler|data\s*cent(?:er|re)|"
    r"datacent(?:er|re)|cloud)\b",
    re.IGNORECASE,
)


def _memory_chart_text_hit(meta: dict) -> bool:
    haystack = " ".join(
        str(meta.get(k) or "")
        for k in (
            "caption", "_text", "text", "exhibit_title", "section", "chart_type",
        )
    )
    return bool(_MEMORY_CHART_TEXT_RE.search(haystack))


def _ai_theme_hit(meta: dict) -> bool:
    tags: list[str] = []
    for k in (
        "theme_tags", "catalyst_tags", "topic_tags", "niche_sector_tags",
        "subindustry_tags", "kpi_tags", "company_tags", "industry_tags",
    ):
        tags.extend(_payload_list(meta.get(k)))
    tagged = " ".join(tags)
    text = " ".join(
        str(meta.get(k) or "")
        for k in (
            "caption", "_text", "text", "exhibit_title", "section", "chart_type",
            "primary_niche_sector", "primary_industry",
        )
    )
    return bool(_AI_THEME_TEXT_RE.search(f"{tagged} {text}"))


def _ai_infra_chart_text_hit(meta: dict) -> bool:
    haystack = " ".join(
        str(meta.get(k) or "")
        for k in (
            "caption", "_text", "text", "exhibit_title", "section", "chart_type",
            "filename",
        )
    )
    return bool(_AI_INFRA_CHART_TEXT_RE.search(haystack))


def _normalized_chart_theme(scope: str = "", theme: str = "") -> str:
    requested = str(theme or "").strip().lower()
    if requested:
        return requested
    # Backward compatibility for older callers that used scope="ai".
    if str(scope or "").strip().lower() == "ai":
        return "ai"
    return ""


def _chart_theme_query(query: str, theme: str) -> str:
    q = (query or "").strip()
    if theme == "ai" and not re.search(
        r"\b(?:ai|artificial intelligence|machine learning|genai|llm)\b",
        q,
        re.IGNORECASE,
    ):
        return f"AI {q}".strip()
    return q


def _matches_chart_theme(meta: dict, theme: str) -> bool:
    # Theme gates are explicit caller choices. Gallery and Ask Claude use the
    # ES `ai` basket by default; Overview/Dashboard/Model do not.
    if str(theme or "").strip().lower() == "ai":
        return _ai_theme_hit(meta)
    return True


def _matches_niche_scope(meta: dict, niche: str) -> bool:
    niche = (niche or "").strip()
    if not niche:
        return True
    if str(meta.get("primary_niche_sector") or "").strip() == niche:
        return True
    return niche in set(_payload_list(meta.get("niche_sector_tags")))


def _matches_industry_scope(meta: dict, industry: str) -> bool:
    industry = (industry or "").strip()
    if not industry:
        return True
    if str(meta.get("primary_industry") or "").strip() == industry:
        return True
    return industry in set(_payload_list(meta.get("industry_tags")))


def _broker_matches(meta: dict, broker: str) -> bool:
    broker = (broker or "").strip()
    if not broker:
        return True
    wanted = normalize_broker(broker).strip().lower()
    actual = normalize_broker(_derive_source_label(meta)).strip().lower()
    if not wanted or not actual:
        return False
    return wanted == actual or wanted in actual or actual in wanted


_QUERY_BROKER_HINTS = (
    (re.compile(r"\barete(?:\s+research)?\b", re.I), "Arete"),
    (re.compile(r"\bmorgan\s+stanley\b|\bms\b", re.I), "MS"),
    (re.compile(r"\bjpm(?:organ)?\b|\bj\.p\.\s*morgan\b", re.I), "JPM"),
    (re.compile(r"\bgoldman\s+sachs\b|\bgs\b", re.I), "GS"),
    (re.compile(r"\bciti(?:group)?\b", re.I), "Citi"),
    (re.compile(r"\bbofa\b|\bbank\s+of\s+america\b", re.I), "BofA"),
    (re.compile(r"\bubs\b", re.I), "UBS"),
    (re.compile(r"\bjefferies\b", re.I), "Jefferies"),
)


def _broker_hint_from_query(query: str) -> str:
    for pat, broker in _QUERY_BROKER_HINTS:
        if pat.search(query or ""):
            return broker
    return ""


def _virtual_rag_chart_id(chunk_key: str) -> int:
    """Return the same negative virtual id used by the gallery for RAG charts.

    The gallery hashes a stable UUID derived from the chunk key into a negative
    integer for edit/delete override routes.
    """
    stable_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, chunk_key or ""))
    h = hashlib.sha1(stable_id.encode("utf-8", errors="ignore")).hexdigest()[:8]
    return -int(h, 16)


def _rag_chart_row_from_payload(
    meta: dict,
    relpath: str,
    score: float = 0.0,
    caption_text: str = "",
) -> dict:
    text = (
        caption_text
        or meta.get("caption")
        or meta.get("_text")
        or meta.get("text")
        or ""
    ).strip()
    chunk_key = str(
        meta.get("_id") or meta.get("chunk_id") or meta.get("id")
        or f"{meta.get('filename','')}::{meta.get('chart_file','')}::{meta.get('page_number') or meta.get('page') or ''}"
    ).strip()
    return {
        "id": _virtual_rag_chart_id(chunk_key) if chunk_key else 0,
        "caption": _clean_chart_caption(text),
        "image_url": "/api/charts/rag-asset/" + relpath,
        "broker": _derive_source_label(meta),
        "date": meta.get("date", "") or meta.get("quarter", ""),
        "date_int": _date_int_of(meta),
        "filename": meta.get("filename", ""),
        "page": (
            meta.get("page_number") or meta.get("page")
            or _page_from_chart_relpath(relpath)
        ),
        "chart_file": meta.get("chart_file", ""),
        "content_type": meta.get("content_type", ""),
        "media_type": meta.get("media_type", ""),
        "doc_type": meta.get("doc_type", ""),
        "source_doc_type": meta.get("source_doc_type", ""),
        "source_rel_path": meta.get("source_rel_path", ""),
        "citation_url": meta.get("citation_url", ""),
        "citation_markdown": meta.get("citation_markdown", ""),
        "primary_ticker": meta.get("primary_ticker", ""),
        "primary_company": meta.get("primary_company", ""),
        "primary_industry": meta.get("primary_industry", ""),
        "primary_niche_sector": meta.get("primary_niche_sector", ""),
        "industry_tags": _payload_list(meta.get("industry_tags")),
        "niche_sector_tags": _payload_list(meta.get("niche_sector_tags")),
        "subindustry_tags": _payload_list(meta.get("subindustry_tags")),
        "kpi_tags": _payload_list(meta.get("kpi_tags")),
        "topic_tags": _payload_list(meta.get("topic_tags")),
        "company_tags": _payload_list(meta.get("company_tags")),
        "period_tags": _payload_list(meta.get("period_tags")),
        "geo_tags": _payload_list(meta.get("geo_tags")),
        "unit_tags": _payload_list(meta.get("unit_tags")),
        "score": float(score or 0.0),
    }


def _retrieve_rag_charts_inventory_sync(
    query: str,
    top_n: int,
    scope: str,
    broker: str = "",
    niche: str = "",
    industry: str = "",
    theme: str = "",
) -> list[dict]:
    """Metadata-first chart inventory. No semantic ranking; used when callers
    want every matching displayable image rather than the ranked top-N sample."""
    wanted_broker = (broker or _broker_hint_from_query(query)).strip()
    active_theme = _normalized_chart_theme(scope, theme)
    limit = RAG_CHART_INVENTORY_MAX_UNCAPPED if top_n <= 0 else top_n
    limit = max(1, min(limit, RAG_CHART_INVENTORY_MAX_UNCAPPED))
    metadata_filters: dict[str, object] = {}
    if wanted_broker:
        metadata_filters["broker"] = wanted_broker
    if niche:
        metadata_filters["niche_sector"] = niche
    if industry:
        metadata_filters["industry"] = industry
    result = get_es_rag_gateway().chart_inventory(
        theme=active_theme,
        metadata_filters=metadata_filters,
    )
    rows: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for hit in result.get("hits") or []:
        if not isinstance(hit, dict):
            continue
        meta = {**dict(hit.get("metadata") or {}), **hit}
        if not _matches_chart_theme(meta, active_theme):
            continue
        if not _matches_niche_scope(meta, niche):
            continue
        if not _matches_industry_scope(meta, industry):
            continue
        if not _broker_matches(meta, wanted_broker):
            continue
        text = str(hit.get("caption") or hit.get("text") or "").strip()
        if rag_chart_skip_reason(meta, text):
            continue
        relpath = rag_chart_relpath_from_payload(
            str(meta.get("filename") or ""),
            str(meta.get("chart_file") or ""),
            str(hit.get("image_url") or meta.get("image_url") or ""),
        )
        if not relpath:
            continue
        key = (
            str(meta.get("filename") or ""),
            str(meta.get("page_number") or meta.get("page") or ""),
            relpath,
        )
        if key in seen:
            continue
        seen.add(key)
        rows.append(_rag_chart_row_from_payload(
            meta,
            relpath,
            score=float(hit.get("score") or 0.0),
            caption_text=text,
        ))
        if len(rows) >= limit:
            break

    rows.sort(key=lambda r: (
        int(r.get("date_int") or 0),
        str(r.get("filename") or "").lower(),
        int(r.get("page") or 0),
        str(r.get("chart_file") or ""),
    ), reverse=True)
    return rows


def _retrieve_rag_charts_sync(
    query: str,
    top_n: int,
    scope: str,
    ranked: bool = True,
    broker: str = "",
    niche: str = "",
    industry: str = "",
    theme: str = "",
) -> list[dict]:
    if not ranked:
        return _retrieve_rag_charts_inventory_sync(
            query, top_n, scope, broker=broker, niche=niche, industry=industry,
            theme=theme,
        )
    active_theme = _normalized_chart_theme(scope, theme)
    q = _chart_theme_query(
        _memory_scope_query(query) if scope == "memory" else query,
        active_theme,
    )
    limit = max(1, int(top_n or 1))
    metadata_filters: dict[str, object] = {"media_type": "chart"}
    if broker:
        metadata_filters["broker"] = broker
    if niche:
        metadata_filters["niche_sector"] = niche
    if industry:
        metadata_filters["industry"] = industry
    result = get_es_rag_gateway().query(
        q,
        ticker="",
        source_filter="charts",
        n_results=max(1, min(100, limit * 3)),
        metadata_filters=metadata_filters,
        theme=active_theme or None,
    )
    hits = result.get("hits") or []
    rows: list[dict] = []

    for h in hits:
        if not isinstance(h, dict):
            continue
        meta = {**dict(h.get("metadata") or {}), **h}
        if not _matches_chart_theme(meta, active_theme):
            continue
        if not _matches_niche_scope(meta, niche):
            continue
        if not _matches_industry_scope(meta, industry):
            continue
        if not _broker_matches(meta, broker):
            continue
        if rag_chart_skip_reason(meta, h.get("caption", "")):
            continue
        relpath = rag_chart_relpath_from_payload(
            str(meta.get("filename") or ""),
            str(meta.get("chart_file") or ""),
            str(h.get("image_url") or meta.get("image_url") or ""),
        )
        if not relpath:
            continue
        chunk_key = str(h.get("chunk_id") or h.get("id") or "")
        if not chunk_key:
            chunk_key = str(meta.get("_id") or meta.get("chunk_id") or "").strip()
        row = _rag_chart_row_from_payload(
            meta,
            relpath,
            score=float(h.get("score", 0.0) or 0.0),
            caption_text=h.get("caption", ""),
        )
        if chunk_key:
            row["id"] = _virtual_rag_chart_id(chunk_key)
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows


async def retrieve_rag_charts(query: str, top_n: int = 6,
                              scope: str = "all", ranked: bool = True,
                              broker: str = "", niche: str = "",
                              industry: str = "", theme: str = "") -> list[dict]:
    """Retrieve sellside CHART images from the RAG WITH a resolvable image_url.

    Each row: {caption, image_url (/api/charts/rag-asset/<relpath>), broker,
    date, date_int, filename, page, chart_file, score}. Newest-relevant first.
    Use for chart DISPLAY (dashboard RAG-chart view, chat inline charts)."""
    return await asyncio.to_thread(
        _retrieve_rag_charts_sync, query, top_n, scope, ranked, broker, niche,
        industry, theme
    )


def index_pasted_chart_to_rag(image_bytes: bytes, *, chart_file: str,
                              caption: str, broker: str = "", date: str = "",
                              page: int = 0) -> dict:
    """Index a pasted chart through ES RAG and expose its proxied image URL.

    Returns {indexed: int, chart_file, image_url} (image_url is the
    Memory-served proxy URL)."""
    out = {"indexed": 0, "chart_file": chart_file, "image_url": ""}
    if not image_bytes or not chart_file:
        return out

    res = get_es_rag_gateway().inject_chart(
        image_bytes,
        filename=chart_file,
        caption=caption or "Pasted memory chart",
        ticker=MEMORY_TICKER,
        broker=broker or "",
        date=date or "",
        page=int(page or 0),
        auto_caption=False,
        source="memory_chart.paste",
    )
    if res.get("ok") and isinstance(res.get("result"), dict):
        result = res["result"]
        out["indexed"] = int(result.get("indexed") or 0)
        out["chart_file"] = str(result.get("chart_file") or chart_file)
        relpath = _rag_chart_relpath(str(result.get("image_url") or ""))
        if not relpath:
            relpath = out["chart_file"]
        out["image_url"] = "/api/charts/rag-asset/" + relpath.lstrip("/")
        if out["indexed"]:
            _bump_last_index_ts()
    elif res.get("reason"):
        logger.warning("ES RAG inject-chart skipped: %s", res["reason"])
    return out

async def index_pasted_chart(image_bytes: bytes, *, chart_file: str, caption: str,
                             broker: str = "", date: str = "", page: int = 0) -> dict:
    """Async wrapper for index_pasted_chart_to_rag (runs in a thread)."""
    return await asyncio.to_thread(
        index_pasted_chart_to_rag, image_bytes,
        chart_file=chart_file, caption=caption, broker=broker, date=date, page=page)
