from __future__ import annotations

import base64
import json
import os
from typing import Any


DEFAULT_MODEL = "gpt-5.6-luna"
DEFAULT_REASONING_EFFORT = "low"


def classifier_cache_key() -> str:
    return f"{DEFAULT_MODEL}:{DEFAULT_REASONING_EFFORT}"


async def classify_chart_json(
    prompt: str,
    *,
    schema_name: str,
    schema: dict[str, Any],
    image_bytes: bytes | None = None,
    media_type: str = "image/png",
    max_output_tokens: int = 1500,
) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    from openai import AsyncOpenAI

    content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
    if image_bytes is not None:
        encoded = base64.standard_b64encode(image_bytes).decode("ascii")
        content.append({
            "type": "input_image",
            "image_url": f"data:{media_type};base64,{encoded}",
            "detail": "high",
        })

    client = AsyncOpenAI(api_key=api_key, timeout=90.0, max_retries=1)
    response = await client.responses.create(
        model=DEFAULT_MODEL,
        reasoning={"effort": DEFAULT_REASONING_EFFORT},
        input=[{"role": "user", "content": content}],
        text={
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "strict": True,
                "schema": schema,
            }
        },
        max_output_tokens=max_output_tokens,
    )
    output_text = str(getattr(response, "output_text", "") or "").strip()
    if not output_text:
        raise RuntimeError("OpenAI chart categorizer returned no output text")
    result = json.loads(output_text)
    if not isinstance(result, dict):
        raise ValueError("OpenAI chart categorizer returned a non-object JSON value")
    return result
