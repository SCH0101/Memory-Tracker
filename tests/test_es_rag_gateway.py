from __future__ import annotations

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from backend.services.es_rag_gateway import (
    EsRagConfigurationError,
    EsRagContractError,
    EsRagGateway,
    EsRagTransportError,
)


class _GatewayHandler(BaseHTTPRequestHandler):
    request_path = ""
    request_headers: dict[str, str] = {}
    request_json: dict[str, object] = {}
    request_body = b""
    request_paths: list[str] = []
    capabilities_payload: dict[str, object] = {
        "contract_version": "2026-09-02",
        "endpoints": {"query": "/api/rag/query"},
    }
    capability_requests = 0
    response_status = 200
    response_override: dict[str, object] | None = None
    error_detail = "query failed"
    shift_last_gallery_page_once = False
    gallery_category_requests = 0

    @classmethod
    def reset(cls) -> None:
        cls.request_path = ""
        cls.request_headers = {}
        cls.request_json = {}
        cls.request_body = b""
        cls.request_paths = []
        cls.capabilities_payload = {
            "contract_version": "2026-09-02",
            "endpoints": {"query": "/api/rag/query"},
        }
        cls.capability_requests = 0
        cls.response_status = 200
        cls.response_override = None
        cls.error_detail = "query failed"
        cls.shift_last_gallery_page_once = False
        cls.gallery_category_requests = 0

    def _capture(self) -> bytes:
        type(self).request_path = self.path
        type(self).request_paths.append(self.path)
        type(self).request_headers = {
            key.lower(): value for key, value in self.headers.items()
        }
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        type(self).request_body = body
        if self.headers.get_content_type() == "application/json" and body:
            type(self).request_json = json.loads(body)
        return body

    def _json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        self._capture()
        if self.path == "/api/rag/capabilities":
            type(self).capability_requests += 1
            self._json(type(self).capabilities_payload, type(self).response_status)
        elif self.path.startswith("/api/rag/health"):
            self._json({"status": "ok", "collection": "equity_research_openai"})
        elif self.path.startswith("/api/rag/sources"):
            self._json({"sources": [{"filename": "MS memory.pdf", "chunk_count": 2}]})
        elif self.path.startswith("/api/rag/chunks"):
            self._json({
                "filename": "MS memory.pdf",
                "chunks": [{"id": "one", "text": "HBM", "metadata": {"page": 3}}],
                "next_offset": None,
            })
        elif self.path.startswith("/api/charts/browse"):
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            category = params.get("category", [""])[0]
            offset = int(params.get("offset", ["0"])[0])
            limit = int(params.get("limit", ["20"])[0])
            if not category:
                self._json({
                    "total": 501,
                    "by": "industry",
                    "groups": [{
                        "group": "semiconductors",
                        "label": "Semiconductors",
                        "count": 501,
                        "charts": [],
                        "subsectors": [{"niche": "memory_semiconductors", "count": 501}],
                    }],
                })
            else:
                type(self).gallery_category_requests += 1
                shifted = (
                    type(self).shift_last_gallery_page_once
                    and offset == 500
                    and type(self).gallery_category_requests == 2
                )
                indexes = (
                    [499]
                    if shifted
                    else range(offset, min(offset + limit, 501))
                )
                charts = [
                    {
                        "chart_file": f"memory-{index}.jpg",
                        "image_url": f"/api/analysis/chart-asset/v12/memory-{index}.jpg",
                        "caption": f"Memory chart {index}",
                        "industry": "",
                        "industry_label": "",
                        "niche": "memory_semiconductors",
                    }
                    for index in indexes
                ]
                self._json({
                    "total": 501,
                    "by": "industry",
                    "groups": [{
                        "group": "semiconductors",
                        "label": "Semiconductors",
                        "count": 501,
                        "charts": charts,
                        "subsectors": [{"niche": "memory_semiconductors", "count": 501}],
                    }],
                })
        elif self.path == "/api/rag/jobs/job-1":
            self._json({"id": "job-1", "status": "done", "result": {"chunks": 4}})
        elif self.path.startswith("/api/analysis/chart-asset/"):
            body = b"\xff\xd8gateway-chart\xff\xd9"
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._json({"detail": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        body = self._capture()
        if self.path == "/api/rag/query":
            if type(self).response_status != 200:
                self._json({"detail": type(self).error_detail}, type(self).response_status)
                return
            inventory = self.request_json.get("n_results") == 0
            self._json({
                "hits": [{
                    "text": "HBM supply",
                    "image_url": "/api/analysis/chart-asset/v10/chart.jpg",
                    "metadata": {},
                }],
                "inventory": inventory,
                "ranked": not inventory,
            })
        elif self.path == "/api/rag/ingest-pdf":
            self._json({"job_id": "job-1", "status": "queued"})
        elif self.path == "/api/rag/ask":
            payload = (
                b"event: token\ndata: {\"token\":\"HBM \"}\n\n"
                b"event: done\ndata: {\"content\":\"HBM answer\"}\n\n"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif type(self).response_override is not None:
            self._json(type(self).response_override)
        else:
            self._json({"ok": True, "chunks": 2, "result": {"indexed": 1}})

    def log_message(self, _format: str, *args: object) -> None:
        return


class EsRagGatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _GatewayHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self) -> None:
        _GatewayHandler.reset()

    def test_environment_requires_url_and_key(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(EsRagConfigurationError, "ESRAG_API_URL"):
                EsRagGateway.from_environment()
        with patch.dict(os.environ, {"ESRAG_API_URL": self.base_url}, clear=True):
            with self.assertRaisesRegex(EsRagConfigurationError, "ES_RAG_API_KEY"):
                EsRagGateway.from_environment()

    def test_query_sends_auth_and_latest_payload(self) -> None:
        gateway = EsRagGateway(self.base_url, "secret", capability_ttl_seconds=60)
        result = gateway.query(
            "HBM supply",
            source_filter="sellside",
            n_results=7,
            metadata_filters={"broker": "MS"},
            rerank_candidates=20,
        )
        self.assertEqual("secret", _GatewayHandler.request_headers["x-rag-ingest-key"])
        self.assertEqual("/api/rag/query", _GatewayHandler.request_path)
        self.assertEqual("sellside", _GatewayHandler.request_json["source_filter"])
        self.assertEqual({"broker": "MS"}, _GatewayHandler.request_json["metadata_filters"])
        self.assertEqual(20, _GatewayHandler.request_json["rerank_candidates"])
        self.assertEqual("HBM supply", result["hits"][0]["text"])

    def test_capabilities_are_cached_and_reject_older_contract(self) -> None:
        gateway = EsRagGateway(self.base_url, "secret", capability_ttl_seconds=60)
        self.assertEqual("2026-09-02", gateway.capabilities()["contract_version"])
        self.assertEqual("2026-09-02", gateway.capabilities()["contract_version"])
        self.assertEqual(1, _GatewayHandler.capability_requests)

        _GatewayHandler.capabilities_payload = {"contract_version": "2026-08-31"}
        with self.assertRaisesRegex(EsRagContractError, "2026-09-02"):
            EsRagGateway(self.base_url, "secret").capabilities()

    def test_chart_inventory_uses_current_gallery_browse_pages(self) -> None:
        gateway = EsRagGateway(self.base_url, "secret")
        result = gateway.chart_inventory()
        self.assertTrue(result["inventory"])
        self.assertEqual(501, len(result["hits"]))
        self.assertEqual(
            {"semiconductors"},
            {row["industry"] for row in result["hits"]},
        )
        self.assertEqual(
            [
                "/api/charts/browse?group=industry&limit=1&offset=0",
                "/api/charts/browse?group=industry&limit=500&offset=0&category=semiconductors",
                "/api/charts/browse?group=industry&limit=500&offset=500&category=semiconductors",
            ],
            [path for path in _GatewayHandler.request_paths if path.startswith("/api/charts/browse")],
        )

    def test_chart_asset_uses_external_contract(self) -> None:
        gateway = EsRagGateway(self.base_url, "secret")
        content, media_type = gateway.chart_asset("v10/chart.jpg")
        self.assertEqual(b"\xff\xd8gateway-chart\xff\xd9", content)
        self.assertEqual("image/jpeg", media_type)
        with self.assertRaises(EsRagContractError):
            gateway.chart_asset("../secret")

    def test_chart_inventory_retries_a_shifted_page_and_deduplicates(self) -> None:
        _GatewayHandler.shift_last_gallery_page_once = True
        gateway = EsRagGateway(self.base_url, "secret")

        result = gateway.chart_inventory()

        chart_files = [row["chart_file"] for row in result["hits"]]
        self.assertEqual(501, len(chart_files))
        self.assertEqual(501, len(set(chart_files)))
        browse_paths = [
            path for path in _GatewayHandler.request_paths
            if path.startswith("/api/charts/browse")
        ]
        self.assertEqual(5, len(browse_paths))

    def test_sources_chunks_and_jobs_preserve_contract_rows(self) -> None:
        gateway = EsRagGateway(self.base_url, "secret")
        sources = gateway.sources(broker="MS", theme="ai")
        self.assertEqual("MS memory.pdf", sources["sources"][0]["filename"])
        chunks = gateway.chunks(filename="MS memory.pdf", offset=0)
        self.assertEqual("HBM", chunks["chunks"][0]["text"])
        self.assertEqual("done", gateway.get_job("job-1")["status"])

    def test_pdf_ingest_is_multipart_and_ask_consumes_sse(self) -> None:
        gateway = EsRagGateway(self.base_url, "secret")
        job = gateway.ingest_pdf(
            b"%PDF-fake",
            filename="memory.pdf",
            ticker="MEMORY",
            broker="MS",
        )
        self.assertEqual("job-1", job["job_id"])
        content_type = _GatewayHandler.request_headers["content-type"]
        self.assertIn("multipart/form-data; boundary=", content_type)
        self.assertIn(b'name="file"; filename="memory.pdf"', _GatewayHandler.request_body)
        self.assertIn(b'name="broker"', _GatewayHandler.request_body)

        answer = gateway.ask(
            "What is HBM supply?",
            session_id="session-1",
            project="memory_tracker",
            ticker="MEMORY",
            theme="ai",
        )
        self.assertEqual("HBM answer", answer)

    def test_http_failures_are_transport_errors_without_key_leakage(self) -> None:
        _GatewayHandler.response_status = 503
        _GatewayHandler.error_detail = "upstream echoed secret-key-value"
        gateway = EsRagGateway(self.base_url, "secret-key-value")
        with self.assertRaises(EsRagTransportError) as raised:
            gateway.query("HBM")
        self.assertEqual(503, raised.exception.status_code)
        self.assertNotIn("secret-key-value", str(raised.exception))

    def test_write_responses_require_confirmed_success_shapes(self) -> None:
        gateway = EsRagGateway(self.base_url, "secret")
        _GatewayHandler.response_override = {}
        with self.assertRaises(EsRagContractError):
            gateway.index_text("HBM", filename="memory.pdf")

        _GatewayHandler.response_override = {"ok": False, "chunks": 0}
        with self.assertRaises(EsRagContractError):
            gateway.index_excel(b"workbook", filename="memory.xlsx")

        _GatewayHandler.response_override = {"ok": True, "result": {}}
        with self.assertRaises(EsRagContractError):
            gateway.inject_chart(b"image", filename="chart.png")

    def test_multipart_filename_strips_header_control_characters(self) -> None:
        gateway = EsRagGateway(self.base_url, "secret")
        gateway.ingest_pdf(b"%PDF", filename="safe\r\nX-Evil: yes.pdf")
        self.assertNotIn(b"\r\nX-Evil:", _GatewayHandler.request_body)


if __name__ == "__main__":
    unittest.main()
