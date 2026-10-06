"""Upload + ingest Excel models into the datapoints table.

POST /api/upload-excel
  multipart form:
    file:    .xlsx (required)
    broker:  override broker (optional — falls back to filename heuristic)
    force:   "true" to re-ingest even if SHA256 matches a prior upload

GET /api/excel-ingests
  Returns the audit list (last 50 ingests) with row counts.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from ...paths import EXCEL_UPLOADS_DIR
from ...services import broker_from_filename, known_brokers, normalize_broker
from ...services.excel_ingest import ingest_excel, list_ingests
from ...services.fact_pool import index_excel_model

logger = logging.getLogger(__name__)
router = APIRouter(tags=["excel"])

# Where uploaded workbooks land. Folder watcher can ALSO drop files here.
EXCEL_UPLOAD_DIR = EXCEL_UPLOADS_DIR


@router.get("/api/excel/resolve-broker")
async def resolve_excel_broker(filename: str):
    broker = broker_from_filename(filename)
    return {
        "filename": filename,
        "broker": broker,
        "needs_broker": not bool(broker),
        "known_brokers": known_brokers(),
    }


@router.post("/api/upload-excel")
async def upload_excel(
    file: UploadFile = File(...),
    broker: str | None = Form(default=None),
    force: bool = Form(default=False),
):
    name = file.filename or "uploaded.xlsx"
    if not name.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "Only .xlsx / .xlsm files accepted")
    resolved_broker = broker_from_filename(name, explicit_broker=broker or "")
    if not resolved_broker:
        return {
            "ok": False,
            "status": "needs_broker",
            "needs_broker": True,
            "filename": name,
            "known_brokers": known_brokers(),
            "message": "Broker name required before ingesting this Excel file.",
        }
    EXCEL_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = EXCEL_UPLOAD_DIR / name
    with open(dest, "wb") as out:
        shutil.copyfileobj(file.file, out)
    resolved_broker = normalize_broker(resolved_broker)
    logger.info("Excel uploaded: %s (broker=%s, force=%s)", name, resolved_broker, force)
    provider = os.getenv("MEMORY_EXCEL_RAG_PROVIDER", "Memory Tracker Project").strip() or "Memory Tracker Project"
    es_broker = os.getenv("MEMORY_EXCEL_RAG_BROKER", provider).strip() or provider
    es_rag_index = await asyncio.to_thread(
        index_excel_model,
        dest,
        provider=provider,
        broker=es_broker,
        force=True,
    )
    result = await ingest_excel(dest, broker=resolved_broker, force=force)
    result["es_rag_index"] = es_rag_index
    return result


@router.get("/api/excel-ingests")
async def get_ingests():
    return {"ingests": await list_ingests()}
