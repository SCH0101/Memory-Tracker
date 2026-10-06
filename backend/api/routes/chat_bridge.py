"""Ask Claude bridge for the Memory Chart workspace.

The Memory app renders ES answers. Current ES uses /api/external/answer with
enforced AI-industry filters. That endpoint is stateless: local session IDs
do not imply upstream conversational memory. Legacy servers retain their SSE path.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from ...services.es_rag_gateway import get_es_rag_gateway
from ...services.rag_settings import SOURCE_OPTIONS, get_tab_sources

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat", tags=["chat"])

MAX_HISTORY = 12

_SOURCE_FILTER_CLAUSES: dict[str, dict[str, Any]] = {
    "broker_research": {
        "doc_type": "sellside",
        "source_doc_type": "broker_research",
    },
    "expert_call": {
        "doc_type": "expert_call",
        "source_doc_type": "expert_call",
    },
    "company_presentation": {
        "doc_type": "company_presentation",
        "source_doc_type": "company_presentation",
    },
    "industry_report": {
        "doc_type": "industry_report",
        "source_doc_type": "industry_report",
    },
    "earnings_transcript": {
        "doc_type": "transcript",
        "source_doc_type": "earnings_transcript",
    },
    "user_input": {
        "$or": [
            {"doc_type": "user_input"},
            {"source_doc_type": "user_input"},
        ],
    },
    "tables": {
        "$or": [
            {"media_type": "table"},
            {
                "content_type": {
                    "$in": [
                        "table",
                        "estimate_table",
                        "valuation_exhibit",
                        "chart_values",
                    ]
                }
            },
        ],
    },
}


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    session_id: str | None = None
    filters: dict[str, Any] | None = None
    metadata_filters: dict[str, Any] | None = None


def _chat_session_id(req: ChatRequest, msgs: list[ChatMessage]) -> str:
    sid = (req.session_id or "").strip()
    if sid:
        return sid[:120]
    seed_parts: list[str] = []
    for msg in msgs:
        if msg.role == "user":
            seed_parts.append(msg.content[:500])
        if len(seed_parts) >= 3:
            break
    seed = "\n".join(seed_parts) or "memory-chat"
    digest = hashlib.sha1(seed.encode("utf-8", errors="ignore")).hexdigest()[:24]
    return f"fallback-{digest}"


def _qualify_es_markdown_links(text: str, base_url: str) -> str:
    base_url = os.getenv('ESRAG_PUBLIC_URL','').strip() or base_url
    if not text or not base_url:
        return text
    base = base_url.rstrip("/")
    return text.replace("](/api/", f"]({base}/api/")


def _and_filters(*items: dict[str, Any] | None) -> dict[str, Any] | None:
    filters = [item for item in items if item]
    if not filters:
        return None
    if len(filters) == 1:
        return filters[0]
    return {"$and": filters}


def _ask_source_filters(sources: list[str]) -> dict[str, Any] | None:
    selected = {s for s in sources if s in SOURCE_OPTIONS}
    all_sources = set(SOURCE_OPTIONS)
    if not selected or selected == all_sources:
        return None
    from ...services.es_research_filters import SOURCE_TYPES
    from ...services.es_rag_gateway import EsRagContractError
    if 'tables' in selected or 'user_input' in selected:
        raise EsRagContractError('Tables-only and user-input source filters are unavailable in the current ES document API; select supported document types or all sources',operation='filters')
    return {'document_types':list(dict.fromkeys(t for s in SOURCE_OPTIONS if s in selected for t in SOURCE_TYPES[s]))}


async def _ask_es(
    question: str,
    session_id: str,
    *,
    filters: dict[str, Any] | None,
    metadata_filters: dict[str, Any] | None,
) -> str:
    client = get_es_rag_gateway()
    project = os.getenv("MEMORY_CHAT_ES_PROJECT", "memory-tracker-project").strip()
    ticker = os.getenv("MEMORY_CHAT_ES_TICKER", "").strip()
    context_label = os.getenv(
        "MEMORY_CHAT_ES_CONTEXT_LABEL",
        "Memory Tracker Project",
    ).strip()
    answer = await asyncio.to_thread(
        client.ask,
        question,
        session_id=session_id or "default",
        project=project or "memory-tracker-project",
        ticker=ticker,
        context_label=context_label,
        filters=filters,
        metadata_filters=metadata_filters,
        theme="ai",
    )
    answer = (answer or "").strip()
    return _qualify_es_markdown_links(answer, getattr(client, "base_url", ""))


@router.post("")
async def chat(req: ChatRequest):
    msgs = [m for m in req.messages if m.role in ("user", "assistant")][-MAX_HISTORY:]
    if not msgs or msgs[-1].role != "user":
        return {"error": "last message must be from user"}

    question = re.sub(r"\s+\Z", "", msgs[-1].content or "")
    if len(question)>12000:
        from fastapi import HTTPException
        raise HTTPException(422,'Question must be at most 12000 characters')
    if not question.strip():
        return {"error": "question is required"}

    filters = _and_filters(
        _ask_source_filters(get_tab_sources("ask_claude")),
        req.filters,
    )
    reply = await _ask_es(
        question,
        _chat_session_id(req, msgs),
        filters=filters,
        metadata_filters=req.metadata_filters,
    )

    # The answer endpoint returns Markdown citations but no structured source
    # metadata. Resolve the cited document IDs so the UI can show providers.
    try:
        from .overview import _overview_citation_sources
        source_index = await _overview_citation_sources(
            reply, client=None, filters=filters or {}
        )
        sources = list(source_index.values())
    except Exception as exc:
        logger.warning("Ask citation provider lookup failed: %s", exc)
        sources = []

    return {"reply": reply, "sources": sources, "charts": []}
