from __future__ import annotations

import asyncio
import json
import sys
import types
import unittest
from unittest.mock import patch

from backend.api.routes.paste import _llm_tag_and_categorize
from backend.api.routes.charts import _save_rag_category_cache
from backend.services.categorizer import categorize_batch


class _FakeResponses:
    calls: list[dict] = []
    payload: dict = {}

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(output_text=json.dumps(self.payload))


class _FakeAsyncOpenAI:
    def __init__(self, **_kwargs):
        self.responses = _FakeResponses()


class OpenAiChartCategorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        _FakeResponses.calls = []
        _FakeResponses.payload = {}
        self.fake_openai = types.SimpleNamespace(AsyncOpenAI=_FakeAsyncOpenAI)

    def test_rag_caption_categorization_uses_luna_with_low_reasoning(self) -> None:
        _FakeResponses.payload = {
            "results": [
                {
                    "index": 0,
                    "category": "HBM",
                    "subcategory": "HBM market share",
                    "new_subcategory": None,
                    "companies": ["SK Hynix"],
                }
            ]
        }

        with (
            patch.dict(
                sys.modules,
                {"openai": self.fake_openai},
            ),
            patch.dict(
                "os.environ",
                {
                    "OPENAI_API_KEY": "test-key",
                    "ANTHROPIC_API_KEY": "",
                },
                clear=False,
            ),
        ):
            result = asyncio.run(
                categorize_batch(
                    [{"tag": "HBM share", "caption": "SK Hynix gains share"}],
                    [{"name": "HBM", "subcategories": [{"name": "HBM market share"}]}],
                )
            )

        self.assertEqual("HBM", result[0].category)
        self.assertEqual("HBM market share", result[0].subcategory)
        request = _FakeResponses.calls[0]
        self.assertEqual("gpt-5.6-luna", request["model"])
        self.assertEqual({"effort": "low"}, request["reasoning"])
        self.assertEqual("json_schema", request["text"]["format"]["type"])
        self.assertEqual("input_text", request["input"][0]["content"][0]["type"])

    def test_pasted_chart_sends_image_to_luna_vision(self) -> None:
        _FakeResponses.payload = {
            "topic": "HBM TAM forecast",
            "chart_type": "bar",
            "broker": "UBS",
            "date": "2026-09-07",
            "tag": "HBM TAM forecast - UBS 20260907",
            "caption": "HBM market growth forecast.",
            "category": "HBM",
            "subcategory": "HBM TAM",
            "companies": ["General"],
            "estimates": "2027e: $40B",
        }

        with (
            patch.dict(sys.modules, {"openai": self.fake_openai}),
            patch.dict(
                "os.environ",
                {
                    "OPENAI_API_KEY": "test-key",
                    "ANTHROPIC_API_KEY": "",
                },
                clear=False,
            ),
        ):
            result = asyncio.run(
                _llm_tag_and_categorize(
                    b"png-bytes",
                    "image/png",
                    "HBM forecast",
                    [{"name": "HBM", "subcategories": [{"name": "HBM TAM"}]}],
                )
            )

        self.assertEqual("HBM TAM forecast", result.get("topic"))
        request = _FakeResponses.calls[0]
        self.assertEqual("gpt-5.6-luna", request["model"])
        self.assertEqual({"effort": "low"}, request["reasoning"])
        content = request["input"][0]["content"]
        image = next(item for item in content if item["type"] == "input_image")
        self.assertEqual("high", image["detail"])
        self.assertTrue(image["image_url"].startswith("data:image/png;base64,"))

    def test_rag_cache_records_luna_model_and_effort(self) -> None:
        calls: list[tuple[str, tuple]] = []

        class _FakeDb:
            async def execute(self, sql, params=()):
                calls.append((sql, tuple(params)))

            async def commit(self):
                return None

        async def _fake_get_db():
            return _FakeDb()

        async def _already_created():
            return None

        with (
            patch("backend.api.routes.charts._ensure_rag_category_cache_table", _already_created),
            patch("backend.db.connection.get_db", _fake_get_db),
        ):
            asyncio.run(
                _save_rag_category_cache(
                    [{
                        "id": 42,
                        "tag": "HBM share",
                        "caption": "SK Hynix gains share",
                        "category_name": "HBM",
                        "subcategory_name": "HBM market share",
                    }]
                )
            )

        sql, params = calls[0]
        self.assertNotIn("'haiku'", sql)
        self.assertEqual("gpt-5.6-luna:low", params[-1])

    def test_missing_openai_key_preserves_existing_fallbacks(self) -> None:
        with patch.dict("os.environ", {"OPENAI_API_KEY": ""}, clear=False):
            rag_result = asyncio.run(
                categorize_batch(
                    [{"tag": "HBM share", "caption": "SK Hynix gains share"}],
                    [],
                )
            )
            paste_result = asyncio.run(
                _llm_tag_and_categorize(b"png-bytes", "image/png", "My hint", [])
            )

        self.assertEqual("General Memory", rag_result[0].category)
        self.assertEqual("Overall memory market", rag_result[0].subcategory)
        self.assertEqual("My hint", paste_result["tag"])
        self.assertEqual("General Memory", paste_result["category"])
        self.assertEqual([], _FakeResponses.calls)


if __name__ == "__main__":
    unittest.main()
