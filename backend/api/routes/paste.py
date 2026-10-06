from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import uuid
from datetime import date
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, Form, HTTPException

from ...db import queries
from ...db.connection import get_db
from ...paths import CHARTS_DIR
from ...services import normalize_broker
from ...services.es_rag_gateway import EsRagError, get_es_rag_gateway
from ...services.fact_pool import _bump_last_index_ts
from ...services.openai_chart_classifier import classify_chart_json

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["paste"])

DATA_DIR = CHARTS_DIR
DATA_DIR.mkdir(parents=True, exist_ok=True)

MAX_IMAGE_SIZE = 10 * 1024 * 1024
_HAIKU_MODEL = os.getenv("OPENAI_EXTRACTION_MODEL", "gpt-5.6-luna")

_PASTED_CHART_SCHEMA = {
    "type": "object",
    "properties": {
        "topic": {"type": "string"},
        "chart_type": {"type": "string"},
        "broker": {"type": "string"},
        "date": {"type": "string"},
        "tag": {"type": "string"},
        "caption": {"type": "string"},
        "category": {"type": "string"},
        "subcategory": {"type": "string"},
        "companies": {"type": "array", "items": {"type": "string"}},
        "estimates": {"type": "string"},
    },
    "required": [
        "topic", "chart_type", "broker", "date", "tag", "caption",
        "category", "subcategory", "companies", "estimates",
    ],
    "additionalProperties": False,
}


def _today_str() -> str:
    return date.today().isoformat()


def _today_int() -> int:
    d = date.today()
    return d.year * 10000 + d.month * 100 + d.day


def _parse_date_int(date_str: str) -> int:
    m = re.match(r"(\d{4})-?(\d{2})-?(\d{2})", date_str)
    if m:
        return int(m.group(1)) * 10000 + int(m.group(2)) * 100 + int(m.group(3))
    return 0


async def _llm_tag_and_categorize(
    image_bytes: bytes, media_type: str, user_hint: str, category_tree: list[dict]
) -> dict:
    """Use OpenAI vision to tag the chart and categorize it."""
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        logger.warning("OPENAI_API_KEY not set; skipping LLM, using fallback")
        return {"tag": user_hint or "Pasted chart", "caption": "", "broker": "",
                "date": "", "chart_type": "", "category": "General Memory",
                "subcategory": "Overall memory market"}

    # Build category tree string
    tree_lines = []
    for cat in category_tree:
        subs = [s["name"] for s in cat.get("subcategories", [])]
        tree_lines.append(f"  {cat['name']}: {', '.join(subs)}")
    tree_str = "\n".join(tree_lines)

    today = _today_str()
    current_year = date.today().year
    next_year = current_year + 1
    next_year2 = current_year + 2
    prompt = f"""You are analyzing a chart image from semiconductor memory industry research.

USER CONTEXT (may be empty): {user_hint}
TODAY'S DATE: {today}

TASK — analyze this chart and return:
1. "topic": What the chart shows (e.g. "HBM TAM forecast", "DDR bit demand")
2. "chart_type": bar, line, stacked_bar, pie, area, waterfall, scatter, other
3. "broker": The research firm / investment bank that produced this chart. You MUST try hard to identify it. Look at ALL of these:
   - Logos (top corners, bottom corners, watermarks)
   - Header/footer text (e.g. "Goldman Sachs", "J.P. Morgan", "Source: UBS estimates")
   - Source citations at the bottom ("Source: Gartner, UBS Research")
   - Disclaimer text, copyright notices
   - Color schemes and chart styling (each bank has a distinctive style)
   - Any text mentioning a firm name anywhere on the chart
   Common brokers: "UBS", "Goldman Sachs", "JPM", "Morgan Stanley", "BofA", "Citi", "Barclays", "Deutsche Bank", "CLSA", "Nomura", "Samsung Securities", "TrendForce", "Yole", "HSBC", "Gartner", "Bernstein", "Jefferies", "Wells Fargo", "KeyBanc", "Mizuho", "Daiwa", "Macquarie"
   Use the SHORT common name (e.g. "UBS" not "UBS AG", "GS" or "Goldman Sachs", "JPM" or "J.P. Morgan").
   If a data provider is cited as source (e.g. "Source: Gartner, UBS estimates"), the BROKER is the bank (UBS), not the data provider.
   NEVER return "Unknown" — if you truly cannot find any broker, return "" (empty string).
4. "date": Publication date in YYYY-MM-DD. Look for date stamps, headers, "Source: Firm, Month YYYY". If only year visible, use YYYY-01-01. If unsure or not visible, default to TODAY: {today}.
5. "tag": Concise label: "<topic> - <broker> <YYYYMMDD>". E.g. "HBM TAM - UBS 20260501". Always include broker if detected.
6. "caption": 1 sentence description.
7. "category": MUST be one of the category names below.
8. "subcategory": MUST be one of the subcategory names below (or propose a new one that fits).

CATEGORY TREE:
{tree_str}

CATEGORIZATION RULES — pick the BEST-FIT category by chart content:
- Bit supply growth, bit demand growth, sufficiency ratios, S/D balance → "Bit Supply vs Bit Demand Growth"
- Inventory weeks, DIO, channel inventory, customer inventory → "Inventory"
- Contract ASP, contract pricing QoQ/YoY, DDR4/DDR5 contract prices → "Contract Pricing Trend"
- Spot pricing, spot-contract spread, spot price charts → "Spot Pricing & Spot-Contract Spread"
- HBM TAM, HBM content/stacks, HBM wafer capacity, HBM roadmap → "HBM Demand & Capacity"
- Wafer starts KWSPM, capacity by supplier/node, wafer additions → "Wafer Starts by Supplier & Node"
- Capex, WFE, cleanroom expansion, capex intensity → "Capex / WFE / Cleanroom Expansion"
- End-market units × memory content (server/mobile/PC/auto demand in GB/TB) → "End-Market Demand × Memory Content"
- Fab utilization rates, optick, shutdown trends → "Utilization Rates"
- Yield commentary, die penalty, HBM yield, cycle time → "Yield & Cycle-Time Commentary"
- ONLY use "General Memory" if the chart is a high-level industry overview that
  truly does not fit any of the 10 specific categories above. Do NOT use
  "General Memory" as a fallback when uncertain — pick the closest specific
  category instead, even if the fit isn't perfect.
- If the user hint mentions a specific category, respect that.

SUBCATEGORY RULES (very important — drives the gallery section the chart lands in):
- PREFER creating a NEW specific 2-4 word subcategory over reusing the
  generic "Overall memory market" / catch-all subcategories.
- If the chart shows a distinctive topic that an existing subcategory in the
  tree already captures, reuse that existing name (verbatim).
- If no existing subcategory cleanly fits, CREATE a new one with a concrete
  descriptive name. Examples of good new subcategory names:
    "HBM3E yield ramp", "Server DDR5 content per box",
    "Samsung 1c node share", "CXMT capacity additions",
    "NAND eSSD attach rate", "TrendForce DRAM spot index"
- A new subcategory is BETTER than the generic catch-all when the chart has
  a clear specific topic. The system auto-creates new subcategories on the
  fly — there is no cost to making one.
- AVOID overly generic names like "Pricing", "Demand", "Supply", "Capacity"
  on their own — qualify with WHAT the chart shows (e.g. "DDR5 contract
  pricing 2026" not "Pricing").

9. "companies": A JSON array of semiconductor memory MANUFACTURER names that this chart is ABOUT (not the broker who wrote it).
   ONLY detect these memory manufacturers: Samsung, SK Hynix, Micron, Kioxia, Western Digital, Nanya, Winbond, Intel, YMTC, CXMT
   - Variants: "SEC" = "Samsung", "SK hynix" = "SK Hynix", "MU" = "Micron", "WDC" = "Western Digital"
   - The chart must be ABOUT that company's memory business (e.g. "Samsung DRAM capex", "SK Hynix HBM")
   - Do NOT include non-memory companies (brokers, OEMs, equipment makers, foundries, etc.)
   - If the chart is about the overall industry (not specific to a memory manufacturer), return ["General"]
   - ALWAYS return at least ["General"] if no memory manufacturer detected

10. "estimates": Extract KEY FORWARD-LOOKING estimates/forecasts visible in the chart.
   CURRENT YEAR: {current_year}
   RULES:
   - ONLY include estimates for year {current_year} and BEYOND (e.g. {current_year}e, {next_year}e, {next_year2}e). EXCLUDE all prior years.
   - ONLY extract ESTIMATES (years with "e", "E", "F", forecast) — NOT historical/actual values
   - Format: concise key=value pairs like "{current_year}e: $25B, {next_year}e: $38B" or "bit growth {current_year}e: +19% YoY, {next_year}e: +22%"
   - If the chart shows a specific metric with clear forecast numbers, extract the latest 2-3 forecast data points
   - If the user provided specific numbers in their hint, use those
   - If you cannot read specific numbers with HIGH CONFIDENCE, return "" (empty string)
   - Do NOT guess or approximate — only label when numbers are clearly readable
   - Keep it under 80 characters

Return STRICT JSON, no markdown fence:
{{"topic":"...","chart_type":"...","broker":"...","date":"...","tag":"...","caption":"...","category":"...","subcategory":"...","companies":["..."],"estimates":"..."}}"""

    try:
        result = await classify_chart_json(
            prompt,
            schema_name="pasted_memory_chart",
            schema=_PASTED_CHART_SCHEMA,
            image_bytes=image_bytes,
            media_type=media_type,
            max_output_tokens=1800,
        )
        logger.info("LLM result: %s", json.dumps(result, ensure_ascii=False))
        return result
    except Exception as exc:
        logger.warning("LLM tag+categorize failed: %s", exc)
        return {"tag": user_hint or "Pasted chart", "caption": "", "broker": "",
                "date": "", "chart_type": "", "category": "General Memory",
                "subcategory": "Overall memory market"}


@router.post("/paste-chart")
async def paste_chart(
    image: UploadFile = File(...),
    user_hint: str = Form(""),
    category: str = Form(""),
    subcategory: str = Form(""),
):
    """Accept a pasted image. If category/subcategory provided, use those; otherwise LLM auto-categorizes."""
    content = await image.read()
    if len(content) > MAX_IMAGE_SIZE:
        raise HTTPException(400, "Image too large (max 10MB)")
    if not content:
        raise HTTPException(400, "Empty image")

    # Determine extension & media type
    ext = ".png"
    media_type = "image/png"
    fname = (image.filename or "").lower()
    if fname.endswith(".jpg") or fname.endswith(".jpeg"):
        ext = ".jpg"; media_type = "image/jpeg"
    elif fname.endswith(".webp"):
        ext = ".webp"; media_type = "image/webp"
    elif content[:4] == b'\xff\xd8\xff\xe0' or content[:4] == b'\xff\xd8\xff\xe1':
        ext = ".jpg"; media_type = "image/jpeg"

    # Content-hash filename: same image bytes → same filename → same image_path
    # → DB row already exists → dedup hit. SHA256-16 (16 hex chars = 64 bits)
    # is collision-safe for any realistic paste-chart volume.
    paste_dir = DATA_DIR / "_pasted"
    paste_dir.mkdir(parents=True, exist_ok=True)
    img_hash = hashlib.sha256(content).hexdigest()[:16]
    filename = f"paste_{img_hash}{ext}"
    save_path = paste_dir / filename
    rel_path = f"_pasted/{filename}"
    file_id = img_hash  # also used downstream for the RAG chart_file name

    # Dedup probe — has this exact image been pasted before?
    db = await get_db()
    cur = await db.execute(
        """SELECT ch.id, ch.tag, ch.caption, ch.broker, ch.chart_date,
                  cat.name AS category_name, sc.name AS subcategory_name
           FROM charts ch
           LEFT JOIN subcategories sc ON ch.subcategory_id = sc.id
           LEFT JOIN categories cat ON sc.category_id = cat.id
           WHERE ch.image_path = ?
           LIMIT 1""",
        (rel_path,),
    )
    existing = await cur.fetchone()
    if existing:
        logger.info("paste-chart dedup hit: %s already in gallery (chart_id=%s)",
                    rel_path, existing["id"])
        # File should already exist; defensive re-write if it was removed.
        if not save_path.exists():
            save_path.write_bytes(content)
        rag_indexed = 0
        rag_image_url = f"/charts/{rel_path}"
        try:
            from ...services.fact_pool import index_pasted_chart
            rag_chart_file = f"memchart_{file_id}{ext}"
            rag_caption = " - ".join(
                x for x in (existing["tag"] or "Pasted chart", existing["caption"] or "")
                if x
            )
            rag = await index_pasted_chart(
                content, chart_file=rag_chart_file, caption=rag_caption,
                broker=existing["broker"] or "", date=existing["chart_date"] or "",
                page=0,
            )
            rag_indexed = rag.get("indexed", 0)
            rag_image_url = rag.get("image_url", "") or rag_image_url
        except EsRagError:
            raise
        except Exception as exc:
            logger.warning("paste-chart dedup RAG repair failed: %s", exc)
        return {
            "id": existing["id"],
            "image_path": rel_path,
            "tag": existing["tag"] or "Pasted chart",
            "broker": existing["broker"] or "",
            "date": existing["chart_date"] or "",
            "category": existing["category_name"] or "",
            "subcategory": existing["subcategory_name"] or "",
            "caption": existing["caption"] or "",
            "rag_indexed": bool(rag_indexed),
            "rag_image_url": rag_image_url,
            "deduped": True,
            "message": "Already in your gallery — same image was pasted before "
                       "(no duplicate row; RAG index checked/repaired).",
        }

    # First time we've seen this image: write file + insert initial gallery row.
    if not save_path.exists():
        save_path.write_bytes(content)
    pdf_id = await queries.insert_source_pdf("__pasted__", broker="")
    chart_id = await queries.insert_chart(
        source_pdf_id=pdf_id, image_path=rel_path,
        page_number=0, region_type="pasted",
    )

    # LLM tag + categorize
    category_tree = await queries.get_category_tree()
    llm = await _llm_tag_and_categorize(content, media_type, user_hint, category_tree)

    tag = llm.get("tag") or user_hint or "Pasted chart"
    broker = normalize_broker(llm.get("broker", ""))
    chart_date = llm.get("date", "")

    # Default date to today if LLM couldn't detect one
    if not chart_date or chart_date.strip() == "":
        chart_date = _today_str()
    date_int = _parse_date_int(chart_date)
    if date_int == 0:
        date_int = _today_int()

    # User-chosen section overrides LLM
    final_category = category.strip() if category.strip() else llm.get("category", "General Memory")
    final_subcategory = subcategory.strip() if subcategory.strip() else llm.get("subcategory", "Overall memory market")

    sub_id = await queries.get_or_create_subcategory(final_category, final_subcategory)

    estimates = llm.get("estimates", "") or ""

    await queries.update_chart_metadata(
        chart_id,
        tag=tag,
        caption=llm.get("caption", ""),
        chart_type=llm.get("chart_type", ""),
        broker=broker,
        chart_date=chart_date,
        chart_date_int=date_int,
        subcategory_id=sub_id,
        quality_status="approved",
        estimates=estimates,
    )

    # Link companies
    companies = llm.get("companies", [])
    if not companies or companies == [""]:
        companies = ["General"]
    await queries.link_chart_companies(chart_id, companies)

    # Index into ES RAG as a retrievable chart and use Memory's authenticated
    # asset proxy for gallery/chat display. The caption blends
    # the tag + 1-sentence caption so it's findable by semantic query.
    rag_indexed = 0
    rag_image_url = ""
    try:
        from ...services.fact_pool import index_pasted_chart
        rag_chart_file = f"memchart_{file_id}{ext}"
        rag_caption = " — ".join(x for x in (tag, llm.get("caption", "")) if x) or tag
        rag = await index_pasted_chart(
            content, chart_file=rag_chart_file, caption=rag_caption,
            broker=broker, date=chart_date, page=0,
        )
        rag_indexed = rag.get("indexed", 0)
        rag_image_url = rag.get("image_url", "")
    except EsRagError:
        raise
    except Exception as exc:
        logger.warning("Failed to index pasted chart into RAG: %s", exc)

    return {
        "id": chart_id,
        "image_path": rel_path,
        "tag": tag,
        "broker": broker,
        "date": chart_date,
        "category": final_category,
        "subcategory": final_subcategory,
        "caption": llm.get("caption", ""),
        "rag_indexed": bool(rag_indexed),
        "rag_image_url": rag_image_url,
        "message": ("Chart saved, categorized, and added to the research pool (RAG)"
                    if rag_indexed else "Chart saved and categorized"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Paste TEXT  →  doc_type='user_input' chunk in the shared fact pool
# ─────────────────────────────────────────────────────────────────────────────
# ES RAG exposes /api/rag/inject-fact for user-curated notes. They land as
# `doc_type='user_input'`
# chunks alongside sellside research, and the chat retrieval surfaces them
# whenever retrieval finds them relevant. Defaults follow the shared
# convention: ticker="USER" sentinel when unattributed, sector pinned to
# memory_semiconductors so the note joins the same recall pool as memory PDFs.


async def _auto_extract_text_metadata(text: str, user_hint: str) -> dict:
    """Tiny Haiku pass to suggest (broker, ticker, date, title, sector,
    company_tags, kpi_tags) from the pasted text. User-supplied values WIN —
    this only fills gaps. Returns empty dict on any failure / no API key."""
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return {}
    from ...services.openai_research import AsyncResearchClient  # type: ignore
    client = AsyncResearchClient(api_key=api_key)

    today = _today_str()
    prompt = f"""You are tagging a user-pasted research note for the memory-industry fact pool.

USER HINT (may be empty): {user_hint or "(none)"}
TODAY'S DATE: {today}

PASTED NOTE (verbatim):
---
{text[:4000]}
---

Extract structured metadata for indexing. Return STRICT JSON, no markdown:
{{
  "title": "<short 2-8 word label (e.g. 'JPM DDR5 May spot update')>",
  "broker": "<research firm / analyst short name if cited or quoted, e.g. UBS, GS, JPM, Morgan Stanley, Arete, TrendForce, BofA, Bernstein. Empty string if it is the user's own note.>",
  "ticker": "<bare BBG code for the SPECIFIC memory ticker the note is about, if any. Use 'MU' for Micron, '005930' for Samsung, '000660' for SK Hynix, '285A' for Kioxia, 'WDC' for Western Digital. Empty string if the note is industry-wide / not company-specific.>",
  "date": "<the date the note's information PERTAINS to, YYYY-MM-DD. If you cannot read a date, use today's date {today}.>",
  "company_tags": [<list of memory manufacturer short names mentioned: Samsung, SK Hynix, Micron, Kioxia, Western Digital, Nanya, Winbond, CXMT, YMTC. Empty list if none.>],
  "kpi_tags": [<list of KPI names mentioned: e.g. "hbm_tam", "dram_contract_asp", "nand_bit_demand", "wafer_capacity", "capex". Use lower_snake_case. Empty list if none.>],
  "topic_tags": [<list of analyst-section topics mentioned: e.g. "supply_demand_balance", "pricing", "inventory", "yield", "capex", "hbm_demand". Empty list if none.>]
}}
"""
    try:
        async def _call() -> str:
            async with client.messages.stream(
                model=_HAIKU_MODEL, max_tokens=400, temperature=0,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                resp = await stream.get_final_message()
            return "".join(getattr(b, "text", "") for b in resp.content).strip()
        raw = await _call()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return {}
        return json.loads(m.group(0))
    except Exception as exc:
        logger.warning("paste-text metadata extraction failed: %s", exc)
        return {}


@router.post("/paste-text")
async def paste_text(
    text: str = Form(...),
    user_hint: str = Form(""),
    title: str = Form(""),
    ticker: str = Form(""),
    broker: str = Form(""),
    date_pertains_to: str = Form(""),  # YYYY-MM-DD; defaults to today
    sector: str = Form("memory_semiconductors"),
    auto_tag: bool = Form(True),
):
    """Paste a free-text note into the shared fact pool as a
    ``doc_type='user_input'`` chunk.

    User-supplied metadata (title/ticker/broker/date) ALWAYS wins. When
    `auto_tag=True` (default) and any of those fields are empty, a small
    Haiku pass infers them from the text — ticker/broker only when the note
    cites them; sector pinned to memory_semiconductors so the note joins
    the same retrieval pool as memory PDFs.

    Returns ``{ok, fact_id, chunks, title, ticker, broker, date, message}``.
    Synchronous (sub-second after the optional Haiku tag call)."""
    text = (text or "").strip()
    if not text:
        raise HTTPException(400, "text is required")
    if len(text) > 50_000:
        raise HTTPException(413, "text too long (max 50k chars)")

    # Optional auto-tag pass — only when any metadata slot is empty.
    auto: dict = {}
    needs_tag = auto_tag and not all((title.strip(), ticker.strip(),
                                      broker.strip(), date_pertains_to.strip()))
    if needs_tag:
        auto = await _auto_extract_text_metadata(text, user_hint)

    final_title = (title or auto.get("title") or text[:60]).strip()
    final_ticker = (ticker or auto.get("ticker") or "").strip() or "USER"
    final_broker = normalize_broker((broker or auto.get("broker") or "").strip())
    final_date = (date_pertains_to or auto.get("date") or "").strip() or _today_str()
    # Date sanity: enforce YYYY-MM-DD, else fall back to today
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", final_date):
        final_date = _today_str()
    final_sector = (sector or "memory_semiconductors").strip()

    company_tags = list(auto.get("company_tags") or [])
    kpi_tags = list(auto.get("kpi_tags") or [])
    topic_tags = list(auto.get("topic_tags") or [])

    client = get_es_rag_gateway()
    res = await asyncio.to_thread(
        client.inject_fact,
        text, date=final_date, sector=final_sector,
        ticker=final_ticker, broker=final_broker, title=final_title,
        company_tags=company_tags, niche_sector_tags=[final_sector],
        industry_tags=[final_sector], kpi_tags=kpi_tags, topic_tags=topic_tags,
        source="memory_chart_paste",
    )
    used_path = "http_authed"

    # Accept nested and top-level result fields across supported contracts.
    inner = (res or {}).get("result") if isinstance((res or {}).get("result"), dict) else (res or {})
    if not (res or {}).get("ok"):
        return {"ok": False, "reason": (inner or {}).get("error") or (res or {}).get("reason") or "indexed 0 chunks",
                "title": final_title, "ticker": final_ticker, "broker": final_broker,
                "date": final_date, "via": used_path}

    _bump_last_index_ts()
    return {
        "ok": True,
        "fact_id": inner.get("fact_id") or "",
        "chunks": int(inner.get("chunks") or 0),
        "title": final_title,
        "ticker": final_ticker,
        "broker": final_broker,
        "date": final_date,
        "sector": final_sector,
        "auto_tagged": bool(auto),
        "company_tags": company_tags,
        "kpi_tags": kpi_tags,
        "topic_tags": topic_tags,
        "via": used_path,
        "message": (f"Note added to the fact pool as a user_input chunk"
                    + (f" — auto-tagged broker={final_broker}" if (auto and final_broker) else "")
                    + (f", ticker={final_ticker}" if final_ticker != "USER" else "")),
    }
