from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import normalize_broker

logger = logging.getLogger(__name__)

_HAIKU_MODEL = os.getenv("OPENAI_EXTRACTION_MODEL", "gpt-5.6-luna")
_TAG_BATCH_SIZE = 6
_TAG_CONCURRENCY = 4


@dataclass
class TagResult:
    topic: str = ""
    chart_type: str = ""
    broker: str = ""
    date: str = ""
    date_int: int = 0
    tag: str = ""
    caption: str = ""
    estimates: str = ""


_TAG_PROMPT_TEMPLATE = """You are analyzing charts from semiconductor memory industry research reports (DRAM, NAND, HBM).
{pdf_context}
For each chart image labelled [CHART n], extract:
1. "topic": What the chart shows (e.g. "HBM TAM forecast", "DRAM bit growth by application", "NAND pricing trend")
2. "chart_type": bar, line, stacked_bar, pie, area, waterfall, scatter, other
3. "broker": The research firm / investment bank that produced this chart. You MUST try hard to identify it. Check ALL of:
   - The PDF filename and front page text above (HIGHEST priority — the broker and date are almost always on the cover page)
   - Logos (top corners, bottom corners, watermarks)
   - Header/footer text ("Goldman Sachs", "J.P. Morgan", "Source: UBS estimates")
   - Source citations at chart bottom ("Source: Gartner, UBS Research")
   - Disclaimer text, copyright notices
   Common brokers: UBS, Goldman Sachs, JPM, Morgan Stanley, BofA, Citi, Barclays, Deutsche Bank, CLSA, Nomura, Samsung Securities, TrendForce, Yole, HSBC, Gartner, Bernstein, Jefferies, Wells Fargo, KeyBanc, Mizuho, Daiwa, Macquarie
   Use the SHORT name (e.g. "UBS" not "UBS AG", "GS" or "Goldman Sachs").
   If source says "Source: Gartner, UBS estimates" → broker is "UBS" (the bank, not data provider).
   NEVER return "Unknown" — if truly undetectable, return "".
4. "date": Publication date in YYYY-MM-DD.
   PRIORITY ORDER for finding the date:
   a) PDF FRONT PAGE TEXT above — look for publication date, report date, "Month DD, YYYY", "DD Month YYYY", date headers. This is the MOST RELIABLE source.
   b) PDF filename — often contains a date like "20260601" or "June_2026"
   c) Chart image itself — date stamps, headers, copyright, "Source: Firm, Month YYYY"
   d) If only year visible, use YYYY-01-01
   e) LAST RESORT: default to "{today}"
   ALL charts from the same PDF should have the SAME publication date (from the PDF cover page).
5. "tag": Concise label: "<topic> - <broker> <YYYYMMDD>". E.g. "HBM TAM - UBS 20260501". Always include broker if detected.
6. "caption": 1-2 sentence description of the chart's content and key takeaway.
7. "estimates": Extract KEY FORWARD-LOOKING estimates/forecasts visible in the chart or mentioned in the PDF PAGE TEXT below.
   RULES:
   - ONLY include estimates for year {current_year} and BEYOND (e.g. {current_year}e, {next_year}e, {next_year2}e). EXCLUDE all prior years.
   - ONLY extract ESTIMATES (years with "e", "E", "F", forecast) — NOT historical/actual values
   - Format: concise key=value pairs like "{current_year}e: $25B, {next_year}e: $38B" or "bit growth {current_year}e: +19% YoY, {next_year}e: +22%"
   - If the chart shows a specific metric with clear forecast numbers, extract the latest 2-3 forecast data points
   - Check BOTH the chart image AND the PDF PAGE TEXT (if provided) for numbers related to this chart
   - Priority: PDF text numbers > chart axis/label numbers (text is more precise)
   - If you cannot read specific numbers with HIGH CONFIDENCE, return "" (empty string)
   - Do NOT guess or approximate — only label when numbers are clearly readable
   - Keep it under 80 characters

Return STRICT JSON, no markdown fence:
{{"charts": [{{"index": 0, "topic": "...", "chart_type": "...", "broker": "...", "date": "YYYY-MM-DD", "tag": "...", "caption": "...", "estimates": "..."}}]}}"""


def _encode_image(path: Path) -> tuple[str, str] | None:
    try:
        data = path.read_bytes()
    except Exception:
        return None
    if not data:
        return None
    media = "image/png" if str(path).lower().endswith(".png") else "image/jpeg"
    return media, base64.standard_b64encode(data).decode("ascii")


def _parse_date_int(date_str: str) -> int:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", date_str)
    if m:
        return int(m.group(1)) * 10000 + int(m.group(2)) * 100 + int(m.group(3))
    m2 = re.match(r"(\d{8})", date_str)
    if m2:
        return int(m2.group(1))
    return 0


async def _tag_batch(
    client, batch: list[Path], offset: int,
    pdf_filename: str = "", pdf_front_text: str = "",
    page_texts: dict[int, str] | None = None,
    chart_pages: list[int] | None = None,
) -> list[TagResult]:
    content: list[dict] = []
    for j, path in enumerate(batch):
        gi = offset + j
        page_text_block = ""
        if page_texts and chart_pages and j < len(chart_pages):
            pg = chart_pages[j]
            nearby = []
            for p in range(max(1, pg - 1), pg + 2):
                if p in page_texts and page_texts[p].strip():
                    nearby.append(page_texts[p])
            if nearby:
                page_text_block = "\n".join(nearby)[:2000]
        label = f"[CHART {gi}]"
        if page_text_block:
            label += f"\nPDF PAGE TEXT near this chart (use for estimates extraction):\n---\n{page_text_block}\n---"
        content.append({"type": "text", "text": label})
        enc = _encode_image(path)
        if enc:
            media, b64 = enc
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": media, "data": b64}})

    from datetime import date as _date
    today_str = _date.today().isoformat()
    pdf_context = ""
    if pdf_filename:
        pdf_context = f"\nPDF FILENAME: \"{pdf_filename}\" — the broker name and date are often in the filename.\n"
    if pdf_front_text:
        pdf_context += f"\nPDF FRONT PAGES TEXT (first 2 pages — use this to find the publication date and broker):\n---\n{pdf_front_text}\n---\n"
    cur_year = _date.today().year
    prompt_text = _TAG_PROMPT_TEMPLATE.format(
        pdf_context=pdf_context, today=today_str,
        current_year=cur_year, next_year=cur_year + 1, next_year2=cur_year + 2,
    )
    content.append({"type": "text", "text": prompt_text})

    try:
        resp = await client.messages.create(
            model=_HAIKU_MODEL, max_tokens=2000, temperature=0,
            messages=[{"role": "user", "content": content}],
        )
    except Exception as exc:
        logger.warning("Tag batch (offset %d) failed: %s", offset, exc)
        return [TagResult() for _ in batch]

    text = "".join(getattr(b, "text", "") for b in resp.content).strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return [TagResult() for _ in batch]
        try:
            payload = json.loads(m.group(0))
        except json.JSONDecodeError:
            return [TagResult() for _ in batch]

    results_map: dict[int, TagResult] = {}
    for row in payload.get("charts", []):
        try:
            idx = int(row["index"])
        except (KeyError, TypeError, ValueError):
            continue
        date_str = row.get("date", "") or ""
        results_map[idx] = TagResult(
            topic=row.get("topic", ""),
            chart_type=row.get("chart_type", ""),
            broker=normalize_broker(row.get("broker", "")),
            date=date_str,
            date_int=_parse_date_int(date_str),
            tag=row.get("tag", ""),
            caption=row.get("caption", ""),
            estimates=row.get("estimates", ""),
        )

    return [results_map.get(offset + j, TagResult()) for j in range(len(batch))]


async def tag_batch(
    chart_paths: list[Path],
    pdf_filename: str = "",
    pdf_front_text: str = "",
    page_texts: dict[int, str] | None = None,
    chart_page_numbers: list[int] | None = None,
) -> list[TagResult]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        logger.warning("No OPENAI_API_KEY — skipping tagging")
        return [TagResult() for _ in chart_paths]

    from .openai_research import AsyncResearchClient
    client = AsyncResearchClient(api_key=api_key)
    sem = asyncio.Semaphore(_TAG_CONCURRENCY)

    async def _run(offset: int) -> list[TagResult]:
        async with sem:
            batch = chart_paths[offset:offset + _TAG_BATCH_SIZE]
            batch_pages = chart_page_numbers[offset:offset + _TAG_BATCH_SIZE] if chart_page_numbers else None
            return await _tag_batch(
                client, batch, offset,
                pdf_filename=pdf_filename, pdf_front_text=pdf_front_text,
                page_texts=page_texts, chart_pages=batch_pages,
            )

    offsets = list(range(0, len(chart_paths), _TAG_BATCH_SIZE))
    batch_results = await asyncio.gather(*(_run(o) for o in offsets),
                                          return_exceptions=True)

    all_results: list[TagResult] = []
    for r in batch_results:
        if isinstance(r, list):
            all_results.extend(r)
        elif isinstance(r, Exception):
            logger.warning("Tag batch exception: %s", r)
            all_results.extend([TagResult() for _ in range(_TAG_BATCH_SIZE)])

    return all_results[:len(chart_paths)]


_ESTIMATES_PROMPT = """You are extracting FORWARD-LOOKING ESTIMATES from semiconductor memory industry charts.
{page_context}
CURRENT YEAR: {current_year}

For each chart image labelled [CHART n], with its tag and caption provided, extract KEY ESTIMATES:

RULES:
- ONLY include estimates for year {current_year} and BEYOND (e.g. {current_year}e, {next_year}e, {next_year2}e). EXCLUDE all prior years.
- ONLY extract ESTIMATES / FORECASTS (years marked "e", "E", "F", or clearly future projections) — NOT historical values
- Format: concise key=value pairs like "{current_year}e: $25B, {next_year}e: $38B" or "bit growth {current_year}e: +19% YoY"
- Extract the 2-3 most important forecast data points visible
- Check BOTH the chart image AND the PDF PAGE TEXT (if provided) for estimate numbers
- Priority: PDF text numbers > chart axis/label numbers (text is more precise)
- If you CANNOT read specific numbers with HIGH CONFIDENCE, return "" (empty string)
- Do NOT guess or approximate — only label when numbers are CLEARLY readable
- Keep each estimate under 80 characters
- For pricing charts: extract forward price levels or % changes
- For volume/capacity charts: extract forward quantities or growth rates
- For market share: extract projected shares
- For capex: extract forward spending figures

Return STRICT JSON array, no markdown fence:
[{{"index": 0, "estimates": "..."}}]"""


async def _extract_estimates_batch(
    client, charts_info: list[dict], offset: int,
    page_texts: dict[int, str] | None = None,
) -> list[str]:
    content: list[dict] = []
    for j, info in enumerate(charts_info):
        gi = offset + j
        page_text_block = ""
        if page_texts and info.get("page"):
            pg = info["page"]
            nearby = []
            for p in range(max(1, pg - 1), pg + 2):
                if p in page_texts and page_texts[p].strip():
                    nearby.append(page_texts[p])
            if nearby:
                page_text_block = "\n".join(nearby)[:2000]

        label = f'[CHART {gi}] tag: "{info.get("tag", "")}" caption: "{info.get("caption", "")}"'
        if page_text_block:
            label += f"\nPDF PAGE TEXT near this chart:\n---\n{page_text_block}\n---"
        content.append({"type": "text", "text": label})

        enc = _encode_image(Path(info["path"]))
        if enc:
            media, b64 = enc
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": media, "data": b64}})

    page_context = ""
    if page_texts:
        page_context = "PDF page text is provided near each chart for context.\n"
    from datetime import date as _date
    cur_year = _date.today().year
    prompt_text = _ESTIMATES_PROMPT.format(
        page_context=page_context,
        current_year=cur_year, next_year=cur_year + 1, next_year2=cur_year + 2,
    )
    content.append({"type": "text", "text": prompt_text})

    try:
        resp = await client.messages.create(
            model=_HAIKU_MODEL, max_tokens=1500, temperature=0,
            messages=[{"role": "user", "content": content}],
        )
    except Exception as exc:
        logger.warning("Estimates batch (offset %d) failed: %s", offset, exc)
        return ["" for _ in charts_info]

    text = "".join(getattr(b, "text", "") for b in resp.content).strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if not m:
            return ["" for _ in charts_info]
        try:
            payload = json.loads(m.group(0))
        except json.JSONDecodeError:
            return ["" for _ in charts_info]

    results_map: dict[int, str] = {}
    for row in payload:
        try:
            idx = int(row["index"])
        except (KeyError, TypeError, ValueError):
            continue
        results_map[idx] = row.get("estimates", "") or ""

    return [results_map.get(offset + j, "") for j in range(len(charts_info))]


async def extract_estimates_batch(
    charts_info: list[dict],
    page_texts: dict[int, str] | None = None,
) -> list[str]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        logger.warning("No OPENAI_API_KEY — skipping estimates")
        return ["" for _ in charts_info]

    from .openai_research import AsyncResearchClient
    client = AsyncResearchClient(api_key=api_key)
    sem = asyncio.Semaphore(_TAG_CONCURRENCY)

    async def _run(offset: int) -> list[str]:
        async with sem:
            batch = charts_info[offset:offset + _TAG_BATCH_SIZE]
            return await _extract_estimates_batch(
                client, batch, offset, page_texts=page_texts,
            )

    offsets = list(range(0, len(charts_info), _TAG_BATCH_SIZE))
    batch_results = await asyncio.gather(*(_run(o) for o in offsets),
                                          return_exceptions=True)

    all_results: list[str] = []
    for r in batch_results:
        if isinstance(r, list):
            all_results.extend(r)
        elif isinstance(r, Exception):
            logger.warning("Estimates batch exception: %s", r)
            all_results.extend(["" for _ in range(_TAG_BATCH_SIZE)])

    return all_results[:len(charts_info)]
