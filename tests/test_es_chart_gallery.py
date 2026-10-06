from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from backend.api.server import app
import backend.api.routes.charts as charts_module
from backend.api.routes.charts import _get_recent_rag_chart_rows, rag_chart_asset


def _chart_hit(
    chunk_id: str,
    caption: str,
    asset_path: str,
    niche: str,
) -> dict[str, object]:
    return {
        "chunk_id": chunk_id,
        "text": caption,
        "caption": caption,
        "image_url": f"/api/analysis/chart-asset/{asset_path}",
        "chart_file": asset_path.rsplit("/", 1)[-1],
        "chart_markdown": "",
        "filename": f"{chunk_id}.pdf",
        "page_number": 7,
        "date": "2026-08-26",
        "date_int": 20260826,
        "broker": "MS",
        "media_type": "chart",
        "content_type": "chart",
        "doc_type": "sellside",
        "source_doc_type": "broker_research",
        "source_rel_path": f"Equity Team/Stocks/Coverage/{chunk_id}.pdf",
        "primary_ticker": "",
        "primary_company": "",
        "industry": "semiconductors" if niche == "memory_semiconductors" else "ai_infrastructure",
        "industry_label": "Semiconductors" if niche == "memory_semiconductors" else "AI Infrastructure",
        "niche": niche,
        "subindustry_tags": [],
        "kpi_tags": [],
        "theme_tags": [],
        "catalyst_tags": [],
        "topic_tags": ["ai"],
        "company_tags": [],
        "period_tags": [],
        "geo_tags": [],
        "unit_tags": [],
        "score": 0.0,
        "metadata": {},
    }


class _EsHandler(BaseHTTPRequestHandler):
    request_json: dict[str, object] = {}
    request_api_key = ""
    request_path = ""
    asset_bytes = b"\xff\xd8fake-chart\xff\xd9"
    request_count = 0
    request_paths: list[str] = []
    delay_seconds = 0.0

    def do_POST(self) -> None:  # noqa: N802 - stdlib HTTP handler API
        type(self).request_count += 1
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        length = int(self.headers.get("Content-Length", "0"))
        type(self).request_json = json.loads(self.rfile.read(length))
        type(self).request_api_key = self.headers.get("X-RAG-Ingest-Key", "")
        type(self).request_path = self.path
        hits = [
            _chart_hit(
                "memory-chart-1",
                "HBM supply and demand",
                "v10/memory/memory-chart-1.jpg",
                "memory_semiconductors",
            ),
            _chart_hit(
                "gpu-chart-1",
                "GPU accelerator shipments",
                "v10/gpu/gpu-chart-1.jpg",
                "ai_accelerators_and_gpus",
            ),
        ]
        body = json.dumps(
            {
                "hits": hits,
                "n_total": len(hits),
                "counts": {"charts": len(hits)},
                "inventory": True,
                "ranked": False,
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib HTTP handler API
        type(self).request_api_key = self.headers.get("X-RAG-Ingest-Key", "")
        type(self).request_path = self.path
        type(self).request_paths.append(self.path)
        if self.path == "/api/rag/capabilities":
            body = json.dumps({"contract_version": "2026-09-02"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/api/charts/browse"):
            type(self).request_count += 1
            if self.delay_seconds:
                time.sleep(self.delay_seconds)
            params = parse_qs(urlparse(self.path).query)
            category = params.get("category", [""])[0]
            groups = [
                {
                    "group": "semiconductors",
                    "label": "Semiconductors",
                    "count": 1,
                    "charts": [] if not category else (
                        [_chart_hit("memory-chart-1", "HBM supply and demand", "v10/memory/memory-chart-1.jpg", "memory_semiconductors")]
                        if category == "semiconductors" else []
                    ),
                    "subsectors": [{"niche": "memory_semiconductors", "count": 1}],
                },
                {
                    "group": "ai_infrastructure",
                    "label": "AI Infrastructure",
                    "count": 1,
                    "charts": [] if not category else (
                        [_chart_hit("gpu-chart-1", "GPU accelerator shipments", "v10/gpu/gpu-chart-1.jpg", "ai_accelerators_and_gpus")]
                        if category == "ai_infrastructure" else []
                    ),
                    "subsectors": [{"niche": "ai_accelerators_and_gpus", "count": 1}],
                },
            ]
            if category:
                groups = [group for group in groups if group["group"] == category]
            body = json.dumps({"total": 2, "by": "industry", "groups": groups}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(self.asset_bytes)))
        self.end_headers()
        self.wfile.write(self.asset_bytes)

    def log_message(self, _format: str, *args: object) -> None:
        return


class _EmptyDb:
    async def execute_fetchall(self, _sql: str, *_args: object) -> list[object]:
        return []


class EsChartGalleryTests(unittest.TestCase):
    def setUp(self) -> None:
        _EsHandler.request_count = 0
        _EsHandler.request_paths = []
        _EsHandler.delay_seconds = 0.0
        cache = getattr(charts_module, "_rag_inventory_cache", None)
        if cache is not None:
            cache.clear()
        base_cache = getattr(charts_module, "_rag_gallery_base_cache", None)
        if base_cache is not None:
            base_cache.clear()

    def test_current_es_gallery_sector_fields_drive_memory_and_other_groups(self) -> None:
        memory = charts_module._rag_inventory_row_to_gallery({
            "filename": "memory.pdf",
            "image_url": "/api/analysis/chart-asset/v12/memory.jpg",
            "chart_file": "memory.jpg",
            "caption": "HBM supply",
            "industry": "semiconductors",
            "industry_label": "Semiconductors",
            "niche": "memory_semiconductors",
        })
        healthcare = charts_module._rag_inventory_row_to_gallery({
            "filename": "healthcare.pdf",
            "image_url": "/api/analysis/chart-asset/v12/healthcare.jpg",
            "chart_file": "healthcare.jpg",
            "caption": "MRI installed base",
            "industry": "healthcare",
            "industry_label": "Healthcare",
            "niche": "medtech_devices_china",
        })

        self.assertIsNotNone(memory)
        self.assertIsNotNone(healthcare)
        assert memory is not None
        assert healthcare is not None
        self.assertEqual("semiconductors", memory["primary_industry"])
        self.assertEqual("memory_semiconductors", memory["primary_niche_sector"])
        self.assertEqual("Memory Sector", charts_module._rag_main_group(memory))
        self.assertEqual("Healthcare", charts_module._rag_main_group(healthcare))
        self.assertEqual("Healthcare", healthcare["category_name"])
        self.assertEqual("Medtech Devices China", healthcare["subcategory_name"])

    def test_gallery_loads_complete_es_ai_theme_chart_basket(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _EsHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"

        async def fake_get_db() -> _EmptyDb:
            return _EmptyDb()

        try:
            with (
                patch.dict(
                    os.environ,
                    {"ESRAG_API_URL": base_url, "ES_RAG_API_KEY": "chart-key"},
                    clear=False,
                ),
                patch(
                    "backend.api.routes.charts._apply_rag_categories",
                    new=AsyncMock(),
                ),
                patch("backend.db.connection.get_db", new=fake_get_db),
            ):
                rows = asyncio.run(_get_recent_rag_chart_rows())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(2, len(rows))
        self.assertEqual(
            [
                "/api/charts/browse?group=industry&limit=1&offset=0&theme=ai",
                "/api/charts/browse?group=industry&limit=500&offset=0&category=semiconductors&theme=ai",
                "/api/charts/browse?group=industry&limit=500&offset=0&category=ai_infrastructure&theme=ai",
            ],
            [path for path in _EsHandler.request_paths if path.startswith("/api/charts/browse")],
        )
        self.assertEqual("chart-key", _EsHandler.request_api_key)
        self.assertEqual(
            {"memory_semiconductors", "ai_accelerators_and_gpus"},
            {row["primary_niche_sector"] for row in rows},
        )
        self.assertEqual([-3841974357, -18551043], [row["id"] for row in rows])
        self.assertEqual(
            "/api/charts/rag-asset/v10/gpu/gpu-chart-1.jpg",
            rows[1]["image_url"],
        )

    def test_gallery_separates_memory_tag_before_named_sector_groups(self) -> None:
        local_chart = {
            "id": 1,
            "category_name": "DRAM",
            "subcategory_name": "Pricing",
            "cat_order": 1,
            "sub_order": 1,
            "broker": "UBS",
            "chart_date_int": 20260901,
            "tag": "DRAM pricing",
        }
        rag_memory = {
            "id": -1,
            "category_name": "HBM",
            "subcategory_name": "HBM TAM",
            "primary_niche_sector": "ai_accelerators_and_gpus",
            "niche_sector_tags": ["ai_accelerators_and_gpus", "memory_semiconductors"],
            "is_rag": True,
            "broker": "MS",
            "chart_date_int": 20260902,
            "tag": "HBM TAM",
        }
        rag_other = {
            "id": -2,
            "category_name": "AI Infrastructure Buildout",
            "subcategory_name": "Accelerator demand",
            "primary_niche_sector": "ai_accelerators_and_gpus",
            "niche_sector_tags": ["ai_accelerators_and_gpus"],
            "is_rag": True,
            "broker": "GS",
            "chart_date_int": 20260903,
            "tag": "GPU demand",
        }

        with (
            patch.object(
                charts_module.queries,
                "get_charts_by_category",
                new=AsyncMock(return_value=[local_chart]),
            ),
            patch(
                "backend.api.routes.charts._get_recent_rag_chart_rows",
                new=AsyncMock(return_value=[rag_memory, rag_other]),
            ),
        ):
            response = TestClient(app).get("/api/charts/by-category")

        self.assertEqual(200, response.status_code)
        groups = response.json()
        self.assertEqual(
            [
                ("Memory Sector", "DRAM"),
                ("Memory Sector", "HBM"),
                ("AI Accelerators and GPUs", "AI Infrastructure Buildout"),
            ],
            [(group.get("main_group"), group["category"]) for group in groups],
        )

    def test_memory_categories_never_appear_under_non_memory_sector_groups(self) -> None:
        memory_categories = ["General Memory", "DDR DRAM", "DRAM", "HBM", "NAND"]
        rag_rows = [
            {
                "id": -(index + 1),
                "category_name": category,
                "subcategory_name": "Test subcategory",
                "primary_niche_sector": "ai_accelerators_and_gpus",
                "niche_sector_tags": ["ai_accelerators_and_gpus"],
                "is_rag": True,
                "broker": "MS",
                "chart_date_int": 20260902,
                "tag": category,
            }
            for index, category in enumerate(memory_categories)
        ]

        with (
            patch.object(
                charts_module.queries,
                "get_charts_by_category",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "backend.api.routes.charts._get_recent_rag_chart_rows",
                new=AsyncMock(return_value=rag_rows),
            ),
        ):
            response = TestClient(app).get("/api/charts/by-category")

        self.assertEqual(200, response.status_code)
        groups = response.json()
        self.assertEqual({"Memory Sector"}, {group["main_group"] for group in groups})
        self.assertEqual(set(memory_categories), {group["category"] for group in groups})

    def test_gallery_asset_is_proxied_from_es(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _EsHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"

        try:
            with patch.dict(
                os.environ,
                {"ESRAG_API_URL": base_url, "ES_RAG_API_KEY": "asset-key"},
                clear=False,
            ):
                try:
                    response = asyncio.run(
                        rag_chart_asset("v10/gpu/gpu-chart-1.jpg")
                    )
                    body: bytes | str = response.body
                except Exception as exc:
                    body = f"ERROR: {exc}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(_EsHandler.asset_bytes, body)
        self.assertEqual(
            "/api/analysis/chart-asset/v10/gpu/gpu-chart-1.jpg",
            _EsHandler.request_path,
        )
        self.assertEqual("asset-key", _EsHandler.request_api_key)
        self.assertEqual("image/jpeg", response.media_type)

    def test_repeated_gallery_reads_reuse_the_es_inventory(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _EsHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"

        async def fake_get_db() -> _EmptyDb:
            return _EmptyDb()

        async def load_twice() -> None:
            await _get_recent_rag_chart_rows()
            await _get_recent_rag_chart_rows()

        categorize = AsyncMock()
        try:
            with (
                patch.dict(
                    os.environ,
                    {"ESRAG_API_URL": base_url, "ES_RAG_API_KEY": "chart-key"},
                    clear=False,
                ),
                patch(
                    "backend.api.routes.charts._apply_rag_categories",
                    new=categorize,
                ),
                patch("backend.db.connection.get_db", new=fake_get_db),
            ):
                asyncio.run(load_twice())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(3, _EsHandler.request_count)
        self.assertEqual(1, categorize.await_count)

    def test_stale_gallery_returns_immediately_while_inventory_refreshes(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _EsHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"

        async def fake_get_db() -> _EmptyDb:
            return _EmptyDb()

        async def stale_read() -> tuple[float, int]:
            await _get_recent_rag_chart_rows()
            charts_module._rag_inventory_cache.loaded_at = 0.0
            _EsHandler.delay_seconds = 0.25
            started = time.perf_counter()
            rows = await _get_recent_rag_chart_rows()
            elapsed = time.perf_counter() - started
            refresh_task = getattr(charts_module._rag_inventory_cache, "refresh_task", None)
            if refresh_task is not None:
                await refresh_task
            return elapsed, len(rows)

        try:
            with (
                patch.dict(
                    os.environ,
                    {"ESRAG_API_URL": base_url, "ES_RAG_API_KEY": "chart-key"},
                    clear=False,
                ),
                patch(
                    "backend.api.routes.charts._apply_rag_categories",
                    new=AsyncMock(),
                ),
                patch("backend.db.connection.get_db", new=fake_get_db),
            ):
                elapsed, row_count = asyncio.run(stale_read())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(2, row_count)
        self.assertLess(elapsed, 0.1)
        self.assertEqual(6, _EsHandler.request_count)

    def test_normal_gallery_response_allows_short_browser_cache(self) -> None:
        with (
            patch.object(charts_module.queries, "get_charts_by_category", new=AsyncMock(return_value=[])),
            patch.object(charts_module, "_get_recent_rag_chart_rows", new=AsyncMock(return_value=[])),
        ):
            response = TestClient(app).get("/api/charts/by-category")

        self.assertEqual(200, response.status_code)
        self.assertEqual("private, max-age=60, stale-while-revalidate=300", response.headers.get("cache-control"))
        self.assertEqual("false", response.headers.get("x-es-rag-stale"))

    def test_stale_gallery_response_is_identified(self) -> None:
        charts_module._rag_inventory_cache.value = []
        charts_module._rag_inventory_cache.loaded_at = 0.0
        with (
            patch.object(charts_module.queries, "get_charts_by_category", new=AsyncMock(return_value=[])),
            patch.object(charts_module, "_get_recent_rag_chart_rows", new=AsyncMock(return_value=[])),
        ):
            response = TestClient(app).get("/api/charts/by-category")

        self.assertEqual(200, response.status_code)
        self.assertEqual("true", response.headers.get("x-es-rag-stale"))

    def test_manual_gallery_refresh_bypasses_inventory_and_browser_caches(self) -> None:
        rag_rows = AsyncMock(return_value=[])
        with (
            patch.object(charts_module.queries, "get_charts_by_category", new=AsyncMock(return_value=[])),
            patch.object(charts_module, "_get_recent_rag_chart_rows", new=rag_rows),
        ):
            response = TestClient(app).get("/api/charts/by-category?refresh=true")

        self.assertEqual(200, response.status_code)
        self.assertEqual("no-store", response.headers.get("cache-control"))
        rag_rows.assert_awaited_once_with(force_refresh=True)


if __name__ == "__main__":
    unittest.main()
