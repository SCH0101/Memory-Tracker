"""OpenAI transport for research extraction, vision, and web discovery.

The small message result keeps existing domain parsers independent of the SDK.
Credentials are read from ES's local configuration, never copied into source.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from types import SimpleNamespace

from dotenv import dotenv_values
from openai import AsyncOpenAI, OpenAI


def openai_settings() -> dict[str, str]:
    path = Path(os.getenv("ES_CONFIG_ENV", str(Path.home() / "ES" / "app" / ".env")))
    config = dotenv_values(path) if path.is_file() else {}
    return {
        "api_key": str(config.get("OPENAI_API_KEY") or os.getenv("OPENAI_API_KEY") or "").strip(),
        "base_url": str(config.get("OPENAI_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/"),
    }


def request_payload(*, model, messages, max_tokens=4096, system=None,
                    temperature=None, output_config=None, tools=None):
    """Translate domain text/image blocks into the Responses input contract."""
    if not str(model).startswith(("gpt-", "o1", "o3", "o4")):
        raise ValueError("Research requests require an OpenAI model")
    inputs = []
    for message in messages:
        content = message["content"]
        if isinstance(content, list):
            converted = []
            for block in content:
                kind = block.get("type")
                if kind == "text":
                    converted.append({"type": "input_text", "text": block["text"]})
                elif kind == "image":
                    source = block["source"]
                    if source.get("type") != "base64":
                        raise ValueError("Unsupported image source")
                    converted.append({"type": "input_image", "image_url":
                                      f"data:{source['media_type']};base64,{source['data']}", "detail": "high"})
                else:
                    raise ValueError(f"Unsupported research content block: {kind}")
            content = converted
        inputs.append({"role": message["role"], "content": content})
    config = output_config or {}
    effort = config.get("effort") or ("medium" if model == "gpt-6-sol" else "low")
    payload = {"model": model, "input": inputs, "reasoning": {"effort": effort},
               "max_output_tokens": max(4096, int(max_tokens) + 4096), "store": False}
    if system:
        payload["instructions"] = system if isinstance(system, str) else "\n".join(b["text"] for b in system)
    if config.get("format"):
        fmt = config["format"]
        payload["text"] = {"format": {"type": "json_schema", "name": "research_result",
                                        "strict": True, "schema": fmt["schema"]}}
    if tools:
        if any(t.get("type") != "web_search" for t in tools):
            raise ValueError("Unsupported research tool")
        payload["tools"] = tools
    return payload


def message_result(response):
    if response.status != "completed":
        raise RuntimeError(f"OpenAI research response did not complete: {response.status}")
    text = response.output_text
    if not text or not text.strip():
        raise RuntimeError("OpenAI research response contains no text")
    usage = response.usage
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason="end_turn",
                           usage=SimpleNamespace(input_tokens=getattr(usage, "input_tokens", 0),
                                                 output_tokens=getattr(usage, "output_tokens", 0)))


class ResearchClient:
    """Synchronous OpenAI client; transport streams while domain code waits."""
    def __init__(self, api_key=None, timeout=180.0, max_retries=1):
        self.options = {**openai_settings(), "timeout": timeout, "max_retries": max_retries}
        if api_key:
            self.options["api_key"] = api_key
        self.messages = self

    def create(self, **kwargs):
        with OpenAI(**self.options) as client:
            with client.responses.stream(**request_payload(**kwargs)) as stream:
                return message_result(stream.get_final_response())

    @contextmanager
    def stream(self, **kwargs):
        result = self.create(**kwargs)
        yield SimpleNamespace(get_final_message=lambda: result)


class AsyncResearchClient:
    """Async OpenAI transport with deterministic connection cleanup."""
    def __init__(self, api_key=None, timeout=180.0, max_retries=1):
        self.options = {**openai_settings(), "timeout": timeout, "max_retries": max_retries}
        if api_key:
            self.options["api_key"] = api_key
        self.messages = self

    async def create(self, **kwargs):
        async with AsyncOpenAI(**self.options) as client:
            async with client.responses.stream(**request_payload(**kwargs)) as stream:
                return message_result(await stream.get_final_response())

    @asynccontextmanager
    async def stream(self, **kwargs):
        result = await self.create(**kwargs)
        async def final():
            return result
        yield SimpleNamespace(get_final_message=final)
