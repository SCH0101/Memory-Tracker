"""Memory Chart — entry point. Run from the memory_chart/ directory:
    python start.py
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / "backend" / ".env")
from backend.services.openai_research import openai_settings
import os
_settings = openai_settings()
os.environ["OPENAI_API_KEY"] = _settings["api_key"]
os.environ["OPENAI_BASE_URL"] = _settings["base_url"]

import uvicorn

if __name__ == "__main__":
    port = int(os.getenv("SERVER_PORT", "8088"))
    reload_enabled = os.getenv("MEMORY_CHART_RELOAD", "0").lower() in {"1", "true", "yes"}
    print(f"Starting Memory Chart API on http://localhost:{port}")
    uvicorn.run(
        "backend.api.server:app",
        host="0.0.0.0",
        port=port,
        reload=reload_enabled,
        reload_dirs=[str(ROOT / "backend")] if reload_enabled else None,
    )
