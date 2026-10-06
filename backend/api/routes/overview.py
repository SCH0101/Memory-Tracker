from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from calendar import monthrange
from contextlib import suppress
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Query

from ...db.connection import get_db
from ...services.rag_settings import (
    get_overview_section_settings,
    get_rerank_candidates,
    get_retrieval_settings,
    get_tab_sources,
    get_tab_prompt,
    render_prompt_template,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/overview", tags=["overview"])

_OVERVIEW_OPENAI_MODEL = (
    os.getenv("OVERVIEW_OPENAI_MODEL")
    or os.getenv("OPENAI_REPORT_MODEL")
    or os.getenv("DASHBOARD_OPENAI_MODEL")
    or "gpt-6-sol"
)

# Per-section retrieval budget (tuned smaller than the defaults in
# fact_pool.retrieve_for_section because Overview fires 10 parallel queries —
# 10 × top_n is what actually lands in the LLM prompt).
#   k      = candidate pool fetched (dense + BM25 → RRF) before rerank
#   top_n  = reranked chunks kept per section
# 10 sections × top_n=4 = 40 chunks max in the PRIMARY SOURCE block.
_OVERVIEW_K = 10
_OVERVIEW_TOP_N = 4

# Recency policy for Overview retrieval. HARD window: chunks dated within
# the last N days only — older / undated chunks are dropped entirely. A
# section with zero recent coverage returns an empty chunk list, and the
# LLM's existing "no relevant data" rule marks that section as N/A in the
# output. We fetch top_n × FETCH_MULT candidates so the swarm's reranker
# has room to surface enough recent material before the local filter kicks
# in.
_OVERVIEW_RECENCY_DAYS = 20
_OVERVIEW_FALLBACK_RECENCY_DAYS = 45
_OVERVIEW_FETCH_MULT = 5
_OVERVIEW_CACHE_VERSION = "esrag_tickerless_v1_20d_closest_broker_v2"
_OVERVIEW_BROKER_TOP_N = 2
_OVERVIEW_BROKER_QUERY_CONCURRENCY = 6

_ARETE_TOPIC_QUERIES = {
    "Pricing":
        "latest Arete Research broker research memory semiconductors DRAM NAND HBM ASP pricing contract spot price outlook",
    "Inventory":
        "latest Arete Research broker research memory semiconductors DRAM NAND inventory DIO days inventory weeks supplier customer channel inventory normalization running on empty memory loss record low inventory",
    "Bit Demand":
        "latest Arete Research broker research memory semiconductors DRAM NAND HBM bit demand AI server mobile PC memory demand forecast",
    "Bit Supply":
        "latest Arete Research broker research memory semiconductors DRAM NAND HBM bit supply sufficiency undersupply oversupply supply growth",
    "HBM / AI Memory":
        "latest Arete Research broker research memory semiconductors HBM demand HBM3E HBM4 AI accelerator GB stacks SK Hynix Samsung Micron",
    "Wafer / Capacity":
        "latest Arete Research broker research memory semiconductors wafer capacity KWSPM wafer starts node migration HBM capacity DRAM NAND",
    "Capex / Strategy":
        "latest Arete Research broker research memory semiconductors capex WFE cleanroom expansion supplier strategy AI memory investment",
}


def _parse_source_date(date_str: str) -> date | None:
    """Parse source dates. YYYY-MM is treated as month-end because some
    indexed PDFs only carry month-level metadata."""
    if not date_str:
        return None
    s = date_str.strip()
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except Exception:
        pass
    m = re.match(r"^(20\d{2})-(\d{2})$", s)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        return date(y, mo, monthrange(y, mo)[1])
    m = re.match(r"^(20\d{2})(\d{2})$", s)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        return date(y, mo, monthrange(y, mo)[1])
    return None


def _date_from_filename(filename: str) -> str:
    m = re.search(r"(?<!\d)(20\d{2})[-_]?(\d{2})[-_]?(\d{2})(?!\d)", filename or "")
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return ""


def _best_chunk_date(chunk: dict) -> str:
    """Prefer exact filename dates when payload metadata is only YYYY-MM."""
    raw = str(chunk.get("date") or "").strip()
    if re.match(r"^20\d{2}-\d{2}$", raw) or re.match(r"^20\d{4}$", raw):
        exact = _date_from_filename(str(chunk.get("filename") or ""))
        if exact:
            return exact
    return raw


def _days_ago(date_str: str) -> int | None:
    """Days between today and source date; None if unparseable or future."""
    s = (date_str or "").strip()
    today = date.today()
    m = re.match(r"^(20\d{2})-(\d{2})$", s) or re.match(r"^(20\d{2})(\d{2})$", s)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        if y == today.year and mo == today.month:
            return (today - date(y, mo, 1)).days

    d = _parse_source_date(s)
    if not d:
        return None
    delta = (today - d).days
    return delta if delta >= 0 else None


def _chunk_recency_broker_key(chunk: dict, idx: int) -> str:
    broker = str(chunk.get("broker") or "").strip()
    if broker:
        return _broker_key(broker)
    filename = str(chunk.get("filename") or "").strip().lower()
    return f"file:{filename}" if filename else f"unknown:{idx}"


def _overview_recent_query(query: str) -> str:
    """Keep recency and source-family policy out of ES's hint parser.

    ISO dates and the phrase ``broker research`` are interpreted by ES as
    exact metadata filters. Overview applies its 20-day policy locally after
    retrieval, while source families are supplied through the API field.
    """
    q = re.sub(r"\bbroker\s+research\b", "research", query or "", flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", q).strip()


def _scoped_arete_topic_query(query: str) -> str:
    """Remove redundant provider/source hints when filename is hard-scoped."""
    q = re.sub(r"\barete\s+research\b", "", query or "", flags=re.IGNORECASE)
    q = re.sub(r"\bbroker\s+research\b", "", q, flags=re.IGNORECASE)
    q = re.sub(r"^\s*latest\b", "", q, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", q).strip()


def _recent_only(
    chunks: list[dict],
    top_n: int,
    window_days: int = _OVERVIEW_RECENCY_DAYS,
    *,
    closest_per_broker: bool = True,
) -> list[dict]:
    """Hard date window: drop any chunk older than `window_days` AND any
    chunk with no parseable date. Preserve the swarm's relevance order
    within the recent set, then slice to top_n. Returns [] when nothing
    recent is available — the LLM is instructed to mark that section N/A."""
    recent: list[tuple[int, int, dict]] = []
    for idx, raw in enumerate(chunks):
        date_label = _best_chunk_date(raw)
        d = _days_ago(date_label)
        if d is None or d > window_days:
            continue
        chunk = dict(raw)
        if date_label and date_label != (chunk.get("date") or ""):
            chunk["date"] = date_label
        recent.append((idx, d, chunk))

    if closest_per_broker:
        closest: dict[str, int] = {}
        for idx, d, chunk in recent:
            key = _chunk_recency_broker_key(chunk, idx)
            if key not in closest or d < closest[key]:
                closest[key] = d
        recent = [
            (idx, d, chunk)
            for idx, d, chunk in recent
            if d == closest.get(_chunk_recency_broker_key(chunk, idx))
        ]

    recent.sort(key=lambda item: (item[1], item[0]))
    return [chunk for _, _, chunk in recent[:top_n]]


def _recent_or_closest(chunks: list[dict], top_n: int) -> list[dict]:
    """Prefer 20-day evidence; fill an otherwise empty section up to 45 days."""
    recent = _recent_only(chunks, top_n, _OVERVIEW_RECENCY_DAYS)
    if recent:
        return recent
    return _recent_only(chunks, top_n, _OVERVIEW_FALLBACK_RECENCY_DAYS)

_cache: dict[str, dict] = {"industry": {}, "company": {}}

# Retrieval queries per overview section — run against the equity swarm fact pool
_SECTION_QUERIES = {
    "Demand & Supply Summary":
        "latest broker research memory semiconductors DRAM NAND HBM supply demand balance shortage sufficiency pricing capacity TSMC foundry CoWoS outlook",
    "Bit Demand & Memory Content":
        "latest broker research memory semiconductors DRAM NAND HBM bit demand AI server mobile PC memory content per device GB TB forecast",
    "Inventory":
        "latest broker research memory semiconductors DRAM NAND inventory weeks of supply DIO supplier customer channel inventory normalization",
    "Contract Pricing / ASP":
        "latest broker research memory semiconductors DRAM NAND contract price ASP QoQ YoY trend DDR4 DDR5 LPDDR contract pricing forecast",
    "Spot Pricing & Spot-Contract Spread":
        "latest broker research memory semiconductors DRAM NAND spot price trend spot versus contract price spread",
    "HBM Demand & Capacity":
        "latest broker research memory semiconductors HBM demand TAM GB content stacks wafer equivalent capacity HBM3E HBM4 roadmap",
    "Bit Supply":
        "latest broker research memory semiconductors DRAM NAND HBM bit supply growth supply discipline sufficiency wafer supply outlook",
    "Foundry / CoWoS Capacity":
        "latest broker research AI memory semiconductors TSMC foundry capacity CoWoS advanced packaging wafers KWSPM leading edge capacity GPU ASIC",
    "Wafer Starts & Node Capacity":
        "latest broker research memory semiconductors wafer starts capacity KWSPM WSPM node migration technology capacity additions",
    "Capex / WFE / Cleanroom Expansion":
        "latest broker research memory semiconductors capex WFE wafer fab equipment spending cleanroom expansion capacity investment",
    "Utilization / Yield / Cycle-Time":
        "latest broker research memory semiconductors DRAM NAND HBM fab utilization yield die penalty cycle time process migration capacity ramp",
}


def _effective_section_queries() -> dict[str, str]:
    out: dict[str, str] = {}
    for topic, default_query in _SECTION_QUERIES.items():
        override = get_overview_section_settings(topic).get("query", "").strip()
        out[topic] = override or default_query
    return out


def _effective_section_sources() -> dict[str, list[str]]:
    return {
        topic: list(get_overview_section_settings(topic).get("sources") or get_tab_sources("overview"))
        for topic in _SECTION_QUERIES
    }


async def _gather_retrieved_context() -> str:
    """Per-section fact pool retrieval (reranked, recency-weighted). Returns prompt block."""
    import asyncio
    from ...services.fact_pool import retrieve_for_section

    retrieval = get_retrieval_settings()
    chunks_get = int(retrieval.get("chunks_get") or _OVERVIEW_K)
    rerank_candidates = get_rerank_candidates(retrieval, _OVERVIEW_TOP_N)
    topics = list(_effective_section_queries().items())
    section_sources = _effective_section_sources()
    fetch_n = chunks_get * _OVERVIEW_FETCH_MULT
    keep_n = min(chunks_get, _OVERVIEW_TOP_N)
    try:
        results = await asyncio.gather(
            *(
                retrieve_for_section(
                    _overview_recent_query(q),
                    k=chunks_get,
                    top_n=fetch_n,
                    rerank_candidates=rerank_candidates,
                    source_options=section_sources.get(topic),
                )
                for topic, q in topics
            ),
            return_exceptions=True,
        )
        # Apply the hard recency window per section: drop anything older or undated.
        # Sections with no recent coverage end up empty; the LLM marks them N/A.
        results = [
            _recent_or_closest(r, keep_n) if isinstance(r, list) else r
            for r in results
        ]
    except Exception as exc:
        logger.warning("Fact pool retrieval failed: %s", exc)
        return ""

    blocks = []
    for (topic, _), hits in zip(topics, results):
        if isinstance(hits, Exception) or not hits:
            continue
        lines = [f"\n### {topic}"]
        for h in hits:
            src = f"({h['broker']} {h['date']})" if h.get("broker") else f"({h.get('date','')})"
            txt = (h.get("text") or "").replace("\n", " ")[:600]
            lines.append(f"  - {src} {txt}")
        blocks.append("\n".join(lines))
    if not blocks:
        return ""
    return (
        "\n\nRETRIEVED RESEARCH CONTEXT from the fact pool "
        "(reranked by relevance; sorted newest-first — base commentary on the LATEST views, "
        "use older data only for trend context):\n" + "\n".join(blocks)
    )


async def _gather_retrieved_context_with_sources() -> tuple[str, dict]:
    """Citation-aware per-section fact-pool retrieval for overview prompts."""
    import asyncio
    from ...services.fact_pool import retrieve_for_section

    retrieval = get_retrieval_settings()
    chunks_get = int(retrieval.get("chunks_get") or _OVERVIEW_K)
    rerank_candidates = get_rerank_candidates(retrieval, _OVERVIEW_TOP_N)
    topics = list(_effective_section_queries().items())
    section_sources = _effective_section_sources()
    fetch_n = chunks_get * _OVERVIEW_FETCH_MULT
    keep_n = min(chunks_get, _OVERVIEW_TOP_N)
    try:
        results = await asyncio.gather(
            *(
                retrieve_for_section(
                    _overview_recent_query(q),
                    k=chunks_get,
                    top_n=fetch_n,
                    rerank_candidates=rerank_candidates,
                    source_options=section_sources.get(topic),
                )
                for topic, q in topics
            ),
            return_exceptions=True,
        )
        # Apply the hard recency window per section: drop anything older or undated.
        # Sections with no recent coverage end up empty; the LLM marks them N/A.
        results = [
            _recent_or_closest(r, keep_n) if isinstance(r, list) else r
            for r in results
        ]
    except Exception as exc:
        logger.warning("Fact pool retrieval failed: %s", exc)
        return "", {}

    blocks: list[str] = []
    source_index: dict[str, dict] = {}
    src_i = 1
    for (topic, _), hits in zip(topics, results):
        if isinstance(hits, Exception) or not hits:
            continue
        lines = [f"\n### {topic}"]
        for h in hits:
            src_id = f"S{src_i}"
            src_i += 1
            page = h.get("page") or 0
            source_index[src_id] = {
                "filename": h.get("filename", ""),
                "page": page,
                "broker": h.get("broker", ""),
                "date": h.get("date", ""),
                "content_type": h.get("content_type", ""),
            }
            src = f"({h.get('broker', '')} {h.get('date', '')})".strip()
            ref = f"[src:{src_id}]"
            if h.get("filename"):
                ref += f" {h.get('filename')} P.{page}" if page else f" {h.get('filename')}"
            txt = (h.get("text") or "").replace("\n", " ")[:600]
            lines.append(f"  - {src} {ref} {txt}")
        blocks.append("\n".join(lines))
    if not blocks:
        return "", {}
    return (
        "\n\nRETRIEVED RESEARCH CONTEXT from the fact pool "
        "(reranked by relevance; sorted newest-first; cite [src:S#] when using these excerpts):\n"
        + "\n".join(blocks)
    ), source_index


def _broker_key(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "", (name or "").lower())
    aliases = {
        "goldmansachs": "gs",
        "jpmorgan": "jpm",
        "jpmorganchase": "jpm",
        "morganstanley": "ms",
        "bankofamerica": "bofa",
        "boaml": "bofa",
        "deutschebank": "db",
        "areteresearch": "arete",
    }
    return aliases.get(s, s)


async def _available_brokers() -> list[str]:
    """Brokers to explicitly cover in Overview.

    Start with the dashboard's canonical sellside list, then add any brokers
    present in the local chart gallery so new/less common sources are not
    silently omitted from the daily narrative.
    """
    seen: dict[str, str] = {}
    try:
        from ...services.dashboard_refresh import KNOWN_BROKERS
        for b in KNOWN_BROKERS:
            key = _broker_key(b)
            if key:
                seen.setdefault(key, b)
    except Exception as exc:
        logger.debug("Dashboard broker list unavailable for Overview: %s", exc)

    try:
        db = await get_db()
        rows = await db.execute_fetchall(
            "SELECT DISTINCT broker FROM charts WHERE broker != '' ORDER BY broker"
        )
        for r in rows:
            b = str(r["broker"] or "").strip()
            key = _broker_key(b)
            if key:
                seen.setdefault(key, b)
    except Exception as exc:
        logger.debug("Local chart broker discovery unavailable for Overview: %s", exc)

    return list(seen.values())


async def _gather_all_broker_context_with_sources(
    src_start: int,
) -> tuple[str, dict, dict, int]:
    """Pull a small recent evidence floor for every available broker.

    The section-level retrieval is relevance-first and intentionally compact;
    this block prevents one or two highly ranked brokers from crowding out the
    rest of the sellside set during summarization.
    """
    from ...services.fact_pool import retrieve_for_section

    brokers = await _available_brokers()
    if not brokers:
        return "", {}, {"requested": [], "included": [], "missing": []}, src_start

    sem = asyncio.Semaphore(_OVERVIEW_BROKER_QUERY_CONCURRENCY)
    fetch_n = _OVERVIEW_BROKER_TOP_N * _OVERVIEW_FETCH_MULT
    retrieval = get_retrieval_settings()
    rerank_candidates = get_rerank_candidates(retrieval, _OVERVIEW_TOP_N)
    source_options = get_tab_sources("overview")

    async def fetch_one(broker: str) -> tuple[str, list[dict]]:
        query = (
            f"{broker} latest broker research memory semiconductors "
            "DRAM NAND HBM supply demand pricing inventory capex capacity forecast"
        )
        query = _overview_recent_query(query)
        try:
            async with sem:
                hits = await retrieve_for_section(
                    query,
                    k=max(_OVERVIEW_K, 16),
                    top_n=fetch_n,
                    scope="memory",
                    rerank_candidates=rerank_candidates,
                    source_options=source_options,
                )
            hits = _recent_only(hits, _OVERVIEW_BROKER_TOP_N)
            return broker, hits
        except Exception as exc:
            logger.debug("Broker coverage retrieval failed for %s: %s", broker, exc)
            return broker, []

    try:
        results = await asyncio.gather(
            *(fetch_one(b) for b in brokers),
            return_exceptions=True,
        )
    except Exception as exc:
        logger.warning("All-broker Overview retrieval failed: %s", exc)
        return "", {}, {"requested": brokers, "included": [], "missing": brokers}, src_start

    blocks: list[str] = [
        "\n\nALL-BROKER COVERAGE BLOCK "
        "(coverage floor; cite [src:S#] when using these excerpts):",
        "Requested brokers: " + ", ".join(brokers),
    ]
    source_index: dict[str, dict] = {}
    included: list[str] = []
    missing: list[str] = []
    src_i = src_start

    for result in results:
        if isinstance(result, Exception):
            logger.debug("Broker coverage retrieval failed: %s", result)
            continue
        broker, hits = result
        lines = [f"\n### {broker}"]
        if not hits:
            missing.append(broker)
            lines.append("  - N/A - no recent evidence retrieved")
            blocks.append("\n".join(lines))
            continue
        included.append(broker)
        for h in hits:
            src_id, src_i = _add_source_ref(source_index, src_i, h)
            page = h.get("page") or 0
            ref = f"[src:{src_id}]"
            if h.get("filename"):
                ref += f" {h.get('filename')} P.{page}" if page else f" {h.get('filename')}"
            src = f"({h.get('broker') or broker} {h.get('date', '')})".strip()
            txt = (h.get("text") or "").replace("\n", " ")[:650]
            lines.append(f"  - {src} {ref} {txt}")
        blocks.append("\n".join(lines))

    coverage = {"requested": brokers, "included": included, "missing": missing}
    if included:
        blocks.insert(2, "Brokers with recent evidence: " + ", ".join(included))
    if missing:
        blocks.insert(3 if included else 2, "No recent evidence retrieved: " + ", ".join(missing))

    return "\n".join(blocks), source_index, coverage, src_i


def _is_arete_hit(hit: dict) -> bool:
    """True only when the source document itself is Arete.

    Do not inspect chunk text here. Non-Arete reports can contain strings like
    "Source: Arete Research" or discuss Arete, and those must not appear in the
    dedicated ARETE Latest section.
    """
    broker = str(hit.get("broker") or "").strip().lower()
    filename = str(hit.get("filename") or "").strip().lower()
    return broker == "arete" or "arete" in filename


_ARETE_TOPIC_TERMS = {
    "Pricing": (
        "price", "pricing", "asp", "contract", "spot", "premium",
    ),
    "Inventory": (
        "inventory", "inventories", "dio", "days inventory", "weeks",
        "channel", "stock", "normalization", "normalisation", "overhang",
        "digestion", "record low",
    ),
    "Bit Demand": (
        "bit demand", "demand growth", "memory content", "shipment",
        "server demand", "pc demand", "mobile demand",
    ),
    "Bit Supply": (
        "bit supply", "supply growth", "supply discipline", "undersupply",
        "oversupply", "sufficiency",
    ),
    "HBM / AI Memory": (
        "hbm", "ai memory", "high bandwidth memory",
    ),
    "Wafer / Capacity": (
        "wafer", "capacity", "wspm", "kwspm", "fab", "cleanroom",
    ),
    "Capex / Strategy": (
        "capex", "wfe", "investment", "strategy", "strategic",
        "roadmap", "expansion",
    ),
}

_ARETE_INVENTORY_STRONG_TERMS = (
    "dio", "days inventory", " days", "weeks", "inventory levels",
    "supplier inventory", "channel inventory", "customer inventory",
    "record low", "below its", "below the", "declined", "decline",
    "inventory cycle", "shortage",
)

_ARETE_MEMORY_FILENAME_TERMS = (
    "semi", "memory", "dram", "nand", "hbm", "hynix", "micron",
    "samsung", "inventory", "ai hardware",
)


async def _latest_arete_source() -> dict | None:
    """Return the newest recent Arete document relevant to semis/memory."""
    from ...services.es_rag_gateway import get_es_rag_gateway

    floor = (date.today() - timedelta(days=_OVERVIEW_RECENCY_DAYS)).isoformat()
    client = get_es_rag_gateway()
    payload = await asyncio.to_thread(
        client.sources,
        broker="Arete",
        date_after=floor,
        limit=100,
    )
    candidates: list[dict] = []
    for raw in payload.get("sources") or []:
        filename = str(raw.get("filename") or "")
        days = _days_ago(str(raw.get("date") or ""))
        if days is None or days > _OVERVIEW_RECENCY_DAYS:
            continue
        if not any(term in filename.casefold() for term in _ARETE_MEMORY_FILENAME_TERMS):
            continue
        candidates.append(dict(raw))
    if not candidates:
        return None

    def rank(source: dict) -> tuple[str, bool]:
        filename = str(source.get("filename") or "")
        is_canonical = re.search(r"__[0-9a-f]{8,}(?=\.pdf$)", filename, re.IGNORECASE) is None
        return str(source.get("date") or ""), is_canonical

    return max(candidates, key=rank)


def _topic_signal(hit: dict, topic: str) -> bool:
    terms = _ARETE_TOPIC_TERMS.get(topic)
    if not terms:
        return True
    text = (hit.get("text") or "").lower()
    if len(text.strip()) < 80:
        return False
    # Reject table/footer fragments such as "Source: Company data, Arete..."
    # unless they also carry a real topic phrase or numeric inventory context.
    if text.strip().startswith("source") and not any(t in text for t in terms):
        return False
    if topic == "Inventory":
        return any(t in text for t in _ARETE_INVENTORY_STRONG_TERMS)
    return any(t in text for t in terms)


def _add_source_ref(source_index: dict[str, dict], src_i: int, hit: dict) -> tuple[str, int]:
    src_id = f"S{src_i}"
    page = hit.get("page") or 0
    source_index[src_id] = {
        "filename": hit.get("filename", ""),
        "page": page,
        "broker": hit.get("broker", ""),
        "date": hit.get("date", ""),
        "content_type": hit.get("content_type", ""),
    }
    return src_id, src_i + 1


def _source_ref_is_arete(ref: dict | None) -> bool:
    if not ref:
        return False
    broker = str(ref.get("broker") or "").strip().lower()
    filename = str(ref.get("filename") or "").strip().lower()
    return broker == "arete" or "arete" in filename


_SRC_REF_RE = re.compile(r"\[src:(S\d+)\]")
_CHART_REF_RE = re.compile(r"\[id:(\d+)\]")


def _sanitize_arete_latest_citations(
    result: dict,
    source_index: dict[str, dict],
    chart_index: dict[str, dict],
) -> None:
    """`arete_latest` is a dedicated Arete block. It must never display UBS
    or other-provider citation chips, even if the model accidentally cites a
    general source/chart ID. Keep bullets only when they retain at least one
    Arete source marker, otherwise mark the card as N/A."""
    arete = result.get("arete_latest")
    if not isinstance(arete, dict):
        return
    cards = arete.get("cards")
    if not isinstance(cards, list):
        return

    for card in cards:
        if not isinstance(card, dict):
            continue
        bullets = card.get("bullets") or []
        if not isinstance(bullets, list):
            bullets = [str(bullets)]
        clean_bullets: list[str] = []
        for raw in bullets:
            text = str(raw or "").strip()
            if not text:
                continue
            if text.upper().startswith("N/A"):
                clean_bullets.append(text)
                continue

            def src_repl(m: re.Match) -> str:
                sid = m.group(1)
                return m.group(0) if _source_ref_is_arete(source_index.get(sid)) else ""

            def chart_repl(m: re.Match) -> str:
                cid = m.group(1)
                return m.group(0) if _source_ref_is_arete(chart_index.get(cid)) else ""

            text = _SRC_REF_RE.sub(src_repl, text)
            text = _CHART_REF_RE.sub(chart_repl, text)
            has_arete_src = any(
                _source_ref_is_arete(source_index.get(m.group(1)))
                for m in _SRC_REF_RE.finditer(text)
            )
            has_arete_chart = any(
                _source_ref_is_arete(chart_index.get(m.group(1)))
                for m in _CHART_REF_RE.finditer(text)
            )
            if has_arete_src or has_arete_chart:
                clean_bullets.append(re.sub(r"\s{2,}", " ", text).strip())

        card["bullets"] = clean_bullets or ["N/A — no Arete evidence retrieved"]
    arete["cards"] = [
        card
        for card in cards
        if isinstance(card, dict)
        and any(
            str(bullet or "").strip()
            and not str(bullet or "").strip().upper().startswith("N/A")
            for bullet in (card.get("bullets") or [])
        )
    ]
    arete["unavailable_bullets"] = (
        [] if arete["cards"] else ["No recent Arete evidence retrieved."]
    )


async def _gather_arete_context_with_sources(src_start: int) -> tuple[str, dict, int]:
    """Pull Arete's latest memory-semiconductor evidence by topic.

    This is intentionally separate from the general per-section retrieval so
    Arete cannot be crowded out by broader broker diversity caps. The prompt
    uses this block both for the dedicated Arete Latest section and for Arete
    bullets inside the normal overview sections where relevant.
    """
    import asyncio
    from ...services.fact_pool import retrieve_for_section

    latest_source = await _latest_arete_source()
    if not latest_source:
        return "", {}, src_start
    latest_filename = str(latest_source.get("filename") or "").strip()
    if not latest_filename:
        return "", {}, src_start

    topics = list(_ARETE_TOPIC_QUERIES.items())
    retrieval = get_retrieval_settings()
    rerank_candidates = get_rerank_candidates(retrieval, _OVERVIEW_TOP_N)
    source_options = get_tab_sources("overview")
    try:
        results = await asyncio.gather(
            *(
                retrieve_for_section(
                    _scoped_arete_topic_query(query),
                    k=max(_OVERVIEW_K, 24),
                    top_n=10,
                    rerank_candidates=rerank_candidates,
                    source_options=source_options,
                    metadata_filters={"filename": {"$eq": latest_filename}},
                )
                for _, query in topics
            ),
            return_exceptions=True,
        )
    except Exception as exc:
        logger.warning("Arete retrieval failed: %s", exc)
        return "", {}, src_start

    blocks: list[str] = []
    source_index: dict[str, dict] = {}
    src_i = src_start
    all_arete: list[dict] = []
    topic_hits: dict[str, list[dict]] = {}

    for (topic, _), hits in zip(topics, results):
        if isinstance(hits, Exception) or not hits:
            topic_hits[topic] = []
            continue
        arete_hits = [h for h in hits if _is_arete_hit(h) and _topic_signal(h, topic)]
        arete_hits = _recent_only(arete_hits, top_n=10)
        arete_hits.sort(key=lambda h: (int(h.get("date_int") or 0), float(h.get("score") or 0)), reverse=True)
        topic_hits[topic] = arete_hits[:3]
        all_arete.extend(arete_hits)

    if not all_arete:
        return "", {}, src_start

    all_arete.sort(key=lambda h: (int(h.get("date_int") or 0), float(h.get("score") or 0)), reverse=True)
    latest_label = latest_filename
    latest_date = str(latest_source.get("date") or "")

    blocks.append(
        "\n\nARETE LATEST SOURCE BLOCK "
        "(high-priority; use for `arete_latest` and include Arete views inside normal sections when topic-relevant):"
    )
    blocks.append(f"Latest detected Arete source: {latest_label} {latest_date}".strip())

    for topic, hits in topic_hits.items():
        lines = [f"\n### Arete — {topic}"]
        if not hits:
            lines.append("  - N/A — no Arete evidence retrieved for this topic")
            blocks.append("\n".join(lines))
            continue
        for h in hits:
            src_id, src_i = _add_source_ref(source_index, src_i, h)
            page = h.get("page") or 0
            ref = f"[src:{src_id}]"
            if h.get("filename"):
                ref += f" {h.get('filename')} P.{page}" if page else f" {h.get('filename')}"
            src = f"(Arete {h.get('date', '')})".strip()
            txt = (h.get("text") or "").replace("\n", " ")[:700]
            lines.append(f"  - {src} {ref} {txt}")
        blocks.append("\n".join(lines))

    return "\n".join(blocks), source_index, src_i


async def _gather_datapoints_summary() -> str:
    """Latest datapoints per (metric, broker) from the datapoints table. Returns prompt block."""
    try:
        from ...services.datapoints import ensure_datapoints_table
        await ensure_datapoints_table()
        db = await get_db()
        rows = await db.execute_fetchall(
            """SELECT segment, metric, period, value, unit, broker, source_date
               FROM datapoints
               ORDER BY segment, metric, broker, period_int"""
        )
    except Exception as exc:
        logger.warning("Datapoints summary failed: %s", exc)
        return ""
    if not rows:
        return ""
    lines = ["\n\nSTRUCTURED DATAPOINTS (unit-normalized, from all reports):"]
    cur_key = None
    for r in rows:
        key = (r["segment"], r["metric"], r["broker"])
        if key != cur_key:
            cur_key = key
            lines.append(f"  {r['segment']} / {r['metric']} ({r['broker']}, {r['unit']}):")
        lines.append(f"    {r['period']} = {r['value']:g}")
    block = "\n".join(lines)
    return block[:12000]


async def _load_db_cache(key: str) -> dict | None:
    db = await get_db()
    cur = await db.execute(
        "SELECT data, ts FROM overview_cache WHERE key = ?", (key,)
    )
    row = await cur.fetchone()
    if row:
        try:
            return {"data": json.loads(row["data"]), "_ts": row["ts"]}
        except Exception:
            pass
    return None


async def _save_db_cache(key: str, data: dict):
    db = await get_db()
    await db.execute(
        """INSERT OR REPLACE INTO overview_cache (key, data, ts)
           VALUES (?, ?, ?)""",
        (key, json.dumps(data), time.time()),
    )
    await db.commit()


async def _ensure_cache_table():
    db = await get_db()
    await db.execute(
        """CREATE TABLE IF NOT EXISTS overview_cache (
               key TEXT PRIMARY KEY,
               data TEXT NOT NULL,
               ts REAL NOT NULL
           )"""
    )
    await db.commit()


_CHART_CATALOGUE_CAP = 200  # max charts surfaced to the LLM in the catalogue


async def _gather_chart_catalogue_from_rag() -> list[dict]:
    """Query ES RAG chart inventory for memory-semiconductor charts.

    Overview does not force the Gallery/Ask-OpenAI AI theme; niche/source
    filters are caller-controlled metadata narrowing only.
    """
    from ...services.fact_pool import retrieve_rag_charts

    try:
        results = await retrieve_rag_charts(
            _overview_recent_query("memory semiconductors"),
            top_n=0,
            scope="memory",
            ranked=False,
            niche="memory_semiconductors",
        )
    except Exception as exc:
        logger.warning("RAG chart catalogue inventory retrieval failed: %s", exc)
        return []

    results = _recent_only(
        list(results or []),
        top_n=max(len(results or []), _CHART_CATALOGUE_CAP),
        closest_per_broker=True,
    )

    out: list[dict] = []
    seen: set[tuple[str, str, str, str]] = set()
    for c in results:
        if not c:
            continue
        date_label = _best_chunk_date(c)
        d = _days_ago(date_label)
        if d is None or d > _OVERVIEW_RECENCY_DAYS:
            continue
        caption = (c.get("caption") or "").strip()
        image_url = c.get("image_url", "") or ""
        chart_file = image_url.split("/api/charts/rag-asset/", 1)[-1] if image_url else c.get("chart_file", "")
        key = (
            c.get("filename", "") or "",
            str(c.get("page") or ""),
            chart_file or "",
            caption[:80],
        )
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "chunk_id": c.get("chart_file", "") or "|".join(key),
            "tag": caption[:120],
            "caption": caption[:280],
            "broker": c.get("broker", "") or "",
            "date": date_label or c.get("date", "") or "",
            "filename": c.get("filename", "") or "",
            "page": c.get("page") or 0,
            "chart_file": chart_file,
        })
    out.sort(key=lambda c: c["date"], reverse=True)
    logger.info("RAG chart catalogue: retrieved %d recent memory-niche charts via ES chart inventory", len(out))
    return out[:_CHART_CATALOGUE_CAP]


def _build_chart_catalogue_block_and_index(
    rag_charts: list[dict],
) -> tuple[str, dict]:
    """Build the prompt's SUPPORTING VISUAL CATALOGUE block + the
    `_chart_index` mapping returned in the LLM response. Sequential `[id:N]`
    citations refer to the same N in both — the order is the scroll order
    (date-desc), so the LLM can cite by id and the frontend resolves the
    chip via response['_chart_index'][N].
    """
    by_broker: dict[str, list[tuple[int, dict]]] = {}
    chart_index: dict[str, dict] = {}
    for i, c in enumerate(rag_charts, 1):
        cid = str(i)
        chart_index[cid] = {
            "tag": c.get("tag", ""),
            "caption": c.get("caption", ""),
            "broker": c.get("broker", ""),
            "date": c.get("date", ""),
            "filename": c.get("filename", ""),
            "page": c.get("page", 0),
            "chart_file": c.get("chart_file", ""),
            "chunk_id": c.get("chunk_id", ""),
        }
        by_broker.setdefault(c.get("broker") or "Unknown", []).append((i, c))
    lines: list[str] = []
    for broker in sorted(by_broker.keys()):
        lines.append(f"\n## {broker}")
        for cid, c in by_broker[broker]:
            label = c.get("tag") or c.get("caption", "")[:120]
            date_str = c.get("date", "")
            lines.append(f"  - [id:{cid}] {date_str} | {label}")
    return "\n".join(lines), chart_index


async def _gather_chart_data() -> list[dict]:
    """LEGACY local-DB chart fetch. Kept for `/api/overview/chartindex`
    (Dashboard click-to-source still uses it) but the prompt path is now
    `_gather_chart_catalogue_from_rag()` -> ES RAG chart retrieval."""
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT ch.id, ch.tag, ch.caption, ch.estimates, ch.broker, ch.chart_date,
                  sc.name as subcategory_name, cat.name as category_name
           FROM charts ch
           LEFT JOIN subcategories sc ON ch.subcategory_id = sc.id
           LEFT JOIN categories cat ON sc.category_id = cat.id
           WHERE ch.quality_status = 'approved'
           ORDER BY cat.name, sc.name, ch.chart_date_int DESC"""
    )
    charts = [dict(r) for r in rows]

    chart_ids = [c["id"] for c in charts]
    if chart_ids:
        placeholders = ",".join("?" * len(chart_ids))
        comp_rows = await db.execute_fetchall(
            f"""SELECT cc.chart_id, co.name
                FROM chart_companies cc
                JOIN companies co ON cc.company_id = co.id
                WHERE cc.chart_id IN ({placeholders})""",
            chart_ids,
        )
        comp_map: dict[int, list[str]] = {}
        for cr in comp_rows:
            comp_map.setdefault(cr["chart_id"], []).append(cr["name"])
        for c in charts:
            c["companies"] = comp_map.get(c["id"], ["General"])
    return charts


def _build_industry_prompt(catalogue_block: str, extra_context: str = "") -> str:
    cur_year = date.today().year
    data_block = catalogue_block

    return f"""You are a buyside semiconductor memory analyst summarizing the latest sellside research views.

CURRENT YEAR: {cur_year}
TODAY: {date.today().isoformat()}

## PRIMARY SOURCE — sellside text & tables (use these for ALL figures and commentary)
{extra_context}

## SUPPORTING VISUAL CATALOGUE — chart thumbnails available in the dashboard (REFERENCE ONLY, do NOT lift figures from these)
Each entry is a chart available to display alongside your answer. Use [id:N] to cite a chart when it illustrates a point, but do NOT extract numbers from a chart's tag/estimate/caption — those are summaries, not the source of truth. All figures must come from the PRIMARY SOURCE block above.

{data_block}

TASK: Produce a structured INDUSTRY OVERVIEW summary in JSON format using the 10 analytical sections below.

Structure the output as:
{{
  "title": "Memory Industry Overview",
  "last_updated": "{date.today().isoformat()}",
  "overall_summary": "A 3-5 sentence executive summary consolidating the key takeaways across ALL sections below — cover the most important supply/demand dynamics, pricing trends, capacity changes, and HBM outlook in one concise paragraph",
  "bull_case": ["2-4 bullet points: factors supporting upside for memory industry — e.g. tightening supply, rising ASPs, HBM demand acceleration, capex discipline"],
  "bear_case": ["2-4 bullet points: factors representing downside risk — e.g. oversupply risk, demand weakness, pricing pressure, China capacity additions"],
  "uncertain": ["2-4 bullet points: key swing factors or debates where sellside views diverge — e.g. inventory normalization timing, AI demand sustainability, DDR5 transition pace"],
  "arete_latest": {{
    "title": "ARETE Latest PDF Summary",
    "source": "Latest Arete filename/date used, or N/A",
    "cards": [
      {{"topic": "Pricing", "bullets": ["1-2 bullets with Arete's latest pricing view and numbers where available [src:S#]"]}},
      {{"topic": "Inventory", "bullets": ["1-2 bullets with Arete's latest supplier/customer/channel inventory view [src:S#]"]}},
      {{"topic": "Bit Demand", "bullets": ["1-2 bullets with Arete's latest demand view [src:S#]"]}},
      {{"topic": "Bit Supply", "bullets": ["1-2 bullets with Arete's latest supply/sufficiency view [src:S#]"]}},
      {{"topic": "HBM / AI Memory", "bullets": ["1-2 bullets with Arete's latest HBM/AI memory view [src:S#]"]}},
      {{"topic": "Wafer / Capacity", "bullets": ["1-2 bullets with Arete's latest wafer/capacity view [src:S#]"]}},
      {{"topic": "Capex / Strategy", "bullets": ["1-2 bullets with Arete's latest capex/strategy view [src:S#]"]}}
    ]
  }},
  "sections": [
    {{
      "topic": "Bit Supply vs Bit Demand Growth",
      "unit": "Bit growth % YoY / Sufficiency ratio %",
      "summary": "One sentence overview",
      "themes": [
        {{
          "title": "Severe Supply Shortage",
          "bullets": ["MS: DRAM sufficiency 2026e: -17% [id:X]", "JPM: bit demand +32% YoY 2026e [id:Y]"]
        }},
        {{
          "title": "Diverging 2027 Views",
          "bullets": ["..."]
        }}
      ]
    }},
    {{"topic": "Inventory", "unit": "Weeks of supply / DIO", "summary": "...", "themes": [...]}},
    {{"topic": "Contract Pricing Trend", "unit": "ASP $/Gb, QoQ %, YoY %", "summary": "...", "themes": [...]}},
    {{"topic": "Spot Pricing & Spot-Contract Spread", "unit": "$/unit, spread %", "summary": "...", "themes": [...]}},
    {{"topic": "HBM Demand & Capacity", "unit": "GB, stacks, wafer-equivalent KWSPM", "summary": "...", "themes": [...]}},
    {{"topic": "Wafer Starts by Supplier & Node", "unit": "KWSPM / WSPM by node", "summary": "...", "themes": [...]}},
    {{"topic": "Capex / WFE / Cleanroom Expansion", "unit": "$bn, capex intensity %", "summary": "...", "themes": [...]}},
    {{"topic": "End-Market Demand × Memory Content", "unit": "Units × GB or TB per device", "summary": "...", "themes": [...]}},
    {{"topic": "Utilization Rates", "unit": "% utilization", "summary": "...", "themes": [...]}},
    {{"topic": "Yield & Cycle-Time Commentary", "unit": "Qualitative / die-penalty ratio", "summary": "...", "themes": [...]}}
  ]
}}

SECTION GUIDE:
1. Bit Supply vs Bit Demand Growth — DRAM & NAND bit supply growth vs bit demand growth YoY, sufficiency ratios, S/D balance shifts
2. Inventory — supplier inventory weeks (DIO), customer/channel inventory weeks, inventory normalization trends
3. Contract Pricing Trend — DRAM & NAND contract ASP trends (QoQ, YoY), DDR4/DDR5/LPDDR/eMMC/UFS pricing, blended ASP
4. Spot Pricing & Spot-Contract Spread — spot price levels, spot-contract spread dynamics, arbitrage signals
5. HBM Demand & Capacity — HBM TAM ($bn), GB content per accelerator, stacks, wafer-equivalent capacity consumed, generation roadmap (HBM3E/4/5)
6. Wafer Starts by Supplier & Node — KWSPM/WSPM by supplier (Samsung, SK Hynix, Micron, CXMT), by node, regional breakdown
7. Capex / WFE / Cleanroom Expansion — memory capex ($bn), capex intensity %, WFE orders, cleanroom expansion plans by supplier
8. End-Market Demand × Memory Content — server, mobile, PC, auto unit demand × DRAM/NAND content per device, total GB/TB demand by end-market
9. Utilization Rates — DRAM & NAND fab utilization rates by supplier, optick/shutdown trends
10. Yield & Cycle-Time Commentary — HBM die penalty ratios, advanced node yields, process migration commentary, cycle-time improvements

RULES:
- DATA SOURCING: every figure, percentage, and qualitative claim MUST be sourced from the PRIMARY SOURCE block (sellside text & tables). The SUPPORTING VISUAL CATALOGUE is REFERENCE-ONLY — never extract a number from a chart's tag/estimate/caption. If a number you want to cite is not in the PRIMARY SOURCE, omit it rather than guess.
- ARETE PRIORITY: Arete is a high-priority source. Use the ARETE LATEST SOURCE BLOCK to fill `arete_latest` after the bull/bear/uncertain cases. Also include an Arete bullet inside each normal section where Arete has relevant evidence. If Arete has no evidence for a topic, write "N/A — no Arete evidence retrieved" for that Arete card; do not fabricate.
- ARETE CITATIONS: Inside `arete_latest`, cite ONLY [src:S#] entries from the ARETE LATEST SOURCE BLOCK. Do NOT use UBS/MS/GS/JPM/BofA sources or [id:N] visual catalogue citations in `arete_latest`.
- BROKER COVERAGE: Use the ALL-BROKER COVERAGE BLOCK as a coverage floor. Consider every broker with recent evidence when writing bull/bear/uncertain and normal sections, so the summary does not only reflect whichever brokers ranked highest in broad retrieval.
- BROKER GAPS: If a requested broker has "N/A - no recent evidence retrieved", do not fabricate that broker's view.
- You MUST output ALL 10 sections above in the exact order listed
- THEMES: organize each section's content under 2-4 short thematic subtitles
  (3-5 words, e.g. "Severe Supply Shortage", "HBM Pricing Premium", "China
  Capacity Risk") — group bullets by the STORY they tell, not by broker.
  Start each bullet with the broker tag, e.g. "MS: ..." or "GS/JPM: ..."
- If a section has only partial data, still include it — show what is available
- If a section has NO relevant data at all, set themes to an empty array and summary to "N/A — no relevant data in current chart pool"
- Include SPECIFIC numbers and estimates where available — do not generalize
- Only include {cur_year}e and beyond estimates
- Keep bullets concise (1 line each) but include numbers
- COMMENTARY RECENCY: prefer evidence within 20 days. If a topic has none, use the closest available cited evidence within 45 days and keep its source date visible.
- CITATIONS: Each bullet MUST end with source IDs. Use [id:123] for chart-pool items and [src:S1] for retrieved RAG excerpts. Multiple citations are allowed, e.g. [id:42][src:S3].
- Return STRICT JSON only, no markdown fence"""


def _build_company_prompt(catalogue_block: str, extra_context: str = "") -> str:
    cur_year = date.today().year
    data_block = catalogue_block
    prompt_override = get_tab_prompt("overview", "company_prompt")
    if prompt_override:
        return render_prompt_template(prompt_override, {
            "cur_year": cur_year,
            "today": date.today().isoformat(),
            "extra_context": extra_context,
            "catalogue_block": data_block,
        })

    return f"""You are a buyside semiconductor memory analyst summarizing sellside research views on specific companies.

CURRENT YEAR: {cur_year}
TODAY: {date.today().isoformat()}

## PRIMARY SOURCE — sellside text & tables (use these for ALL figures and commentary)
{extra_context}

## SUPPORTING VISUAL CATALOGUE — chart thumbnails grouped by manufacturer (REFERENCE ONLY, do NOT lift figures from these)
Each entry is a chart available to display alongside your answer. Use [id:N] to cite a chart when it illustrates a point, but do NOT extract numbers from a chart's tag/estimate/caption — those are summaries, not the source of truth. All figures must come from the PRIMARY SOURCE block above.

{data_block}

TASK: Produce a structured COMPANY OVERVIEW summary in JSON format.

Structure the output as:
{{
  "title": "Memory Company Overview",
  "last_updated": "{date.today().isoformat()}",
  "overall_summary": "A 3-5 sentence executive summary consolidating the key themes across ALL companies below — cover competitive positioning, capacity leadership, HBM race dynamics, and margin/pricing outlook in one concise paragraph",
  "bull_case": ["2-4 bullet points: factors supporting upside for memory companies — e.g. HBM leadership, margin expansion, technology node advantage, pricing power"],
  "bear_case": ["2-4 bullet points: factors representing downside risk — e.g. capex overcommitment, competitive share loss, yield issues, customer concentration"],
  "uncertain": ["2-4 bullet points: key swing factors where views diverge across companies — e.g. China market access, HBM3e yield ramp, NAND profitability recovery timing"],
  "arete_latest": {{
    "title": "ARETE Latest PDF Summary",
    "source": "Latest Arete filename/date used, or N/A",
    "cards": [
      {{"topic": "Pricing", "bullets": ["1-2 bullets with Arete's latest pricing view and company implications [src:S#]"]}},
      {{"topic": "Inventory", "bullets": ["1-2 bullets with Arete's latest supplier/customer/channel inventory view and company implications [src:S#]"]}},
      {{"topic": "Bit Demand", "bullets": ["1-2 bullets with Arete's latest demand view and company implications [src:S#]"]}},
      {{"topic": "Bit Supply", "bullets": ["1-2 bullets with Arete's latest supply/sufficiency view and company implications [src:S#]"]}},
      {{"topic": "HBM / AI Memory", "bullets": ["1-2 bullets with Arete's latest HBM/AI memory view and company implications [src:S#]"]}},
      {{"topic": "Wafer / Capacity", "bullets": ["1-2 bullets with Arete's latest wafer/capacity view and company implications [src:S#]"]}},
      {{"topic": "Capex / Strategy", "bullets": ["1-2 bullets with Arete's latest capex/strategy view and company implications [src:S#]"]}}
    ]
  }},
  "companies": [
    {{
      "name": "Samsung",
      "summary_bullets": ["One concise positioning bullet ending with a source citation [src:S#]"],
      "segments": [
        {{
          "topic": "DRAM",
          "views": [
            {{
              "broker": "Morgan Stanley",
              "bullets": ["specific view with numbers"]
            }}
          ]
        }},
        {{
          "topic": "HBM",
          "views": [...]
        }}
      ],
      "strategic_updates": [
        "Any announced capacity expansions, technology transitions, strategic moves mentioned in chart captions"
      ]
    }}
  ]
}}

RULES:
- DATA SOURCING: every figure, percentage, and qualitative claim MUST be sourced from the PRIMARY SOURCE block (sellside text & tables). The SUPPORTING VISUAL CATALOGUE is REFERENCE-ONLY — never extract a number from a chart's tag/estimate/caption. If a number you want to cite is not in the PRIMARY SOURCE, omit it rather than guess.
- ARETE PRIORITY: Arete is a high-priority source. Use the ARETE LATEST SOURCE BLOCK to fill `arete_latest` after the bull/bear/uncertain cases. Also include Arete's relevant company implications inside the normal company sections where evidence exists. If Arete has no evidence for a topic, write "N/A — no Arete evidence retrieved" for that Arete card; do not fabricate.
- ARETE CITATIONS: Inside `arete_latest`, cite ONLY [src:S#] entries from the ARETE LATEST SOURCE BLOCK. Do NOT use UBS/MS/GS/JPM/BofA sources or [id:N] visual catalogue citations in `arete_latest`.
- BROKER COVERAGE: Use the ALL-BROKER COVERAGE BLOCK as a coverage floor. Consider every broker with recent evidence when writing the company and segment views, so the summary does not only reflect whichever brokers ranked highest in broad retrieval.
- BROKER GAPS: If a requested broker has "N/A - no recent evidence retrieved", do not fabricate that broker's view.
- Group by company, then by memory segment, then by broker view
- BULLET-ONLY OUTPUT: every narrative claim must be an item inside a JSON array. Do not return prose paragraphs or string summaries.
- Include SPECIFIC numbers and estimates where available
- Only include {cur_year}e and beyond estimates
- The "strategic_updates" section captures company announcements, capex plans, technology roadmaps, capacity changes mentioned in the data
- If a company has limited data, still include it but keep it brief
- Skip companies with no meaningful data beyond "General"
- Keep bullets concise but include numbers
- COMMENTARY RECENCY: prefer evidence within 20 days. If a topic has none, use the closest available cited evidence within 45 days and keep its source date visible.
- CITATIONS: Each bullet MUST end with source IDs. Use [id:123] for chart-pool items and [src:S1] for retrieved RAG excerpts. Multiple citations are allowed, e.g. [id:55][src:S7].
- Return STRICT JSON only, no markdown fence"""


def _build_industry_prompt(catalogue_block: str, extra_context: str = "") -> str:
    """Current Industry Overview contract.

    Defined after the legacy prompt above so this version wins at route-call
    time without touching the large historical prompt string.
    """
    cur_year = date.today().year
    data_block = catalogue_block
    prompt_override = get_tab_prompt("overview", "industry_prompt")
    if prompt_override:
        return render_prompt_template(prompt_override, {
            "cur_year": cur_year,
            "today": date.today().isoformat(),
            "extra_context": extra_context,
            "catalogue_block": data_block,
        })

    return f"""You are a buyside semiconductor memory analyst summarizing the latest sellside research views.

CURRENT YEAR: {cur_year}
TODAY: {date.today().isoformat()}

## PRIMARY SOURCE - sellside text & tables (use these for ALL figures and commentary)
{extra_context}

## SUPPORTING VISUAL CATALOGUE - chart thumbnails available in the dashboard (REFERENCE ONLY, do NOT lift figures from these)
Each entry is a chart available to display alongside your answer. Use [id:N] to cite a chart when it illustrates a point, but do NOT extract numbers from a chart's tag/estimate/caption. Those are summaries, not the source of truth. All figures must come from the PRIMARY SOURCE block above.

{data_block}

TASK: Produce a structured INDUSTRY OVERVIEW summary in JSON format.

Structure the output as:
{{
  "title": "Memory Industry Overview",
  "last_updated": "{date.today().isoformat()}",
  "overall_bullets": [
    "4-6 concise executive bullets covering demand, supply, pricing, capacity, and HBM. Each bullet must end with [src:S#] or [id:N]."
  ],
  "bull_case": ["2-4 concise bullets: upside factors for memory industry"],
  "bear_case": ["2-4 concise bullets: downside risks"],
  "uncertain": ["2-4 concise bullets: key swing factors or broker debates"],
  "arete_latest": {{
    "title": "ARETE Latest PDF Summary",
    "source": "Latest Arete filename/date used, or N/A",
    "cards": [
      {{"topic": "Pricing", "bullets": ["1-2 concise bullets with Arete's latest pricing view and numbers where available [src:S#]"]}},
      {{"topic": "Inventory", "bullets": ["1-2 concise bullets with Arete's latest supplier/customer/channel inventory view [src:S#]"]}},
      {{"topic": "Bit Demand", "bullets": ["1-2 concise bullets with Arete's latest demand view [src:S#]"]}},
      {{"topic": "Bit Supply", "bullets": ["1-2 concise bullets with Arete's latest supply/sufficiency view [src:S#]"]}},
      {{"topic": "HBM / AI Memory", "bullets": ["1-2 concise bullets with Arete's latest HBM/AI memory view [src:S#]"]}},
      {{"topic": "Wafer / Capacity", "bullets": ["1-2 concise bullets with Arete's latest wafer/capacity view [src:S#]"]}},
      {{"topic": "Capex / Strategy", "bullets": ["1-2 concise bullets with Arete's latest capex/strategy view [src:S#]"]}}
    ]
  }},
  "sections": [
    {{
      "group": "Demand & Supply Summary",
      "topic": "Demand & Supply Summary",
      "unit": "Cross-segment balance",
      "summary_bullets": ["One concise summary bullet ending with a source citation [src:S#]"],
      "themes": [
        {{"title": "Balance Snapshot", "bullets": ["Broker: concise demand/supply balance with specific number [src:S#]"]}},
        {{"title": "Key Debate", "bullets": ["Broker: concise disagreement or swing factor [src:S#]"]}}
      ]
    }},
    {{"group": "Demand", "topic": "Bit Demand & Memory Content", "unit": "Bit growth %, units x GB/TB content", "summary_bullets": ["..."], "themes": [...]}},
    {{"group": "Demand", "topic": "Inventory", "unit": "Weeks of supply / DIO", "summary_bullets": ["..."], "themes": [...]}},
    {{"group": "Demand", "topic": "Contract Pricing / ASP", "unit": "ASP $/Gb, QoQ %, YoY %", "summary_bullets": ["..."], "themes": [...]}},
    {{"group": "Demand", "topic": "Spot Pricing & Spot-Contract Spread", "unit": "$/unit, spread %", "summary_bullets": ["..."], "themes": [...]}},
    {{"group": "Demand", "topic": "HBM Demand & Capacity", "unit": "GB, stacks, wafer-equivalent KWSPM", "summary_bullets": ["..."], "themes": [...]}},
    {{"group": "Supply", "topic": "Bit Supply", "unit": "DRAM/NAND/HBM bit supply growth %, sufficiency", "summary_bullets": ["..."], "themes": [...]}},
    {{"group": "Supply", "topic": "Foundry / CoWoS Capacity", "unit": "TSMC/foundry KWSPM, CoWoS wafers, advanced packaging", "summary_bullets": ["..."], "themes": [...]}},
    {{"group": "Supply", "topic": "Wafer Starts & Node Capacity", "unit": "KWSPM / WSPM by node and technology", "summary_bullets": ["..."], "themes": [...]}},
    {{"group": "Supply", "topic": "Capex / WFE / Cleanroom Expansion", "unit": "$bn, capex intensity %, WFE", "summary_bullets": ["..."], "themes": [...]}},
    {{"group": "Supply", "topic": "Utilization / Yield / Cycle-Time", "unit": "% utilization, yield, cycle time", "summary_bullets": ["..."], "themes": [...]}}
  ]
}}

SECTION GUIDE:
0. Demand & Supply Summary - summarize the entire memory setup, explicitly connecting demand pull, pricing, memory supply, foundry/CoWoS constraints, and biggest broker disagreement.
DEMAND
1. Bit Demand & Memory Content - DRAM/NAND/HBM bit demand growth, AI server/mobile/PC/auto unit demand, memory content per device or system.
2. Inventory - supplier inventory weeks, DIO, customer/channel inventory weeks, inventory normalization.
3. Contract Pricing / ASP - DRAM and NAND contract ASP trends, DDR4/DDR5/LPDDR/eMMC/UFS pricing, blended ASP.
4. Spot Pricing & Spot-Contract Spread - spot price levels, spot-contract spread, arbitrage signals.
5. HBM Demand & Capacity - HBM TAM, GB content per accelerator, stacks, wafer-equivalent capacity consumed, HBM3E/HBM4 roadmap.
SUPPLY
6. Bit Supply - DRAM/NAND/HBM bit supply growth, sufficiency, supply discipline, and broad supply/demand balance.
7. Foundry / CoWoS Capacity - latest TSMC/foundry capacity, leading-edge KWSPM, CoWoS/advanced packaging wafers, AI accelerator logic bottlenecks.
8. Wafer Starts & Node Capacity - memory KWSPM/WSPM by node, technology migration, and regional capacity additions.
9. Capex / WFE / Cleanroom Expansion - memory capex, capex intensity, WFE orders, cleanroom expansion plans.
10. Utilization / Yield / Cycle-Time - DRAM/NAND fab utilization, HBM die penalty/yield, cycle-time/process migration commentary.

RULES:
- DATA SOURCING: every figure, percentage, and qualitative claim MUST be sourced from the PRIMARY SOURCE block. The SUPPORTING VISUAL CATALOGUE is REFERENCE-ONLY; never extract a number from chart tags/estimates/captions. If a number is not in PRIMARY SOURCE, omit it.
- ARETE FIRST: Arete is a high-priority source. Fill `arete_latest` as the first content section after the executive bullets. Also include Arete inside the normal Demand/Supply sections where topic-relevant. If Arete has no evidence for a topic, write "N/A - no Arete evidence retrieved"; do not fabricate.
- ARETE CITATIONS: Inside `arete_latest`, cite ONLY [src:S#] entries from the ARETE LATEST SOURCE BLOCK. Do NOT use UBS/MS/GS/JPM/BofA sources or [id:N] visual catalogue citations in `arete_latest`.
- BROKER COVERAGE: Use the ALL-BROKER COVERAGE BLOCK as a coverage floor. Consider every broker with recent evidence so the summary does not only reflect whichever brokers ranked highest in broad retrieval.
- BROKER GAPS: If a requested broker has "N/A - no recent evidence retrieved", do not fabricate that broker's view.
- You MUST output all 11 sections above in the exact order listed, with the exact `group` and `topic` names.
- THEMES: organize each section under 2-4 short thematic subtitles. Group bullets by the story they tell, not by broker. Start each bullet with the broker tag, e.g. "MS: ..." or "GS/JPM: ...".
- Demand sections should focus on demand, inventory, pricing, ASP, and HBM consumption.
- Supply sections should focus on latest bit supply, TSMC/foundry/CoWoS capacity, wafer starts, capex, utilization, yield, and cycle-time. Do not create or require a memory-company-by-company supply breakdown.
- If a section has only partial data, still include it and show what is available.
- If a section has NO relevant data at all, set themes and summary_bullets to empty arrays.
- BULLET-ONLY OUTPUT: every narrative claim must be an item inside a JSON array. Do not return prose paragraphs or string summaries.
- Include SPECIFIC numbers and estimates where available; do not generalize.
- Only include {cur_year}e and beyond estimates.
- Keep bullets concise: one line, preferably under 28 words, but include the key number.
- COMMENTARY RECENCY: prefer evidence within 20 days. If a topic has none, use the closest available cited evidence within 45 days and keep its source date visible.
- CITATIONS: Each bullet MUST end with source IDs. Use [id:123] for chart-pool items and [src:S1] for retrieved RAG excerpts. Multiple citations are allowed, e.g. [id:42][src:S3].
- Return STRICT JSON only, no markdown fence"""


async def _call_llm(prompt: str) -> dict:
    import re
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return {"error": "No API key configured"}

    from ...services.openai_research import AsyncResearchClient
    client = AsyncResearchClient(api_key=api_key)

    try:
        t0 = time.time()
        # Streaming keeps the connection active — non-streaming calls idle for
        # 60s+ during generation and get killed by the corporate proxy.
        async with client.messages.stream(
            model=_OVERVIEW_OPENAI_MODEL, max_tokens=20000, temperature=0,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            resp = await stream.get_final_message()
        elapsed = round(time.time() - t0, 1)
        logger.info("Overview LLM call took %.1fs (tokens in=%d out=%d)", elapsed, resp.usage.input_tokens, resp.usage.output_tokens)
        text = "".join(getattr(b, "text", "") for b in resp.content).strip()
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
        try:
            result = json.loads(text)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                result = json.loads(m.group(0))
            else:
                raise
        result["_generation_seconds"] = elapsed
        return result
    except Exception as exc:
        logger.warning("Overview LLM call failed: %s", exc)
        return {"error": str(exc)}


def _meaningful_overview_text(value: object) -> str:
    text = str(value or "").strip()
    normalized = re.sub(r"\s+", " ", text).casefold()
    if not normalized or normalized in {"n/a", "na"}:
        return ""
    if normalized.startswith("n/a -") or "no relevant data" in normalized:
        return ""
    if "no recent sellside research evidence retrieved" in normalized:
        return ""
    return text


def _has_overview_citation(value: object) -> bool:
    text = str(value or "")
    return bool(_SRC_REF_RE.search(text) or _CHART_REF_RE.search(text))


def _cited_bullets(values: object) -> list[str]:
    if not isinstance(values, list):
        values = [values] if values else []
    return [
        text
        for text in (_meaningful_overview_text(value) for value in values)
        if text and _has_overview_citation(text)
    ]


def _normalize_overview_bullets(result: dict, *, view: str) -> None:
    """Make all narrative fields list-based and discard unsupported claims."""
    for key in ("overall_bullets", "bull_case", "bear_case", "uncertain"):
        result[key] = _cited_bullets(result.get(key))

    if view == "industry":
        for section in result.get("sections") or []:
            if not isinstance(section, dict):
                continue
            summaries = _cited_bullets(section.get("summary_bullets"))
            if not summaries:
                summaries = _cited_bullets(section.get("summary"))
            section["summary_bullets"] = summaries or ["No recent evidence retrieved."]
            clean_themes: list[dict] = []
            for theme in section.get("themes") or []:
                if not isinstance(theme, dict):
                    continue
                clean = dict(theme)
                clean["bullets"] = _cited_bullets(theme.get("bullets"))
                if clean["bullets"]:
                    clean_themes.append(clean)
            section["themes"] = clean_themes
        return

    for company in result.get("companies") or []:
        if not isinstance(company, dict):
            continue
        summaries = _cited_bullets(company.get("summary_bullets"))
        if not summaries:
            summaries = _cited_bullets(company.get("summary"))
        company["summary_bullets"] = summaries or ["No recent evidence retrieved."]
        clean_segments: list[dict] = []
        for segment in company.get("segments") or []:
            if not isinstance(segment, dict):
                continue
            clean_views: list[dict] = []
            for view_item in segment.get("views") or []:
                if not isinstance(view_item, dict):
                    continue
                clean_view = dict(view_item)
                clean_view["bullets"] = _cited_bullets(view_item.get("bullets"))
                if clean_view["bullets"]:
                    clean_views.append(clean_view)
            if clean_views:
                clean_segment = dict(segment)
                clean_segment["views"] = clean_views
                clean_segments.append(clean_segment)
        company["segments"] = clean_segments
        company["strategic_updates"] = _cited_bullets(company.get("strategic_updates"))


def _overview_highlights(result: dict, *, view: str) -> list[str]:
    highlights: list[str] = []
    if view == "industry":
        for section in result.get("sections") or []:
            if not isinstance(section, dict):
                continue
            summary_bullets = _cited_bullets(section.get("summary_bullets"))
            if summary_bullets:
                highlights.append(summary_bullets[0])
                continue
            theme_highlight = ""
            for theme in section.get("themes") or []:
                if not isinstance(theme, dict):
                    continue
                for bullet in theme.get("bullets") or []:
                    text = _meaningful_overview_text(bullet)
                    if text:
                        theme_highlight = text
                        break
                if theme_highlight:
                    break
            if theme_highlight:
                highlights.append(theme_highlight)
    else:
        for company in result.get("companies") or []:
            if not isinstance(company, dict):
                continue
            summary_bullets = _cited_bullets(company.get("summary_bullets"))
            if summary_bullets:
                highlights.append(summary_bullets[0])
    return highlights


def _normalize_overview_shape(result: dict, *, view: str) -> dict:
    """Accept harmless JSON shape drift while preserving the card contract."""
    prepared = dict(result)
    wrapper_keys = (
        ("industry_overview", "overview", "data", "result")
        if view == "industry"
        else ("company_overview", "overview", "data", "result")
    )
    for key in wrapper_keys:
        nested = prepared.get(key)
        if isinstance(nested, dict):
            carried = {
                name: prepared[name]
                for name in ("_generation_seconds",)
                if name in prepared
            }
            prepared = {**nested, **carried}
            break

    collection_key = "sections" if view == "industry" else "companies"
    label_key = "topic" if view == "industry" else "name"
    collection = prepared.get(collection_key)
    if isinstance(collection, dict):
        normalized: list[dict] = []
        for label, value in collection.items():
            if not isinstance(value, dict):
                continue
            item = dict(value)
            item.setdefault(label_key, str(label))
            normalized.append(item)
        prepared[collection_key] = normalized
    return prepared


def _prepare_overview_result(result: dict, *, view: str) -> dict:
    """Repair a partial structured generation before it reaches the cache/UI."""
    if not isinstance(result, dict) or result.get("error"):
        return result
    prepared = _normalize_overview_shape(result, view=view)
    _normalize_overview_bullets(prepared, view=view)
    bullets = list(prepared.get("overall_bullets") or [])
    highlights = _overview_highlights(prepared, view=view)
    if not highlights:
        return {
            "error": f"ES evidence produced no usable structured {view} overview",
            "_overview_diagnostics": {
                "keys": sorted(str(key) for key in prepared),
                "sections_type": type(prepared.get("sections")).__name__,
                "sections_count": len(prepared.get("sections") or [])
                if isinstance(prepared.get("sections"), (list, dict))
                else 0,
                "companies_type": type(prepared.get("companies")).__name__,
                "companies_count": len(prepared.get("companies") or [])
                if isinstance(prepared.get("companies"), (list, dict))
                else 0,
            },
        }
    if not bullets:
        bullets = highlights[:4]
    if bullets:
        prepared["overall_bullets"] = bullets
    if not _meaningful_overview_text(prepared.get("overall_summary")) and bullets:
        prepared["overall_summary"] = " ".join(bullets[:3])
    prepared["last_updated"] = str(prepared.get("last_updated") or date.today().isoformat())
    return prepared


def _rebase_source_block(
    context: str,
    source_index: dict[str, dict],
    start: int,
) -> tuple[str, dict[str, dict], int]:
    """Give an independently retrieved source block globally unique IDs."""
    mapping: dict[str, str] = {}
    rebased: dict[str, dict] = {}
    next_id = start

    def source_number(source_id: str) -> int:
        match = re.search(r"\d+", source_id)
        return int(match.group(0)) if match else 0

    for old_id in sorted(source_index, key=source_number):
        new_id = f"S{next_id}"
        next_id += 1
        mapping[old_id] = new_id
        rebased[new_id] = source_index[old_id]

    rewritten = re.sub(
        r"\[src:(S\d+)\]",
        lambda match: f"[src:{mapping.get(match.group(1), match.group(1))}]",
        context or "",
    )
    return rewritten, rebased, next_id


async def _gather_overview_inputs() -> tuple[list[dict], str, dict[str, dict], dict]:
    """Fetch independent Overview evidence branches concurrently."""
    rag_charts, base_result, arete_result, broker_result = await asyncio.gather(
        _gather_chart_catalogue_from_rag(),
        _gather_retrieved_context_with_sources(),
        _gather_arete_context_with_sources(1),
        _gather_all_broker_context_with_sources(1),
    )

    blocks: list[str] = []
    source_index: dict[str, dict] = {}
    next_id = 1
    for context, index in (
        base_result,
        (arete_result[0], arete_result[1]),
        (broker_result[0], broker_result[1]),
    ):
        rewritten, rebased, next_id = _rebase_source_block(context, index, next_id)
        if rewritten:
            blocks.append(rewritten)
        source_index.update(rebased)

    return rag_charts, "".join(blocks), source_index, broker_result[2]


async def _direct_es_overview(view: str, refresh: bool = False, broker: str | None = None):
    """ES owns research and final composition; Memory only caches and displays."""
    from ...services.es_rag_gateway import get_es_rag_gateway, EsRagContractError
    from .chat_bridge import _ask_source_filters, _qualify_es_markdown_links
    await _ensure_cache_table()
    today = date.today()
    source_filters = _ask_source_filters(get_tab_sources("overview")) or {}
    filters = {**source_filters, "date_from": (today-timedelta(days=_OVERVIEW_RECENCY_DAYS)).isoformat(),
               "date_to": today.isoformat()}
    if view == "industry" and broker:
        filters["brokers"] = [broker]
    broker_display = "BofA" if broker == "BOFA" else broker
    override = get_tab_prompt("overview", view+"_prompt") or ""
    fingerprint = __import__('hashlib').sha256(json.dumps(
        {"filters":filters,"prompt":override},sort_keys=True).encode()).hexdigest()[:16]
    cache_version = "v4" if view == "industry" else "v2"
    cache_key = f"{view}_es_direct_answer_{cache_version}_{fingerprint}"
    if not refresh:
        cached = _cache.get(cache_key) or await _load_db_cache(cache_key)
        if cached:
            cached_data = cached["data"]
            existing_sources = cached_data.get("_source_index") or {}
            existing_sources.update(await _overview_citation_sources(
                cached_data.get("answer_markdown", ""), client=None, filters=filters,
                known_ids=set(existing_sources),
            ))
            cached_data["_source_index"] = existing_sources
            return cached_data
    focus = (
        "Use sections: Executive summary; Demand and supply; DRAM pricing; NAND pricing; "
        "HBM and AI memory; Inventory; Capacity and capex; Bull case; Bear case. "
        "Summarize each section in one or two evidence-backed bullets. Keep the whole answer under 850 words "
        "and avoid repeating claims across sections."
        if view == "industry" else
        "Use sections: Executive summary; SK hynix; Samsung Electronics; Micron; "
        "AI demand context (NVIDIA, Google, Meta and cloud customers); Competitive outlook; Evidence gaps."
    )
    provider_scope = (
        f"This is the {broker_display} section. Summarize only {broker_display} source documents. "
        f"Title the answer '{broker_display} memory overview'. Do not compare with or cite other providers. "
        if view == "industry" and broker else ""
    )
    bullet_style = "concise bullets" if view == "industry" else "detailed concise bullets"
    question = (
        "Write the final Memory Tracker "+view+" overview for an equity analyst. "+provider_scope+focus+
        " Explain the key takeaway first in each section, then specific evidence and implications. "
        "Use Markdown headings and "+bullet_style+"; bold key points. Cite original stored "
        "pages using the ES citation mechanism. Distinguish broker forecasts from reported facts. "
        "Use only the enforced publication window; never silently backfill older evidence. "
        "For each section, if the latest PDF has no relevant content or data, search other PDFs "
        "within the enforced provider and publication filters. Do not dwell on the latest PDF "
        "or declare evidence missing until other eligible PDFs have been checked. "
        "State missing evidence explicitly. Do not claim all brokers were reviewed unless verified. "
        "Do not return JSON or invented chart references."
    )
    if override:
        question += "\nAdditional user Overview instructions:\n"+override
    if len(question)>12000:
        raise EsRagContractError("Overview instructions exceed ES's 12000-character question limit",operation="overview")
    client = get_es_rag_gateway()
    started = time.monotonic()
    try:
        answer = await asyncio.to_thread(client.ask,question,filters=filters,theme="ai")
    except EsRagContractError:
        raise
    except Exception as exc:
        logger.warning("Overview answer request failed for %s: %s", broker or view, exc)
        raise
    if not answer.strip():
        raise EsRagContractError("ES returned an empty Overview answer",operation="overview")
    result = {"title":(f"{broker_display} Industry Overview" if broker else view.title()+" Overview"), "last_updated":today.isoformat(),
              "answer_markdown":_qualify_es_markdown_links(answer,client.base_url),
              "_answer_provider":"ES", "_applied_filters":{"themes":["ai"],**filters},
              "_generation_seconds":round(time.monotonic()-started,2)}
    result["_source_index"] = await _overview_citation_sources(
        result["answer_markdown"], client=client, filters=filters
    )
    _cache[cache_key]={"data":result,"_ts":time.time()}
    await _save_db_cache(cache_key,result)
    return result


async def _overview_citation_sources(
    answer: str, *, client, filters: dict, known_ids: set[str] | None = None
) -> dict:
    """Resolve cited document IDs to stored publisher/provider metadata."""
    from urllib.parse import unquote
    from ...services.es_rag_gateway import get_es_rag_gateway

    ids = set()
    for encoded_id in re.findall(
        r"/api/research-documents/([^/]+)/(?:pages|pdf|snapshot|read)(?:/|\?|$)", answer
    ):
        try:
            ids.add(unquote(encoded_id))
        except Exception:
            continue
    if not ids:
        return {}
    ids.difference_update(known_ids or set())
    if not ids:
        return {}
    gateway = client or get_es_rag_gateway()
    resolved: dict[str, dict] = {}
    offset = 0
    revision = None
    try:
        while len(resolved) < len(ids):
            page = await asyncio.to_thread(
                gateway.search_research_documents,
                filters=filters,
                limit=100,
                offset=offset,
                inventory_revision=revision,
            )
            revision = page.get("inventory_revision")
            for doc in page.get("results", []):
                document_id = doc.get("document_version_id")
                if document_id not in ids:
                    continue
                provider = (doc.get("broker") or doc.get("publisher") or
                            doc.get("source_provider") or "").strip()
                resolved[document_id] = {
                    "id": document_id,
                    "provider": provider,
                    "broker": doc.get("broker") or "",
                    "filename": doc.get("name") or "",
                    "date": doc.get("publication_date") or "",
                    "content_type": doc.get("document_type") or "",
                }
            next_offset = page.get("next_offset")
            if next_offset is None:
                break
            offset = int(next_offset)
    except Exception as exc:
        logger.warning("Overview citation provider lookup failed: %s", exc)
    return resolved


@router.get("/industry/brokers")
async def get_industry_brokers():
    """List providers with in-window ES documents that support broker scoping."""
    from ...services.dashboard_refresh import KNOWN_BROKERS
    from ...services.es_rag_gateway import get_es_rag_gateway
    from .chat_bridge import _ask_source_filters

    today = date.today()
    source_filters = _ask_source_filters(get_tab_sources("overview")) or {}
    filters = {**source_filters,
               "date_from": (today-timedelta(days=_OVERVIEW_RECENCY_DAYS)).isoformat(),
               "date_to": today.isoformat()}
    recognized = {_broker_key(name) for name in KNOWN_BROKERS if _broker_key(name) != "arete"}
    found: dict[str, str] = {}
    client = get_es_rag_gateway()
    offset = 0
    revision = None
    while True:
        page = await asyncio.to_thread(client.search_research_documents, filters=filters,
                                       limit=100, offset=offset, inventory_revision=revision)
        revision = page.get("inventory_revision")
        for doc in page.get("results", []):
            provider = str(doc.get("broker") or "").strip()
            key = _broker_key(provider)
            if key in recognized:
                found[key] = provider
        next_offset = page.get("next_offset")
        if next_offset is None or int(next_offset) > 10000:
            break
        offset = int(next_offset)
    brokers = sorted(found.values(), key=str.casefold)
    return {"brokers": brokers, "date_from": filters["date_from"], "date_to": filters["date_to"]}


@router.get("/industry")
async def get_industry_overview(refresh: bool = False, broker: str = Query("Arete", min_length=1, max_length=80)):
    return await _direct_es_overview("industry",refresh,broker=broker)


@router.get("/company")
async def get_company_overview(refresh: bool = False):
    return await _direct_es_overview("company",refresh)


_overview_refresh_lock = asyncio.Lock()


def _overview_auto_refresh_enabled() -> bool:
    return False


def _overview_refresh_hour() -> int:
    try:
        return max(0, min(23, int(os.getenv("OVERVIEW_DAILY_REFRESH_HOUR", "7"))))
    except ValueError:
        return 7


def _daily_refresh_cutoff(now: datetime | None = None) -> datetime:
    now = now or datetime.now()
    return now.replace(
        hour=_overview_refresh_hour(),
        minute=0,
        second=0,
        microsecond=0,
    )


def _seconds_until_next_daily_refresh(now: datetime | None = None) -> float:
    now = now or datetime.now()
    target = _daily_refresh_cutoff(now)
    if now >= target:
        target += timedelta(days=1)
    return max(1.0, (target - now).total_seconds())


async def _overview_cache_due_for_today() -> bool:
    now = datetime.now()
    cutoff = _daily_refresh_cutoff(now)
    if now < cutoff:
        return False
    await _ensure_cache_table()
    keys = [
        f"industry_{_OVERVIEW_CACHE_VERSION}",
        f"company_{_OVERVIEW_CACHE_VERSION}",
    ]
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT key, ts FROM overview_cache WHERE key IN (?, ?)",
        tuple(keys),
    )
    by_key = {r["key"]: float(r["ts"] or 0) for r in rows}
    cutoff_ts = cutoff.timestamp()
    return any(by_key.get(k, 0.0) < cutoff_ts for k in keys)


async def refresh_overview_cache() -> dict[str, dict]:
    """Regenerate both Overview caches, protected against overlapping runs."""
    async with _overview_refresh_lock:
        logger.info("Refreshing Overview caches with all-broker coverage")
        industry = await get_industry_overview(refresh=True)
        company = await get_company_overview(refresh=True)
        return {"industry": industry, "company": company}


async def overview_auto_refresh_loop() -> None:
    """Daily Overview refresh loop. Uses server-local time by design."""
    if not _overview_auto_refresh_enabled():
        logger.info("Overview auto-refresh disabled")
        return
    logger.info(
        "Overview auto-refresh enabled for %02d:00 local time",
        _overview_refresh_hour(),
    )
    while True:
        try:
            if await _overview_cache_due_for_today():
                await refresh_overview_cache()
                await asyncio.sleep(60)
                continue
            wait_s = _seconds_until_next_daily_refresh()
            logger.info("Next Overview auto-refresh in %.0f seconds", wait_s)
            await asyncio.sleep(wait_s)
            await refresh_overview_cache()
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Overview auto-refresh failed: %s", exc)
            await asyncio.sleep(15 * 60)


def start_overview_auto_refresh() -> asyncio.Task | None:
    if not _overview_auto_refresh_enabled():
        logger.info("Overview auto-refresh disabled")
        return None
    return asyncio.create_task(
        overview_auto_refresh_loop(),
        name="overview-auto-refresh",
    )


async def stop_overview_auto_refresh(task: asyncio.Task | None) -> None:
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


@router.get("/chartindex")
async def get_chart_index():
    """Lightweight chart lookup: id -> {tag, broker, category, subcategory, chart_date, image}"""
    db = await get_db()
    rows = await db.execute_fetchall(
        """SELECT ch.id, ch.tag, ch.broker, ch.chart_date, ch.image_path,
                  ch.page_number, sp.filename as source_filename,
                  sc.name as subcategory_name, cat.name as category_name
           FROM charts ch
           LEFT JOIN subcategories sc ON ch.subcategory_id = sc.id
           LEFT JOIN categories cat ON sc.category_id = cat.id
           LEFT JOIN source_pdfs sp ON ch.source_pdf_id = sp.id
           WHERE ch.quality_status = 'approved'"""
    )
    index = {}
    for r in rows:
        index[str(r["id"])] = {
            "tag": r["tag"],
            "broker": r["broker"],
            "category": r["category_name"],
            "subcategory": r["subcategory_name"],
            "date": r["chart_date"],
            "image": r["image_path"],
            "filename": r["source_filename"],
            "page": r["page_number"],
        }
    return index
