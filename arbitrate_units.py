"""Run the LLM unit arbiter over ALL datapoints.

Reviews every value >=2x off its same-period peers: keep / convert (and learn
the conversion rule) / delete. Safe to re-run — verdicts are remembered.

Run: python memory_chart/arbitrate_units.py
"""
import asyncio, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent / "backend" / ".env")

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

from memory_chart.backend.db.connection import close_db
from memory_chart.backend.services.unit_arbiter import arbitrate_metric_outliers


async def main():
    stats = await arbitrate_metric_outliers(None)  # all metrics
    print(f"\nARBITRATION DONE: {stats}")
    await close_db()

asyncio.run(main())
