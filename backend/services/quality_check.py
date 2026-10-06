from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_HAIKU_MODEL = os.getenv("OPENAI_EXTRACTION_MODEL", "gpt-5.6-luna")
_QC_BATCH_SIZE = 6
_QC_CONCURRENCY = 4


@dataclass
class QCResult:
    status: str = "approved"  # approved | needs_recrop | rejected
    reason: str = ""
    chart_count: int = 1
    sub_positions: list[str] = field(default_factory=list)


_QC_PROMPT = """You are a chart quality inspector for semiconductor memory research reports.

For each chart image labelled [CHART n], determine:
1. Does this image contain exactly ONE clear chart/graph? → status: "approved"
2. Does it contain MULTIPLE distinct charts merged together? → status: "needs_recrop", describe where each chart is (top_half/bottom_half/left_half/right_half/top_third/middle_third/bottom_third)
3. Is it too unclear, cut off, mostly text, or a table/logo/disclaimer? → status: "rejected"

Return STRICT JSON, no markdown fence:
{"results": [{"index": <CHART n number>, "status": "approved"|"needs_recrop"|"rejected", "reason": "brief explanation", "chart_count": N, "positions": ["top_half", "bottom_half"]}]}
positions only needed for needs_recrop status."""


def _encode_image(path: Path) -> tuple[str, str] | None:
    try:
        data = path.read_bytes()
    except Exception:
        return None
    if not data:
        return None
    media = "image/png" if str(path).lower().endswith(".png") else "image/jpeg"
    return media, base64.standard_b64encode(data).decode("ascii")


async def _qc_batch(client, batch: list[Path], offset: int) -> list[QCResult]:
    content: list[dict] = []
    for j, path in enumerate(batch):
        gi = offset + j
        content.append({"type": "text", "text": f"[CHART {gi}]"})
        enc = _encode_image(path)
        if enc:
            media, b64 = enc
            content.append({"type": "image", "source": {
                "type": "base64", "media_type": media, "data": b64}})

    content.append({"type": "text", "text": _QC_PROMPT})

    try:
        resp = await client.messages.create(
            model=_HAIKU_MODEL, max_tokens=1500, temperature=0,
            messages=[{"role": "user", "content": content}],
        )
    except Exception as exc:
        logger.warning("QC batch (offset %d) failed: %s", offset, exc)
        return [QCResult() for _ in batch]

    text = "".join(getattr(b, "text", "") for b in resp.content).strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text).strip()

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return [QCResult() for _ in batch]
        try:
            payload = json.loads(m.group(0))
        except json.JSONDecodeError:
            return [QCResult() for _ in batch]

    results_map: dict[int, QCResult] = {}
    for row in payload.get("results", []):
        try:
            idx = int(row["index"])
        except (KeyError, TypeError, ValueError):
            continue
        results_map[idx] = QCResult(
            status=row.get("status", "approved"),
            reason=row.get("reason", ""),
            chart_count=row.get("chart_count", 1),
            sub_positions=row.get("positions", []),
        )

    return [results_map.get(offset + j, QCResult()) for j in range(len(batch))]


async def quality_check_batch(chart_paths: list[Path]) -> list[QCResult]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        logger.warning("No OPENAI_API_KEY — skipping QC, approving all")
        return [QCResult() for _ in chart_paths]

    from .openai_research import AsyncResearchClient
    client = AsyncResearchClient(api_key=api_key)
    sem = asyncio.Semaphore(_QC_CONCURRENCY)

    async def _run(offset: int) -> list[QCResult]:
        async with sem:
            batch = chart_paths[offset:offset + _QC_BATCH_SIZE]
            return await _qc_batch(client, batch, offset)

    offsets = list(range(0, len(chart_paths), _QC_BATCH_SIZE))
    batch_results = await asyncio.gather(*(_run(o) for o in offsets),
                                          return_exceptions=True)

    all_results: list[QCResult] = []
    for r in batch_results:
        if isinstance(r, list):
            all_results.extend(r)
        elif isinstance(r, Exception):
            logger.warning("QC batch exception: %s", r)
            all_results.extend([QCResult() for _ in range(_QC_BATCH_SIZE)])

    return all_results[:len(chart_paths)]
