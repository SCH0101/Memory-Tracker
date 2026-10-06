import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

import uvicorn

if __name__ == "__main__":
    port = int(os.getenv("SERVER_PORT", "8088"))
    reload_enabled = os.getenv("MEMORY_CHART_RELOAD", "0").lower() in {"1", "true", "yes"}
    uvicorn.run(
        "backend.api.server:app",
        host="0.0.0.0",
        port=port,
        reload=reload_enabled,
        reload_dirs=[str(Path(__file__).resolve().parent)] if reload_enabled else None,
    )
