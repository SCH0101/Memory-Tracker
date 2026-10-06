from __future__ import annotations

import os
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _local_base() -> Path:
    override = os.getenv("MEMORY_CHART_RUNTIME_DIR", "").strip()
    if override:
        return Path(override)
    local = os.getenv("LOCALAPPDATA", "").strip()
    if local:
        return Path(local) / "EquitySwarm" / "memory_chart"
    return PROJECT_ROOT / "data"


DATA_DIR = _local_base() / "data"
CHARTS_DIR = DATA_DIR / "charts"
UPLOADS_DIR = DATA_DIR / "uploads"
EXCEL_UPLOADS_DIR = DATA_DIR / "excel_uploads"
DB_PATH = DATA_DIR / "memory_chart.db"
UNIT_RULES_PATH = DATA_DIR / "unit_rules.json"
RAG_SETTINGS_PATH = DATA_DIR / "rag_settings.json"

LEGACY_DATA_DIR = PROJECT_ROOT / "data"
LEGACY_CHARTS_DIR = LEGACY_DATA_DIR / "charts"
LEGACY_UPLOADS_DIR = LEGACY_DATA_DIR / "uploads"
