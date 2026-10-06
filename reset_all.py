"""Full reset of memory_chart derived data ahead of fresh PDF re-uploads.

Wipes:
  - SQLite: charts, chart_companies, source_pdfs, processing_jobs,
            datapoints, overview_cache   (categories/subcategories KEPT)
  - Files:  data/uploads/*.pdf, data/charts/* images
  - Qdrant: all points with ticker=MEMORY (memory_chart's own documents only)
  - Fact JSON mirrors under equity_swarm_V5/fact_pool/memory_chart/

Run: python memory_chart/reset_all.py
"""
import asyncio, os, shutil, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "equity_swarm_V5"))
os.environ.setdefault("VECTOR_BACKEND", "qdrant")

MC = ROOT / "memory_chart"

from memory_chart.backend.db.connection import get_db, close_db


async def wipe_sqlite():
    db = await get_db()
    for table in ("chart_companies", "charts", "source_pdfs", "processing_jobs",
                  "datapoints", "overview_cache"):
        try:
            cur = await db.execute(f"DELETE FROM {table}")
            print(f"  sqlite {table}: {cur.rowcount} rows deleted")
        except Exception as exc:
            print(f"  sqlite {table}: skipped ({exc})")
    await db.commit()
    await close_db()


def wipe_files():
    uploads = MC / "data" / "uploads"
    n = 0
    for p in uploads.glob("*.pdf"):
        p.unlink()
        n += 1
    print(f"  uploads: {n} PDFs deleted")
    charts_dir = MC / "data" / "charts"
    n = 0
    if charts_dir.is_dir():
        for child in charts_dir.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
            n += 1
    print(f"  chart images: {n} entries deleted")
    fact_pool = ROOT / "equity_swarm_V5" / "fact_pool" / "memory_chart"
    n = 0
    if fact_pool.is_dir():
        for p in fact_pool.glob("*.json"):
            p.unlink()
            n += 1
    print(f"  fact JSON mirrors: {n} deleted")


def wipe_qdrant():
    from qdrant_client import QdrantClient
    from qdrant_client import models as qm
    # Resolve the LIVE collection name from the swarm so this script stays
    # correct across embedding migrations (Qwen now; whatever's next later).
    # NEVER hardcode "equity_research_qdrant" — that's the dead MiniLM legacy.
    import sys
    sys.path.insert(0, str(ROOT / "equity_swarm_V5"))
    from tools.qdrant_store import COLLECTION_NAME as coll  # type: ignore
    client = QdrantClient(url="http://localhost:6333", timeout=60)
    flt = qm.Filter(must=[qm.FieldCondition(key="ticker", match=qm.MatchValue(value="MEMORY"))])
    before = client.count(coll, count_filter=flt).count
    client.delete(coll, points_selector=flt)
    after = client.count(coll, count_filter=flt).count
    print(f"  qdrant ({coll}) MEMORY points: {before} -> {after}")


print("Wiping SQLite tables...")
asyncio.run(wipe_sqlite())
print("Wiping files...")
wipe_files()
print("Wiping Qdrant MEMORY docs...")
wipe_qdrant()
print("DONE — re-upload PDFs to rebuild everything with the new workflow.")
