from __future__ import annotations

import asyncio
import logging
import os

from fastapi import APIRouter, UploadFile, File, HTTPException

from ..models import UploadResponse, JobStatus
from ...db import queries
from ...services import broker_from_filename
from ...services.es_rag_gateway import EsRagContractError, get_es_rag_gateway

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["upload"])

MAX_FILE_SIZE = int(os.getenv("MEMORY_PDF_UPLOAD_MAX_MB", "200") or "200") * 1024 * 1024
ES_JOB_PREFIX = "esrag:"


def _memory_status_from_es(job: dict) -> JobStatus:
    status = str(job.get("status") or "").lower()
    if status in {"pending", "queued"}:
        mapped = "queued"
    elif status == "running":
        mapped = "extracting"
    elif status in {"done", "skipped"}:
        mapped = "done"
    elif status in {"failed", "error"}:
        mapped = "error"
    else:
        mapped = status or "queued"
    processed = 1 if mapped == "done" else 0
    return JobStatus(
        id=f"{ES_JOB_PREFIX}{job.get('id') or ''}",
        filename=str(job.get("filename") or ""),
        status=mapped,
        total_charts=1,
        processed_charts=processed,
        error_message=(
            "ES RAG ingestion failed"
            if mapped == "error" and job.get("error")
            else None
        ),
    )


@router.post("/upload", response_model=list[UploadResponse])
async def upload_pdfs(files: list[UploadFile] = File(...), force: bool = False):
    """Upload one or more PDFs through ES RAG's canonical ingest queue.

    Memory does not run local PDF extraction/indexing. ES RAG owns Docling,
    OCR, table extraction, chart extraction, metadata, and corpus writes.
    """
    if not files:
        raise HTTPException(400, "No files provided")
    client = get_es_rag_gateway()

    responses = []
    for f in files:
        if not f.filename or not f.filename.lower().endswith(".pdf"):
            raise HTTPException(400, f"Only PDF files are accepted: {f.filename}")

        content = await f.read()
        if len(content) > MAX_FILE_SIZE:
            mb = MAX_FILE_SIZE // (1024 * 1024)
            raise HTTPException(400, f"File too large (max {mb}MB): {f.filename}")

        broker = broker_from_filename(f.filename) or ""
        result = await asyncio.to_thread(
            client.ingest_pdf,
            content,
            filename=f.filename,
            ticker="",
            broker=broker,
            report_type="broker_research",
            extract_charts=True,
            force=force,
            source="memory_chart_upload",
        )

        job_id = str(result.get("job_id") or "")
        if not job_id:
            raise EsRagContractError(
                "ES RAG ingest response has no job_id",
                operation="ingest_pdf",
            )

        responses.append(UploadResponse(
            job_id=f"{ES_JOB_PREFIX}{job_id}",
            filename=f.filename,
            message="ES RAG ingest queued" + (" (force re-index)" if force else ""),
        ))

    return responses


@router.get("/jobs/{job_id}", response_model=JobStatus)
async def get_job_status(job_id: str):
    if job_id.startswith(ES_JOB_PREFIX):
        client = get_es_rag_gateway()
        es_job_id = job_id[len(ES_JOB_PREFIX):]
        job = await asyncio.to_thread(client.get_job, es_job_id)
        return _memory_status_from_es(job)

    # Compatibility for old local Memory jobs created before PDF ingest was
    # delegated to ES RAG.
    job = await queries.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    return JobStatus(**job)
