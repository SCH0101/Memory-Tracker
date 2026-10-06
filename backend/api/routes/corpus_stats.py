"""Lightweight introspection on the external ES RAG service.

Returns external health fields and a process-local "last write" timestamp
bumped on every successful indexing call in fact_pool.py. Cached
10s server-side to avoid unnecessary health requests.

Used by the UI to render "data updated since last view" banners on Overview
/ Model without forcing expensive Haiku regeneration."""
from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter

from ...services.es_rag_gateway import get_es_rag_gateway
from ...services.fact_pool import get_last_index_ts

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/corpus-stats", tags=["corpus-stats"])

_CACHE_TTL_S = 10.0
_cache: dict = {"data": None, "ts": 0.0}


def _stats_sync() -> dict:
    health = get_es_rag_gateway().health(detail=True)
    status = str(health.get("status") or "").lower()
    qdrant_alive = bool(health.get("qdrant_alive"))
    collection_healthy = bool(health.get("qdrant_collection_healthy"))
    return {
        "ok": status == "ok" or (
            not status and qdrant_alive and collection_healthy
        ),
        "collection": health.get("collection") or health.get("collection_name") or "",
        "last_index_ts": get_last_index_ts(),
        "qdrant_alive": qdrant_alive,
        "qdrant_collection_healthy": collection_healthy,
        "queue": health.get("queue") or {},
    }


@router.get("")
async def get_corpus_stats():
    """Return external-service health plus Memory's latest-write timestamp.

    `last_index_ts` = process-local unix timestamp of the most recent
    successful write into the fact pool by THIS server process (0.0 if no
    writes since startup — restart clears it).

    Frontend usage: when `last_index_ts` advances past the cached value, show a
    "New data — click Refresh" banner on Overview / Model. Cached 10s."""
    now = time.time()
    if _cache["data"] is not None and now - _cache["ts"] < _CACHE_TTL_S:
        return _cache["data"]
    data = await asyncio.to_thread(_stats_sync)
    _cache["data"] = data
    _cache["ts"] = now
    return data
