from __future__ import annotations

import logging
import shutil
from pathlib import Path

from ..tools.pdf_chart_extractor import ExtractedChart, extract_charts_from_pdf
from ..paths import CHARTS_DIR

logger = logging.getLogger(__name__)

DATA_DIR = CHARTS_DIR


async def extract_charts(pdf_path: Path) -> list[ExtractedChart]:
    safe_stem = pdf_path.stem[:80]
    output_dir = DATA_DIR / safe_stem
    output_dir.mkdir(parents=True, exist_ok=True)

    charts = extract_charts_from_pdf(
        pdf_path, output_dir,
        dpi=200,
        margin_pts=10.0,
        upper_extra_pixels=24,
        cluster_gap_pts=28.0,
        min_paths=10,
        min_width_pts=65.0,
        min_height_pts=50.0,
        jpg_quality=90,
    )
    logger.info("Extracted %d charts from %s", len(charts), pdf_path.name)
    return charts


def get_relative_image_path(chart: ExtractedChart) -> str:
    try:
        return str(chart.path.relative_to(DATA_DIR))
    except ValueError:
        return str(chart.path)
