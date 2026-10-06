from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from ..db.connection import get_db, close_db
from .routes.upload import router as upload_router
from .routes.charts import router as charts_router
from .routes.categories import router as categories_router
from .routes.paste import router as paste_router
from .routes.companies import router as companies_router
from .routes.overview import (
    router as overview_router,
    start_overview_auto_refresh,
    stop_overview_auto_refresh,
)
from .routes.dashboard import router as dashboard_router
from .routes.model import router as model_router
from .routes.chat_bridge import router as chat_router
from .routes.inject import router as inject_router
from .routes.corpus_stats import router as corpus_stats_router
from .routes.excel import router as excel_router
from .routes.settings import router as settings_router
from ..paths import DATA_DIR, CHARTS_DIR
from ..services.es_rag_gateway import EsRagError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

FRONTEND_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    await get_db()
    overview_refresh_task = start_overview_auto_refresh()
    logger.info("Memory Chart API started")
    try:
        yield
    finally:
        await stop_overview_auto_refresh(overview_refresh_task)
        await close_db()
        logger.info("Memory Chart API shut down")


app = FastAPI(title="Memory Chart API", version="1.0.0", lifespan=lifespan)


@app.exception_handler(EsRagError)
async def es_rag_error_handler(_request, exc: EsRagError):
    logger.warning(
        "ES RAG operation failed: operation=%s status=%s",
        exc.operation,
        exc.status_code,
    )
    return JSONResponse(
        status_code=503,
        content={
            "detail": "ES RAG service unavailable",
            "operation": exc.operation,
        },
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(upload_router)
app.include_router(charts_router)
app.include_router(categories_router)
app.include_router(paste_router)
app.include_router(companies_router)
app.include_router(overview_router)
app.include_router(dashboard_router)
app.include_router(model_router)
app.include_router(chat_router)
app.include_router(inject_router)
app.include_router(corpus_stats_router)
app.include_router(excel_router)
app.include_router(settings_router)

app.mount("/charts", StaticFiles(directory=str(CHARTS_DIR)), name="chart-images")


@app.get("/health")
async def health():
    return {"status": "ok", "service": "memory-chart"}


def _serve_index():
    index = FRONTEND_DIST / "index.html"
    if FRONTEND_DIST.is_dir() and index.exists():
        return FileResponse(str(index), headers={"Cache-Control": "no-store"})
    return HTMLResponse("<h1>Frontend not built</h1>", status_code=404)


@app.get("/", include_in_schema=False)
async def root():
    return _serve_index()


@app.get("/{spa_path:path}", include_in_schema=False)
async def spa_entry(spa_path: str):
    if FRONTEND_DIST.is_dir():
        asset_file = FRONTEND_DIST / spa_path
        if asset_file.exists() and asset_file.is_file():
            headers = (
                {"Cache-Control": "public, max-age=31536000, immutable"}
                if spa_path.startswith("assets/")
                else {"Cache-Control": "no-cache"}
            )
            return FileResponse(str(asset_file), headers=headers)
    if spa_path.startswith("assets/"):
        return PlainTextResponse(
            "Frontend asset not found",
            status_code=404,
            headers={"Cache-Control": "no-store"},
        )
    return _serve_index()
