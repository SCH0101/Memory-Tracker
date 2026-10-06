"""Pure-RAG PDF injection endpoint that bypasses the gallery pipeline.

The PDF is sent to the authenticated external ES RAG ingestion API, then the
route polls the returned job until it reaches a terminal state.

Use this when you want a PDF in the shared fact pool WITHOUT the gallery
side-effects (no chart QC / recrop / categorization, no local SQLite rows).
For uploads that should also populate the gallery, use POST /api/upload.

Examples (curl):
    curl -F 'file=@/path/to/report.pdf' http://localhost:80/api/inject
    curl -F 'file=@x.pdf' -F 'ticker=MU' -F 'force=true' http://localhost:80/api/inject
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, UploadFile, File, Form, HTTPException

from ...services.es_rag_gateway import get_es_rag_gateway
from ...services.fact_pool import _bump_last_index_ts

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/inject", tags=["inject"])

MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 MB


def _inject_pdf_sync(pdf_bytes: bytes, filename: str, ticker: str,
                     broker: str, date: str, force: bool) -> dict:
    """Submit a PDF and synchronously wait for the external ingestion job."""
    client = get_es_rag_gateway()
    job = client.ingest_pdf(
        pdf_bytes,
        filename=filename,
        ticker=ticker or "MEMORY",
        broker=broker or "",
        date=date or "",
        report_type="broker_research",
        extract_charts=True,
        force=bool(force),
        source="memory_chart.inject",
    )
    result = client.wait_for_job(job["job_id"], timeout_s=900, poll_s=2.0)
    if result.get("status") == "done":
        _bump_last_index_ts()
        return result.get("result") or result
    return result


@router.post("")
async def inject_pdf_endpoint(
    file: UploadFile = File(...),
    ticker: str = Form("MEMORY"),
    broker: str = Form(""),
    date: str = Form(""),
    force: bool = Form(False),
):
    """Inject one PDF into ES RAG through the authenticated HTTP API.

    Synchronous (blocks until Docling finishes — 30s-3min). For non-blocking
    ingestion with progress polling + gallery integration, use POST /api/upload.

    Form params:
      - file (required): PDF file
      - ticker (default 'MEMORY'): canonical BBG-style code; use a real ticker
        (e.g. 'MU') to fire the ticker→sector boost; 'MEMORY' for industry docs
      - broker, date: override ES filename/cover-page auto-detection
      - force (default false): bypass is_document_indexed dedup; replaces
        existing chunks for this filename

    Returns the completed ES ingestion result:
      {ok, kind: "pdf", filename, ticker, broker, date,
       text_chunks, charts_indexed, skipped, reason}
    """
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, f"Only PDFs accepted (got {file.filename!r})")
    content = await file.read()
    if not content:
        raise HTTPException(400, "Empty file")
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            400, f"File too large (max {MAX_FILE_SIZE // 1024 // 1024} MB)")

    logger.info("ESRAG inject: %s (%d KB, ticker=%s, force=%s)",
                file.filename, len(content) // 1024, ticker, force)
    result = await asyncio.to_thread(
        _inject_pdf_sync, content, file.filename, ticker, broker, date, force)
    return result
