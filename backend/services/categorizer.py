from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

from .openai_chart_classifier import classify_chart_json

logger = logging.getLogger(__name__)

_CAT_BATCH_SIZE = 12
_CAT_CONCURRENCY = 2

_BATCH_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "category": {"type": "string"},
                    "subcategory": {"type": "string"},
                    "new_subcategory": {"type": ["string", "null"]},
                    "companies": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "index", "category", "subcategory", "new_subcategory", "companies"
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


@dataclass
class CatResult:
    category: str = "General Memory"
    subcategory: str = "Overall memory market"
    is_new_subcategory: bool = False
    companies: list[str] | None = None


def _build_categorize_prompt(category_tree: list[dict]) -> str:
    tree_lines = []
    for cat in category_tree:
        subs = [s["name"] for s in cat.get("subcategories", [])]
        tree_lines.append(f"  {cat['name']}: {', '.join(subs)}")
    tree_str = "\n".join(tree_lines)

    return f"""You are categorizing semiconductor memory and AI infrastructure charts into sections.

EXISTING CATEGORY TREE:
{tree_str}

Additional allowed categories for AI-theme charts:
- "AI Infrastructure Buildout" for AI datacenter buildout, GW/power provisioning, AI server/rack deployment, cloud/hyperscaler capex, and AI infrastructure spending.
- "Hyperscaler Economics" for hyperscaler returns/economics such as ROIC, return on invested capital, FCF/free cash flow, capex intensity, and capital intensity.

STEP 1 — Determine the CATEGORY from the chart tag/caption:
- If the tag/caption mentions "DDR", "DDR4", "DDR5", "DDR bit", "LPDDR" → "DDR DRAM" (NOT "DRAM")
- If the tag/caption mentions "HBM" → "HBM" (NOT "DRAM")
- If the tag/caption mentions BOTH "DRAM" and "NAND" → pick the PRIMARY subject. If DRAM is the main focus (e.g. "DRAM shortfall", "DRAM wafer"), use "DRAM". If NAND is the main focus, use "NAND". If truly equal (e.g. "DRAM/NAND pricing trends"), use "General Memory".
- If the tag/caption mentions "DRAM" (bit growth, pricing, TAM, supply/demand, market share, capex, inventory, shipments, revenue, wafer) → "DRAM"
- If the tag/caption mentions "NAND", "flash", "SSD", "3D NAND" → "NAND"
- If NONE of the above keywords appear → "General Memory"
- Only use "General Memory" when the chart truly spans ALL memory types equally or has NO specific memory type keyword

STEP 2 — Determine the SUBCATEGORY:
- First check if an existing subcategory from the tree above fits. If yes, use the EXACT name.
- If no existing subcategory fits, CREATE a new one with a clear descriptive name (e.g. "DRAM revenue", "HBM capacity", "NAND pricing trends", "Datacenter GW build", "Hyperscaler capex", "ROIC / returns", "Free cash flow"). Set new_subcategory to this name.
- Subcategory names should be short and descriptive (2-4 words).

IMPORTANT: Always auto-create a new subcategory rather than forcing a chart into a wrong one. The system will auto-create it in the database.

STEP 3 — Determine which MEMORY COMPANIES this chart is about:
- ONLY detect semiconductor memory MANUFACTURERS: Samsung, SK Hynix, Micron, Kioxia, Western Digital, Nanya, Winbond, Intel, YMTC, CXMT
- Variants: "SEC" = "Samsung", "SK hynix" = "SK Hynix", "MU" = "Micron", "WDC" = "Western Digital", "Hynix" = "SK Hynix", "Samsung Electronics" = "Samsung"
- Do NOT include non-memory companies (brokers, data providers, PC/phone OEMs, semiconductor equipment makers, foundries, etc.)
- The chart must be ABOUT that company's memory business (e.g. "Samsung DRAM capex", "SK Hynix HBM market share")
- If a chart just mentions a company as a data point in a broader industry chart, do NOT include it
- If no memory manufacturer is the subject, return ["General"]. For AI infrastructure/hyperscaler charts, only return a memory manufacturer if the chart is directly about that memory company; otherwise return ["General"].

For each chart (given its tag and caption), return:
{{"results": [{{"index": N, "category": "<category>", "subcategory": "<subcategory>", "new_subcategory": null, "companies": ["Samsung", "SK Hynix"]}}]}}

If a new subcategory is needed:
{{"index": N, "category": "<category>", "subcategory": "<new name>", "new_subcategory": "<new name>", "companies": ["..."]}}

Return STRICT JSON, no markdown fence."""


async def _cat_batch(batch: list[dict], offset: int,
                     category_tree: list[dict]) -> list[CatResult]:
    chart_descs = []
    for j, item in enumerate(batch):
        gi = offset + j
        chart_descs.append(f"[CHART {gi}] tag: {item['tag']}, caption: {item['caption']}")

    prompt = _build_categorize_prompt(category_tree)
    user_msg = prompt + "\n\nCharts to categorize:\n" + "\n".join(chart_descs)

    try:
        payload = await classify_chart_json(
            user_msg,
            schema_name="memory_chart_categories",
            schema=_BATCH_RESULT_SCHEMA,
            max_output_tokens=1500,
        )
    except Exception as exc:
        logger.warning("Categorize batch (offset %d) failed: %s", offset, exc)
        return [CatResult() for _ in batch]

    results_map: dict[int, CatResult] = {}
    for row in payload.get("results", []):
        try:
            idx = int(row["index"])
        except (KeyError, TypeError, ValueError):
            continue
        new_sub = row.get("new_subcategory")
        companies = row.get("companies", ["General"])
        if not companies:
            companies = ["General"]
        results_map[idx] = CatResult(
            category=row.get("category", "General Memory"),
            subcategory=row.get("subcategory", "Overall memory market"),
            is_new_subcategory=bool(new_sub),
            companies=companies,
        )

    return [results_map.get(offset + j, CatResult()) for j in range(len(batch))]


async def categorize_batch(
    chart_infos: list[dict],
    category_tree: list[dict],
) -> list[CatResult]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        logger.warning("No OPENAI_API_KEY; defaulting all to General Memory")
        return [CatResult() for _ in chart_infos]

    sem = asyncio.Semaphore(_CAT_CONCURRENCY)

    async def _run(offset: int) -> list[CatResult]:
        async with sem:
            batch = chart_infos[offset:offset + _CAT_BATCH_SIZE]
            return await _cat_batch(batch, offset, category_tree)

    offsets = list(range(0, len(chart_infos), _CAT_BATCH_SIZE))
    batch_results = await asyncio.gather(*(_run(o) for o in offsets),
                                          return_exceptions=True)

    all_results: list[CatResult] = []
    for r in batch_results:
        if isinstance(r, list):
            all_results.extend(r)
        elif isinstance(r, Exception):
            logger.warning("Categorize batch exception: %s", r)
            all_results.extend([CatResult() for _ in range(_CAT_BATCH_SIZE)])

    return all_results[:len(chart_infos)]
