from __future__ import annotations

import asyncio
import logging
import hashlib
import os
import re as _re
import time
import uuid
from calendar import monthrange
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response

from ..models import ChartOut, ChartUpdate
from ...db import queries
from ...paths import (
    CHARTS_DIR,
    UPLOADS_DIR,
)
from ...services import normalize_broker
from ...services.categorizer import categorize_batch
from ...services.es_rag_gateway import EsRagError, get_es_rag_gateway
from ...services.openai_chart_classifier import classifier_cache_key
from ...services.tagger import extract_estimates_batch

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/charts", tags=["charts"])

_SECTOR_ACRONYMS = {
    "ai": "AI",
    "cdmo": "CDMO",
    "cpus": "CPUs",
    "ev": "EV",
    "gpus": "GPUs",
    "idc": "IDC",
    "smb": "SMB",
}
_MEMORY_CATEGORIES = {
    "general memory",
    "ddr dram",
    "dram",
    "hbm",
    "nand",
}


def _sector_display_name(sector: str) -> str:
    words = [word for word in _re.split(r"[^a-zA-Z0-9]+", sector.strip()) if word]
    return " ".join(
        _SECTOR_ACRONYMS.get(word.lower(), word.lower() if word.lower() == "and" else word.title())
        for word in words
    )


def _rag_main_group(row: dict) -> str:
    category = str(row.get("category_name") or "").strip().casefold()
    industry = str(
        row.get("industry") or row.get("primary_industry") or ""
    ).strip()
    industry_label = str(row.get("industry_label") or "").strip()
    primary_sector = str(
        row.get("niche") or row.get("primary_niche_sector") or ""
    ).strip().lower()
    sector_tags = [
        str(tag).strip().lower()
        for tag in (row.get("niche_sector_tags") or [])
        if str(tag).strip()
    ]
    if "memory_semiconductors" in {
        primary_sector,
        *sector_tags,
    } or (category in _MEMORY_CATEGORIES and not row.get("industry")):
        return "Memory Sector"
    if industry_label:
        return industry_label
    if industry:
        return _sector_display_name(industry)
    sector = primary_sector or next(iter(sector_tags), "")
    return _sector_display_name(sector) if sector else "Unspecified Sector"

_RAG_CATEGORIZE_SYNC_LIMIT = int(os.getenv("RAG_CHART_CATEGORIZE_SYNC_LIMIT", "96"))
_RAG_INVENTORY_CACHE_TTL_SECONDS = float(os.getenv("RAG_INVENTORY_CACHE_TTL_SECONDS", "300"))
_STALE_RAG_SUBCATEGORY_LABELS = {"ES RAG charts", "Latest ES RAG charts"}


class _RagInventoryCache:
    def __init__(self, ttl_seconds: float):
        self.ttl_seconds = ttl_seconds
        self.value: list[dict] | None = None
        self.loaded_at = 0.0
        self.last_error: str | None = None
        self.lock = asyncio.Lock()
        self.refresh_task: asyncio.Task[None] | None = None

    def clear(self) -> None:
        if self.refresh_task is not None and not self.refresh_task.done():
            self.refresh_task.cancel()
        self.refresh_task = None
        self.value = None
        self.loaded_at = 0.0
        self.last_error = None

    def _is_fresh(self) -> bool:
        return self.value is not None and time.monotonic() - self.loaded_at < self.ttl_seconds

    def is_stale(self) -> bool:
        return self.value is not None and not self._is_fresh()

    async def get(self, *, force_refresh: bool = False) -> list[dict]:
        if force_refresh:
            return await self._refresh(force_refresh=True)
        if self._is_fresh():
            return self.value or []
        if self.value is not None:
            if self.refresh_task is None or self.refresh_task.done():
                self.refresh_task = asyncio.create_task(self._refresh_in_background())
            return self.value
        return await self._refresh(force_refresh=False)

    async def _refresh(self, *, force_refresh: bool) -> list[dict]:
        async with self.lock:
            if not force_refresh and self._is_fresh():
                return self.value or []
            client = get_es_rag_gateway()
            response = await asyncio.to_thread(client.chart_inventory, theme="ai")
            inventory = list(response.get("hits") or [])
            self.value = inventory
            self.loaded_at = time.monotonic()
            self.last_error = None
            return inventory

    async def _refresh_in_background(self) -> None:
        try:
            await self._refresh(force_refresh=True)
        except Exception as exc:
            self.last_error = type(exc).__name__
            logger.warning("Background ES chart inventory refresh failed: %s", exc)


_rag_inventory_cache = _RagInventoryCache(_RAG_INVENTORY_CACHE_TTL_SECONDS)


class _RagGalleryBaseCache:
    """Cache remote row shaping while leaving local visibility/overrides live."""

    def __init__(self):
        self.value: list[dict] | None = None
        self.source_loaded_at = 0.0
        self.lock = asyncio.Lock()
        self.refresh_task: asyncio.Task[None] | None = None

    def clear(self) -> None:
        if self.refresh_task is not None and not self.refresh_task.done():
            self.refresh_task.cancel()
        self.refresh_task = None
        self.value = None
        self.source_loaded_at = 0.0

    async def get(
        self,
        inventory: list[dict],
        *,
        source_loaded_at: float,
        force_refresh: bool = False,
    ) -> list[dict]:
        if force_refresh or self.value is None:
            return await self._refresh(
                inventory,
                source_loaded_at=source_loaded_at,
                force_refresh=force_refresh,
            )
        if self.source_loaded_at == source_loaded_at:
            return self.value
        if self.refresh_task is None or self.refresh_task.done():
            self.refresh_task = asyncio.create_task(
                self._refresh_in_background(inventory, source_loaded_at)
            )
        return self.value

    async def _refresh(
        self,
        inventory: list[dict],
        *,
        source_loaded_at: float,
        force_refresh: bool,
    ) -> list[dict]:
        async with self.lock:
            if (
                not force_refresh
                and self.value is not None
                and self.source_loaded_at == source_loaded_at
            ):
                return self.value
            rows = _shape_rag_inventory_rows(inventory)
            await _apply_rag_categories(rows)
            self.value = rows
            self.source_loaded_at = source_loaded_at
            return rows

    async def _refresh_in_background(
        self,
        inventory: list[dict],
        source_loaded_at: float,
    ) -> None:
        try:
            await self._refresh(
                inventory,
                source_loaded_at=source_loaded_at,
                force_refresh=True,
            )
        except Exception as exc:
            logger.warning("Background RAG gallery row refresh failed: %s", exc)


_rag_gallery_base_cache = _RagGalleryBaseCache()

DATA_DIR = CHARTS_DIR
SOURCE_PDF_ROOTS = (UPLOADS_DIR,)
_COVERAGE_REL_ROOT = Path("Equity Team") / "Stocks" / "Coverage"
_SOURCE_PDF_EXCLUDE_PARTS = {
    ".git", ".venv", "node_modules", "__pycache__", "_pdf_chart_cache",
    "_backfill_charts", "_matplotlib_cache", "_bbg_chart_cache",
}


def _local_env_value(key: str) -> str:
    """Read a source-document root from Memory Tracker's environment."""
    return os.getenv(key, "").strip().strip('"').strip("'")


def _source_pdf_roots() -> list[Path]:
    roots = list(SOURCE_PDF_ROOTS)
    mirror = _local_env_value("SELLSIDE_LOCAL_MIRROR")
    if mirror:
        roots.append(Path(mirror))
    sp_root = _local_env_value("SHAREPOINT_LOCAL_ROOT")
    if sp_root:
        roots.append(Path(sp_root) / _COVERAGE_REL_ROOT)
    seen: set[str] = set()
    out: list[Path] = []
    for root in roots:
        try:
            resolved = str(root.resolve())
        except Exception:
            resolved = str(root)
        if resolved.lower() not in seen:
            seen.add(resolved.lower())
            out.append(root)
    return out


def _safe_pdf_basename(filename: str) -> str:
    return Path((filename or "").replace("\\", "/")).name.strip()


@lru_cache(maxsize=2048)
def _resolve_source_pdf(filename: str) -> str:
    """Resolve a cited source filename to an on-disk PDF.

    RAG payloads store filenames, not absolute source paths. Use exact
    case-insensitive basename matching across the controlled local document
    roots only. No fuzzy matching: a missing source should 404 rather than open
    the wrong broker report.
    """
    target = _safe_pdf_basename(filename)
    if not target or not target.lower().endswith(".pdf"):
        return ""
    target_low = target.lower()

    for root in _source_pdf_roots():
        if not root.is_dir():
            continue
        direct = root / target
        if direct.is_file():
            return str(direct.resolve())

    for root in _source_pdf_roots():
        if not root.is_dir():
            continue
        try:
            for p in root.rglob("*.pdf"):
                parts = {part.lower() for part in p.parts}
                if parts & _SOURCE_PDF_EXCLUDE_PARTS:
                    continue
                if p.name.lower() == target_low:
                    return str(p.resolve())
        except Exception as exc:
            logger.debug("source PDF scan skipped for %s: %s", root, exc)
    return ""


def _parse_date_int(date_str: str) -> int:
    m = _re.match(r"(\d{4})-?(\d{2})-?(\d{2})", date_str or "")
    if m:
        return int(m.group(1)) * 10000 + int(m.group(2)) * 100 + int(m.group(3))
    m = _re.match(r"(\d{4})-?(\d{2})$", date_str or "")
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        return y * 10000 + mo * 100 + monthrange(y, mo)[1]
    return 0


def _clean_rag_caption(text: str) -> str:
    text = _re.sub(r"^\s*(?:\[CHART(?:_DATA)?\]\s*)+", "", text or "").strip()
    text = _re.sub(r"\s+", " ", text)
    return text[:240]


def _rag_category(text: str) -> str:
    t = (text or "").lower()
    if any(x in t for x in (
        "roic", "return on invested capital", "return on capital",
        "return on investment", "free cash flow", "fcf", "capex intensity",
        "capital intensity",
    )):
        return "Hyperscaler Economics"
    if (
        any(x in t for x in (
            "data center", "datacenter", "data centre", "datacentre",
            "gw build", "gw capacity", "gigawatt", "dc build", "dc power",
            "provisioning", "power demand", "power capacity", "hyperscaler",
            "cloud capex", "ai capex", "ai infrastructure",
        ))
        and any(x in t for x in ("ai", "gpu", "hyperscaler", "cloud", "data center", "datacenter", "data centre", "datacentre"))
    ):
        return "AI Infrastructure Buildout"
    if any(x in t for x in ("inventory", "inventories", "dio", "weeks of supply")):
        return "Inventory"
    if "spot" in t:
        return "Spot Pricing & Spot-Contract Spread"
    if any(x in t for x in ("contract", "asp", "pricing", "price", "ddr4", "ddr5", "lpddr", "ufs", "emmc")):
        return "Contract Pricing Trend"
    if any(x in t for x in ("hbm", "hbm3", "hbm4", "stack", "ai memory")):
        return "HBM Demand & Capacity"
    if any(x in t for x in ("capex", "wfe", "cleanroom", "equipment spend")):
        return "Capex / WFE / Cleanroom Expansion"
    if any(x in t for x in ("wafer", "wspm", "kwspm", "capacity", "node migration", "fab")):
        return "Wafer Starts by Supplier & Node"
    if any(x in t for x in ("utilization", "utilisation", "shutdown", "optic")):
        return "Utilization Rates"
    if any(x in t for x in ("yield", "cycle time", "die penalty")):
        return "Yield & Cycle-Time Commentary"
    if any(x in t for x in ("bit demand", "bit supply", "sufficiency", "supply demand", "demand growth", "supply growth")):
        return "Bit Supply vs Bit Demand Growth"
    if any(x in t for x in ("server", "mobile", "pc", "smartphone", "auto", "content per", "end-market")):
        return "End-Market Demand × Memory Content"
    return "General Memory"


def _rag_subcategory(text: str) -> str:
    t = (text or "").lower()
    if any(x in t for x in ("roic", "return on invested capital", "return on capital", "return on investment")):
        return "ROIC / returns"
    if any(x in t for x in ("free cash flow", "fcf")):
        return "Free cash flow"
    if any(x in t for x in ("capex intensity", "capital intensity")):
        return "Capex intensity"
    if any(x in t for x in ("data center", "datacenter", "data centre", "datacentre", "gw build", "gw capacity", "gigawatt", "provisioning")):
        return "Datacenter GW build"
    if any(x in t for x in ("hyperscaler", "cloud capex", "ai capex", "ai infrastructure")):
        return "Hyperscaler capex"
    if any(x in t for x in ("power demand", "power capacity", "dc power")):
        return "Power capacity"
    if any(x in t for x in ("hbm", "hbm3", "hbm4")):
        if any(x in t for x in ("share", "market share")):
            return "HBM market share"
        if any(x in t for x in ("tam", "revenue", "$bn", "sales")):
            return "HBM TAM"
        if any(x in t for x in ("capacity", "wspm", "kwspm", "wafer", "supply")):
            return "HBM capacity"
        if any(x in t for x in ("roadmap", "hbm4", "hbm3e", "stack")):
            return "HBM roadmap"
        if any(x in t for x in ("price", "pricing", "asp")):
            return "HBM pricing"
        return "HBM supply/demand"
    if any(x in t for x in ("nand", "flash", "ssd", "ufs", "emmc")):
        if any(x in t for x in ("price", "pricing", "asp", "contract", "spot")):
            return "NAND pricing"
        if any(x in t for x in ("bit", "demand", "supply", "sufficiency")):
            return "NAND supply/demand"
        if any(x in t for x in ("share", "market share")):
            return "NAND market share"
        if any(x in t for x in ("capex", "wfe", "equipment")):
            return "NAND capex"
        if any(x in t for x in ("inventory", "dio", "weeks")):
            return "NAND inventory"
        return "NAND general"
    if any(x in t for x in ("ddr", "ddr4", "ddr5", "lpddr")):
        if any(x in t for x in ("price", "pricing", "asp", "contract", "spot", "ceiling", "floor")):
            return "DDR pricing"
        if any(x in t for x in ("adoption", "transition", "mix")):
            return "DDR5 adoption"
        return "DDR DRAM general"
    if "dram" in t:
        if any(x in t for x in ("price", "pricing", "asp", "contract", "spot")):
            return "DRAM pricing"
        if any(x in t for x in ("bit", "demand", "supply", "sufficiency")):
            return "DRAM supply/demand"
        if any(x in t for x in ("share", "market share")):
            return "DRAM market share"
        if any(x in t for x in ("capex", "wfe", "equipment")):
            return "DRAM capex"
        if any(x in t for x in ("inventory", "dio", "weeks")):
            return "DRAM inventory"
        return "DRAM general"
    if any(x in t for x in ("wafer", "wspm", "kwspm", "capacity", "fab")):
        return "Wafer starts"
    if any(x in t for x in ("capex", "wfe", "cleanroom", "equipment spend")):
        return "Capital spending"
    if any(x in t for x in ("inventory", "dio", "weeks of supply")):
        return "Inventory cycle"
    if any(x in t for x in ("server", "mobile", "pc", "smartphone", "auto", "content per", "end-market")):
        return "End-market demand"
    return "Overall memory market"


def _caption_hash(row: dict) -> str:
    text = "|".join(str(row.get(k) or "") for k in ("tag", "caption", "source_filename", "image_path"))
    return hashlib.sha1(text.encode("utf-8", errors="ignore")).hexdigest()


def _virtual_es_chart_id(row: dict) -> int:
    identity = str(
        row.get("chunk_id")
        or row.get("asset_id")
        or row.get("image_url")
        or "|".join(
            str(row.get(key) or "")
            for key in ("filename", "page_number", "chart_file")
        )
    )
    qdrant_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, identity))
    digest = hashlib.sha1(qdrant_id.encode("utf-8")).hexdigest()[:8]
    return -int(digest, 16)


def _es_chart_asset_relpath(image_url: str) -> str:
    prefix = "/api/analysis/chart-asset/"
    clean = str(image_url or "").replace("\\", "/")
    if not clean.startswith(prefix):
        return ""
    relpath = clean[len(prefix):].lstrip("/")
    if not relpath or ".." in relpath.split("/"):
        return ""
    return relpath


def _normalize_chart_row_broker(row: dict) -> dict:
    row = dict(row)
    row["broker"] = normalize_broker(row.get("broker") or "")
    return row


def _page_from_chart_relpath(relpath: str) -> int:
    m = _re.search(r"(?:_chart_p|_p)(\d+)_", relpath or "", _re.IGNORECASE)
    return int(m.group(1)) if m else 0


def _rag_inventory_row_to_gallery(row: dict) -> dict | None:
    filename = row.get("filename", "") or ""
    if filename.startswith("memory_chart_paste/") or filename.startswith("esrag_inject/"):
        return None
    chart_relpath = _es_chart_asset_relpath(row.get("image_url", "") or "")
    if not chart_relpath:
        return None
    caption = _clean_rag_caption(row.get("caption", "") or "")
    tag = caption.split(".", 1)[0][:120] if caption else (row.get("chart_file") or "RAG chart")
    chart_date = row.get("date", "") or ""
    page_num = int(
        row.get("page_number")
        or row.get("page")
        or _page_from_chart_relpath(chart_relpath)
        or 0
    )
    industry = str(
        row.get("industry") or row.get("primary_industry") or ""
    ).strip()
    industry_label = str(row.get("industry_label") or "").strip()
    niche = str(
        row.get("niche") or row.get("primary_niche_sector") or ""
    ).strip()
    industry_tags = row.get("industry_tags") or ([industry] if industry else [])
    niche_sector_tags = row.get("niche_sector_tags") or ([niche] if niche else [])
    is_memory_sector = niche == "memory_semiconductors" or (
        "memory_semiconductors" in {
            str(tag).strip().lower() for tag in niche_sector_tags
        }
    )
    if industry and not is_memory_sector:
        category_name = industry_label or _sector_display_name(industry)
        subcategory_name = _sector_display_name(niche) if niche else "Sector Wide"
    else:
        category_name = _rag_category(f"{caption} {filename}")
        subcategory_name = _rag_subcategory(f"{caption} {filename}")
    return {
        "id": _virtual_es_chart_id(row),
        "source_pdf_id": 0,
        "subcategory_id": None,
        "image_path": chart_relpath,
        "image_url": "/api/charts/rag-asset/" + quote(chart_relpath, safe="/"),
        "page_number": page_num,
        "region_type": "rag_chart",
        "tag": tag,
        "caption": caption,
        "chart_type": "rag_chart",
        "broker": normalize_broker(row.get("broker") or ""),
        "chart_date": chart_date,
        "chart_date_int": int(row.get("date_int") or _parse_date_int(chart_date) or 0),
        "estimates": "",
        "quality_status": "approved",
        "category_name": category_name,
        "subcategory_name": subcategory_name,
        "source_filename": filename,
        "is_rag": True,
        "content_type": row.get("content_type", ""),
        "media_type": row.get("media_type", ""),
        "doc_type": row.get("doc_type", ""),
        "source_doc_type": row.get("source_doc_type", ""),
        "source_rel_path": row.get("source_rel_path", ""),
        "citation_url": row.get("citation_url", ""),
        "citation_markdown": row.get("citation_markdown", ""),
        "primary_ticker": row.get("primary_ticker", ""),
        "primary_company": row.get("primary_company", ""),
        "industry": industry,
        "industry_label": industry_label,
        "niche": niche,
        "primary_industry": industry,
        "primary_niche_sector": niche,
        "industry_tags": industry_tags,
        "niche_sector_tags": niche_sector_tags,
        "subindustry_tags": row.get("subindustry_tags") or [],
        "kpi_tags": row.get("kpi_tags") or [],
        "theme_tags": row.get("theme_tags") or [],
        "catalyst_tags": row.get("catalyst_tags") or [],
        "topic_tags": row.get("topic_tags") or [],
        "company_tags": row.get("company_tags") or [],
        "period_tags": row.get("period_tags") or [],
        "geo_tags": row.get("geo_tags") or [],
        "unit_tags": row.get("unit_tags") or [],
    }


async def _ensure_rag_category_cache_table() -> None:
    from ...db.connection import get_db
    db = await get_db()
    await db.execute(
        """CREATE TABLE IF NOT EXISTS rag_chart_categories (
               virtual_id       INTEGER PRIMARY KEY,
               caption_hash     TEXT NOT NULL,
               category_name    TEXT NOT NULL,
               subcategory_name TEXT NOT NULL,
               model            TEXT NOT NULL DEFAULT 'haiku',
               updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
           )"""
    )
    await db.commit()


async def _load_rag_category_cache(rows: list[dict]) -> dict[int, dict]:
    await _ensure_rag_category_cache_table()
    ids = [int(r.get("id") or 0) for r in rows if int(r.get("id") or 0)]
    if not ids:
        return {}
    from ...db.connection import get_db
    db = await get_db()
    found: dict[int, dict] = {}
    for offset in range(0, len(ids), 800):
        chunk = ids[offset:offset + 800]
        placeholders = ",".join("?" for _ in chunk)
        cache_rows = await db.execute_fetchall(
            f"""SELECT virtual_id, caption_hash, category_name, subcategory_name
                FROM rag_chart_categories
                WHERE virtual_id IN ({placeholders})""",
            chunk,
        )
        for row in cache_rows:
            found[int(row["virtual_id"])] = dict(row)
    return found


async def _save_rag_category_cache(rows: list[dict]) -> None:
    if not rows:
        return
    await _ensure_rag_category_cache_table()
    from ...db.connection import get_db
    db = await get_db()
    for row in rows:
        await db.execute(
            """INSERT INTO rag_chart_categories
               (virtual_id, caption_hash, category_name, subcategory_name, model)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(virtual_id) DO UPDATE SET
                   caption_hash=excluded.caption_hash,
                   category_name=excluded.category_name,
                   subcategory_name=excluded.subcategory_name,
                   model=excluded.model,
                   updated_at=datetime('now')""",
            (
                int(row.get("id") or 0),
                _caption_hash(row),
                row.get("category_name") or "General Memory",
                row.get("subcategory_name") or "Overall memory market",
                classifier_cache_key(),
            ),
        )
    await db.commit()


async def _apply_rag_categories(rows: list[dict]) -> None:
    if not rows:
        return
    cache = await _load_rag_category_cache(rows)
    uncached: list[dict] = []
    for row in rows:
        if _rag_main_group(row) != "Memory Sector":
            continue
        virtual_id = int(row.get("id") or 0)
        cached = cache.get(virtual_id)
        if cached and cached.get("caption_hash") == _caption_hash(row):
            row["category_name"] = cached.get("category_name") or row.get("category_name")
            row["subcategory_name"] = cached.get("subcategory_name") or row.get("subcategory_name")
        else:
            uncached.append(row)

    if not uncached or _RAG_CATEGORIZE_SYNC_LIMIT <= 0:
        return
    if not os.getenv("OPENAI_API_KEY", "").strip():
        return

    to_categorize = uncached[:_RAG_CATEGORIZE_SYNC_LIMIT]
    try:
        category_tree = await queries.get_category_tree()
        infos = [
            {"tag": r.get("tag", ""), "caption": r.get("caption", "")}
            for r in to_categorize
        ]
        results = await categorize_batch(infos, category_tree)
    except Exception as exc:
        logger.warning("RAG chart categorization failed: %s", exc)
        return

    changed: list[dict] = []
    for row, cat in zip(to_categorize, results):
        if cat.category == "General Memory" and cat.subcategory == "Overall memory market":
            continue
        row["category_name"] = cat.category or row.get("category_name")
        row["subcategory_name"] = cat.subcategory or row.get("subcategory_name")
        changed.append(row)
    if changed:
        await _save_rag_category_cache(changed)
        logger.info("RAG chart categorization cached %d virtual charts", len(changed))


def _shape_rag_inventory_rows(inventory: list[dict]) -> list[dict]:
    rows = []
    seen: set[tuple[str, int, str]] = set()
    for item in inventory:
        shaped = _rag_inventory_row_to_gallery(item)
        if not shaped:
            continue
        key = (
            shaped.get("source_filename", "") or "",
            int(shaped.get("page_number") or 0),
            shaped.get("image_path", "") or "",
        )
        if key in seen:
            continue
        seen.add(key)
        rows.append(shaped)
    return rows


async def _get_recent_rag_chart_rows(*, force_refresh: bool = False) -> list[dict]:
    inventory = await _rag_inventory_cache.get(force_refresh=force_refresh)
    base_rows = await _rag_gallery_base_cache.get(
        inventory,
        source_loaded_at=_rag_inventory_cache.loaded_at,
        force_refresh=force_refresh,
    )
    if not base_rows:
        return []
    rows = [dict(row) for row in base_rows]
    if not rows:
        return rows
    try:
        from ...db.connection import get_db
        db = await get_db()
        hidden_rows = await db.execute_fetchall("SELECT virtual_id FROM rag_chart_hidden")
        hidden = {int(r["virtual_id"]) for r in hidden_rows}
    except Exception as exc:
        logger.warning("Failed to load hidden RAG chart ids: %s", exc)
        hidden = set()
    visible = [r for r in rows if int(r.get("id") or 0) not in hidden]
    overrides = await _load_rag_chart_overrides()
    for row in visible:
        _apply_rag_chart_override(row, overrides.get(int(row.get("id") or 0), {}))
    return visible


async def _load_rag_chart_overrides() -> dict[int, dict]:
    try:
        from ...db.connection import get_db
        db = await get_db()
        rows = await db.execute_fetchall("SELECT * FROM rag_chart_overrides")
    except Exception as exc:
        logger.warning("Failed to load RAG chart overrides: %s", exc)
        return {}
    result: dict[int, dict] = {}
    for r in rows:
        row = dict(r)
        virtual_id = int(row.pop("virtual_id"))
        row.pop("updated_at", None)
        result[virtual_id] = row
    return result


def _apply_rag_chart_override(row: dict, override: dict | None) -> dict:
    if not override:
        return row
    for key in (
        "tag", "caption", "chart_type", "broker", "chart_date",
        "chart_date_int", "estimates", "category_name", "subcategory_name",
    ):
        if key in override and override[key] is not None:
            if key == "subcategory_name" and str(override[key]) in _STALE_RAG_SUBCATEGORY_LABELS:
                continue
            row[key] = normalize_broker(override[key]) if key == "broker" else override[key]
    row["broker"] = normalize_broker(row.get("broker") or "")
    return row


async def _get_rag_chart_by_virtual_id(chart_id: int) -> dict | None:
    rows = await _get_recent_rag_chart_rows()
    chart = next((r for r in rows if int(r.get("id") or 0) == chart_id), None)
    if not chart:
        return None
    return chart


async def _save_rag_chart_override(chart_id: int, updates: dict) -> None:
    if not updates:
        return
    from ...db.connection import get_db
    allowed = {
        "tag", "caption", "chart_type", "broker", "chart_date",
        "chart_date_int", "estimates", "category_name", "subcategory_name",
    }
    clean = {k: v for k, v in updates.items() if k in allowed}
    if not clean:
        return
    db = await get_db()
    cols = ["virtual_id", *clean.keys()]
    placeholders = ", ".join("?" for _ in cols)
    assignments = ", ".join(f"{k} = excluded.{k}" for k in clean.keys())
    sql = (
        f"INSERT INTO rag_chart_overrides ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(virtual_id) DO UPDATE SET {assignments}, updated_at = datetime('now')"
    )
    await db.execute(sql, [chart_id, *clean.values()])
    await db.commit()


@router.get("", response_model=list[ChartOut])
async def list_charts(
    category: str | None = Query(None),
    broker: str | None = Query(None),
    date_from: int | None = Query(None),
    date_to: int | None = Query(None),
    search: str | None = Query(None),
):
    rows = await queries.get_charts_flat(
        category_name=category,
        broker=None,
        date_from=date_from,
        date_to=date_to,
        search=search,
    )
    rows = [_normalize_chart_row_broker(r) for r in rows]
    if broker:
        want = normalize_broker(broker)
        rows = [r for r in rows if normalize_broker(r.get("broker") or "") == want]
    return rows


@router.get("/by-category")
async def charts_by_category(
    response: Response,
    refresh: bool = Query(False),
):
    response.headers["Cache-Control"] = (
        "no-store" if refresh else "private, max-age=60, stale-while-revalidate=300"
    )
    rows = [_normalize_chart_row_broker(r) for r in await queries.get_charts_by_category()]
    grouped: dict[tuple[str, str], dict] = {}
    for r in rows:
        main_group = "Memory Sector"
        cat_name = r.get("category_name") or "Uncategorized"
        sub_name = r.get("subcategory_name") or "Uncategorized"
        group_key = (main_group, cat_name)

        if group_key not in grouped:
            grouped[group_key] = {
                "main_group": main_group,
                "category": cat_name,
                "cat_order": r.get("cat_order", 99),
                "subcategories": {},
            }

        cat = grouped[group_key]
        if sub_name not in cat["subcategories"]:
            cat["subcategories"][sub_name] = {
                "subcategory": sub_name,
                "sub_order": r.get("sub_order", 99),
                "charts": [],
            }
        cat["subcategories"][sub_name]["charts"].append(r)

    # Merge recent ES RAG chart chunks into the same gallery response. These
    # are read-only virtual records: they live in ES RAG, not in
    # local SQLite, but users expect newly indexed memory PDFs to appear in
    # the Memory Chart gallery.
    for r in await _get_recent_rag_chart_rows(force_refresh=refresh):
        main_group = _rag_main_group(r)
        cat_name = r.get("category_name") or "General Memory"
        sub_name = r.get("subcategory_name") or "Overall memory market"
        group_key = (main_group, cat_name)
        if group_key not in grouped:
            grouped[group_key] = {
                "main_group": main_group,
                "category": cat_name,
                "cat_order": 98,
                "subcategories": {},
            }
        cat = grouped[group_key]
        if sub_name not in cat["subcategories"]:
            cat["subcategories"][sub_name] = {
                "subcategory": sub_name,
                "sub_order": -1,
                "charts": [],
            }
        cat["subcategories"][sub_name]["charts"].append(r)

    response.headers["X-ES-RAG-Stale"] = str(
        _rag_inventory_cache.is_stale()
    ).lower()

    result = []
    for cat in sorted(
        grouped.values(),
        key=lambda c: (
            0 if c["main_group"] == "Memory Sector" else 1,
            "" if c["main_group"] == "Memory Sector" else c["main_group"].lower(),
            c["cat_order"],
        ),
    ):
        subs = sorted(cat["subcategories"].values(), key=lambda s: s["sub_order"])
        for s in subs:
            s["charts"] = sorted(
                s["charts"],
                key=lambda c: (
                    c.get("chart_date_int") or 0,
                    c.get("broker") or "",
                    c.get("tag") or "",
                ),
                reverse=True,
            )
        result.append({
            "main_group": cat["main_group"],
            "category": cat["category"],
            "subcategories": subs,
        })
    return result


@router.get("/brokers")
async def list_brokers():
    brokers = {normalize_broker(b) for b in await queries.get_distinct_brokers()}
    try:
        for row in await _get_recent_rag_chart_rows():
            brokers.add(normalize_broker(row.get("broker") or ""))
    except Exception as exc:
        logger.warning("Failed to merge RAG brokers into broker list: %s", exc)
    return sorted((b for b in brokers if b), key=lambda s: s.lower())


# ── RAG charts (sourced from the external ES RAG service) ───────────────────
# Unlike the routes above (local SQLite + data/charts static files), these pull
# sellside chart images through the authenticated Memory proxy. Declared before
# /{chart_id} (int) so these routes win.

@router.get("/rag")
async def rag_charts(
    query: str = Query(..., min_length=1),
    top_n: int = Query(
        12,
        ge=0,
        le=500,
        description="Maximum charts to return. Use 0 with ranked=false/mode=inventory for uncapped inventory.",
    ),
    scope: str = Query("ai"),
    ranked: bool = Query(True, description="Use semantic ranking. False returns metadata inventory sorted newest-first."),
    mode: str = Query("ranked", pattern="^(ranked|inventory)$"),
    broker: str = Query("", description="Optional hard broker/source filter, e.g. Arete."),
    niche: str = Query("", description="Optional hard primary/content niche filter, e.g. memory_semiconductors."),
    industry: str = Query("", description="Optional hard primary/content broad industry filter."),
):
    """RAG chart search/inventory, each with a displayable image_url.

    Memory Chart gallery/search is intentionally scoped to ES RAG's AI-theme
    chart basket by default. Optional broker/niche/industry params can narrow
    the results, but the default gate is theme, not memory industry.

    Backward compatible default:
      /api/charts/rag?query=... -> semantic ranked top 12.

    Inventory mode:
      /api/charts/rag?query=AI%20datacenter%20capex&broker=Arete&ranked=false&top_n=0
    returns all matching displayable charts, newest-first, with no semantic
    ranking and no request cap except the server safety ceiling.
    """
    from ...services.fact_pool import retrieve_rag_charts
    use_ranked = ranked and mode != "inventory" and top_n != 0
    charts = await retrieve_rag_charts(
        query,
        top_n=top_n,
        scope="memory",
        ranked=use_ranked,
        broker=broker,
        niche=niche,
        industry=industry,
        theme="ai",
    )
    return {
        "query": query,
        "total": len(charts),
        "ranked": use_ranked,
        "capped": bool(top_n),
        "top_n": top_n,
        "scope": "ai",
        "broker": broker,
        "niche": niche,
        "industry": industry,
        "charts": charts,
    }


@router.get("/rag-asset/{relpath:path}")
async def rag_chart_asset(relpath: str):
    """Proxy an ES chart asset without exposing the ES key to the browser."""
    client = get_es_rag_gateway()
    try:
        content, media_type = await asyncio.to_thread(client.chart_asset, relpath)
    except EsRagError as exc:
        if exc.status_code == 404:
            raise HTTPException(404, "Chart asset not found") from exc
        raise
    return Response(
        content=content,
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.get("/source-pdf")
async def source_pdf(filename: str = Query(..., min_length=1)):
    """Open the exact cited source PDF when it exists locally.

    Frontend appends `#page=N` to this URL so browser PDF viewers jump to the
    cited page. The hash fragment is client-side, so the backend only resolves
    and streams the document.
    """
    target = _resolve_source_pdf(filename)
    if not target:
        raise HTTPException(404, "Source PDF not found locally")
    p = Path(target)
    if not p.is_file():
        raise HTTPException(404, "Source PDF not found locally")
    return FileResponse(
        path=str(p),
        media_type="application/pdf",
        filename=p.name,
        content_disposition_type="inline",
        headers={"Cache-Control": "private, max-age=300"},
    )


@router.get("/{chart_id}", response_model=ChartOut)
async def get_chart(chart_id: int):
    if chart_id < 0:
        chart = await _get_rag_chart_by_virtual_id(chart_id)
        if not chart:
            raise HTTPException(404, "Chart not found")
        return chart
    chart = await queries.get_chart_by_id(chart_id)
    if not chart:
        raise HTTPException(404, "Chart not found")
    return _normalize_chart_row_broker(chart)


@router.patch("/{chart_id}")
async def update_chart(chart_id: int, body: ChartUpdate):
    if chart_id < 0:
        chart = await _get_rag_chart_by_virtual_id(chart_id)
        if not chart:
            raise HTTPException(404, "Chart not found")

        updates = {}
        if body.tag is not None:
            updates["tag"] = body.tag
        if body.caption is not None:
            updates["caption"] = body.caption
        if body.chart_type is not None:
            updates["chart_type"] = body.chart_type
        if body.broker is not None:
            updates["broker"] = normalize_broker(body.broker)
        if body.chart_date is not None:
            updates["chart_date"] = body.chart_date
            m = _re.match(r"(\d{4})-(\d{2})-(\d{2})", body.chart_date)
            updates["chart_date_int"] = (
                int(m.group(1)) * 10000 + int(m.group(2)) * 100 + int(m.group(3))
                if m else _parse_date_int(body.chart_date)
            )
        if body.estimates is not None:
            updates["estimates"] = body.estimates
        if body.category_name is not None:
            updates["category_name"] = body.category_name
        if body.subcategory_name is not None:
            updates["subcategory_name"] = body.subcategory_name

        if body.recategorize and body.tag:
            try:
                category_tree = await queries.get_category_tree()
                cat_results = await categorize_batch(
                    [{"tag": body.tag, "caption": body.caption or chart.get("caption", "") or ""}],
                    category_tree,
                )
                if cat_results:
                    cat = cat_results[0]
                    updates["category_name"] = cat.category
                    updates["subcategory_name"] = cat.subcategory
                    logger.info("Re-categorized RAG chart %d: %s > %s",
                                chart_id, cat.category, cat.subcategory)
            except Exception as exc:
                logger.warning("RAG chart re-categorization failed for %d: %s", chart_id, exc)

        await _save_rag_chart_override(chart_id, updates)
        updated = await _get_rag_chart_by_virtual_id(chart_id)
        return updated

    chart = await queries.get_chart_by_id(chart_id)
    if not chart:
        raise HTTPException(404, "Chart not found")

    updates = {}
    if body.tag is not None:
        updates["tag"] = body.tag
    if body.caption is not None:
        updates["caption"] = body.caption
    if body.chart_type is not None:
        updates["chart_type"] = body.chart_type
    if body.broker is not None:
        updates["broker"] = normalize_broker(body.broker)
    if body.chart_date is not None:
        updates["chart_date"] = body.chart_date
        m = _re.match(r"(\d{4})-(\d{2})-(\d{2})", body.chart_date)
        if m:
            updates["chart_date_int"] = int(m.group(1)) * 10000 + int(m.group(2)) * 100 + int(m.group(3))
    if body.estimates is not None:
        updates["estimates"] = body.estimates

    if body.category_name and body.subcategory_name:
        sub_id = await queries.get_or_create_subcategory(body.category_name, body.subcategory_name)
        updates["subcategory_id"] = sub_id

    # LLM re-categorization when tag changes
    if body.recategorize and body.tag:
        try:
            category_tree = await queries.get_category_tree()
            new_tag = body.tag
            caption = body.caption or chart.get("caption", "") or ""
            cat_results = await categorize_batch(
                [{"tag": new_tag, "caption": caption}], category_tree
            )
            if cat_results:
                cat = cat_results[0]
                sub_id = await queries.get_or_create_subcategory(cat.category, cat.subcategory)
                updates["subcategory_id"] = sub_id
                logger.info("Re-categorized chart %d: %s > %s", chart_id, cat.category, cat.subcategory)
        except Exception as exc:
            logger.warning("Re-categorization failed for chart %d: %s", chart_id, exc)

    if updates:
        await queries.update_chart_metadata(chart_id, **updates)

    updated = await queries.get_chart_by_id(chart_id)
    return _normalize_chart_row_broker(updated or {})


@router.post("/recategorize-all")
async def recategorize_all():
    """Re-run LLM categorization on ALL charts."""
    from ...db.connection import get_db
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT ch.id, ch.tag, ch.caption FROM charts ch "
        "WHERE ch.quality_status = 'approved'"
    )
    charts_list = [dict(r) for r in rows]
    if not charts_list:
        return {"recategorized": 0}

    category_tree = await queries.get_category_tree()
    infos = [{"tag": c.get("tag", ""), "caption": c.get("caption", "")} for c in charts_list]
    results = await categorize_batch(infos, category_tree)

    changed = 0
    for chart, cat_result in zip(charts_list, results):
        sub_id = await queries.get_or_create_subcategory(cat_result.category, cat_result.subcategory)
        await queries.update_chart_metadata(chart["id"], subcategory_id=sub_id)
        companies = cat_result.companies or ["General"]
        await queries.link_chart_companies(chart["id"], companies)
        changed += 1
        logger.info("Recategorized chart %d (%s) → %s > %s, companies=%s",
                     chart["id"], chart.get("tag", ""), cat_result.category, cat_result.subcategory, companies)

    return {"recategorized": changed}


@router.post("/extract-estimates")
async def extract_estimates_all():
    """Extract estimates for all approved charts using LLM vision + PDF page text."""
    import fitz
    from ...db.connection import get_db

    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT ch.id, ch.tag, ch.caption, ch.image_path, ch.page_number, "
        "sp.filename as source_filename "
        "FROM charts ch "
        "LEFT JOIN source_pdfs sp ON ch.source_pdf_id = sp.id "
        "WHERE ch.quality_status = 'approved' "
        "ORDER BY sp.filename, ch.page_number"
    )
    charts_list = [dict(r) for r in rows]
    if not charts_list:
        return {"updated": 0}

    pdf_dir = UPLOADS_DIR

    pdf_page_texts: dict[str, dict[int, str]] = {}
    for chart in charts_list:
        fname = chart.get("source_filename", "")
        if not fname or fname == "__pasted__" or fname in pdf_page_texts:
            continue
        pdf_path = pdf_dir / fname
        if not pdf_path.exists():
            continue
        try:
            doc = fitz.open(str(pdf_path))
            texts = {}
            for i in range(doc.page_count):
                texts[i + 1] = doc[i].get_text("text")[:2000]
            doc.close()
            pdf_page_texts[fname] = texts
        except Exception as exc:
            logger.warning("Failed to extract text from %s: %s", fname, exc)

    charts_info = []
    for chart in charts_list:
        charts_info.append({
            "id": chart["id"],
            "tag": chart.get("tag", ""),
            "caption": chart.get("caption", ""),
            "path": str(DATA_DIR / chart["image_path"]),
            "page": chart.get("page_number", 0),
            "source_filename": chart.get("source_filename", ""),
        })

    current_pdf = None
    current_batch = []
    updated = 0

    async def _process_batch(batch, page_texts):
        nonlocal updated
        if not batch:
            return
        estimates = await extract_estimates_batch(batch, page_texts=page_texts)
        for info, est in zip(batch, estimates):
            if est:
                await queries.update_chart_metadata(info["id"], estimates=est)
                updated += 1
                logger.info("Estimates for chart %d (%s): %s", info["id"], info["tag"], est)

    for info in charts_info:
        pdf = info["source_filename"]
        if pdf != current_pdf:
            await _process_batch(current_batch, pdf_page_texts.get(current_pdf, {}))
            current_batch = []
            current_pdf = pdf
        current_batch.append(info)

    await _process_batch(current_batch, pdf_page_texts.get(current_pdf, {}))

    return {"updated": updated, "total": len(charts_list)}


@router.delete("/{chart_id}")
async def delete_chart(chart_id: int):
    if chart_id < 0:
        from ...db.connection import get_db
        db = await get_db()
        await db.execute(
            "INSERT OR IGNORE INTO rag_chart_hidden (virtual_id) VALUES (?)",
            (chart_id,),
        )
        await db.commit()
        return {"ok": True, "hidden": True}
    deleted = await queries.delete_chart(chart_id)
    if not deleted:
        raise HTTPException(404, "Chart not found")
    return {"ok": True}
