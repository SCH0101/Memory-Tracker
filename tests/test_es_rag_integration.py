from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, mock_open, patch

from fastapi.testclient import TestClient

from backend.api.server import app
import backend.api.routes.corpus_stats as corpus_stats_module
from backend.services.es_rag_gateway import EsRagTransportError
from backend.services.fact_pool import retrieve_for_section, retrieve_rag_charts
from backend.services.fact_pool import _index_pdf_sync, index_pasted_chart_to_rag
import backend.services.dashboard_refresh as dashboard_refresh


class EsRagReadIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        corpus_stats_module._cache = {"data": None, "ts": 0.0}

    def test_retrieve_for_section_uses_gateway_and_normalizes_hit(self) -> None:
        gateway = MagicMock()
        gateway.query.return_value = {
            "hits": [{
                "text": "HBM supply grows 40% in 2027.",
                "content_type": "narrative",
                "media_type": "text",
                "doc_type": "sellside",
                "source_doc_type": "broker_research",
                "broker": "MS",
                "filename": "MS memory.pdf",
                "date": "2026-08-26",
                "page_number": 7,
            }]
        }
        with patch(
            "backend.services.fact_pool.get_es_rag_gateway",
            return_value=gateway,
        ):
            rows = asyncio.run(retrieve_for_section(
                "HBM supply",
                top_n=1,
                source_options=["broker_research"],
            ))

        self.assertEqual(1, len(rows))
        self.assertEqual("MS", rows[0]["broker"])
        self.assertEqual("MS memory.pdf", rows[0]["filename"])
        self.assertEqual(7, rows[0]["page"])
        gateway.query.assert_called_once_with(
            "HBM supply",
            ticker="",
            source_filter="sellside",
            n_results=15,
            user_input=False,
            rerank_candidates=None,
        )

    def test_retrieval_failure_is_not_reinterpreted_as_empty_evidence(self) -> None:
        gateway = MagicMock()
        gateway.query.side_effect = EsRagTransportError(
            "ES unavailable", operation="query"
        )
        with patch(
            "backend.services.fact_pool.get_es_rag_gateway",
            return_value=gateway,
        ):
            with self.assertRaises(EsRagTransportError):
                asyncio.run(retrieve_for_section("HBM supply"))

    def test_retrieve_for_section_forwards_exact_source_metadata_filter(self) -> None:
        gateway = MagicMock()
        gateway.query.return_value = {
            "hits": [{
                "text": "Latest Arete HBM view.",
                "content_type": "narrative",
                "media_type": "text",
                "source_doc_type": "industry_report",
                "broker": "Arete",
                "filename": "latest-arete.pdf",
                "date": "2026-09-17",
                "page_number": 3,
            }]
        }
        metadata_filters = {"filename": {"$eq": "latest-arete.pdf"}}

        with patch(
            "backend.services.fact_pool.get_es_rag_gateway",
            return_value=gateway,
        ):
            rows = asyncio.run(retrieve_for_section(
                "HBM supply",
                top_n=1,
                source_options=["industry_report"],
                metadata_filters=metadata_filters,
            ))

        self.assertEqual("latest-arete.pdf", rows[0]["filename"])
        self.assertEqual(metadata_filters, gateway.query.call_args.kwargs["metadata_filters"])

    def test_ranked_chart_search_uses_gateway_chart_query_and_proxy_url(self) -> None:
        gateway = MagicMock()
        gateway.query.return_value = {
            "hits": [{
                "chunk_id": "chart-one",
                "text": "HBM supply demand",
                "caption": "HBM supply demand",
                "image_url": "/api/analysis/chart-asset/v10/memory/chart-one.jpg",
                "chart_file": "chart-one.jpg",
                "filename": "MS memory.pdf",
                "page_number": 4,
                "date": "2026-08-26",
                "broker": "MS",
                "media_type": "chart",
                "content_type": "chart",
                "doc_type": "sellside",
                "source_doc_type": "broker_research",
                "topic_tags": ["ai"],
            }]
        }
        with patch(
            "backend.services.fact_pool.get_es_rag_gateway",
            return_value=gateway,
        ):
            rows = asyncio.run(retrieve_rag_charts(
                "HBM supply",
                top_n=3,
                ranked=True,
                theme="ai",
            ))

        self.assertEqual(1, len(rows))
        self.assertEqual(
            "/api/charts/rag-asset/v10/memory/chart-one.jpg",
            rows[0]["image_url"],
        )
        gateway.query.assert_called_once()
        kwargs = gateway.query.call_args.kwargs
        self.assertEqual("charts", kwargs["source_filter"])
        self.assertEqual("ai", kwargs["theme"])

    def test_corpus_stats_uses_external_health_without_direct_point_count(self) -> None:
        gateway = MagicMock()
        gateway.health.return_value = {
            "status": "ok",
            "collection": "equity_research_openai",
            "qdrant_alive": True,
            "qdrant_collection_healthy": True,
            "queue": {"depth": 0},
        }
        with patch(
            "backend.api.routes.corpus_stats.get_es_rag_gateway",
            return_value=gateway,
        ):
            response = TestClient(app).get("/api/corpus-stats")

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual("equity_research_openai", payload["collection"])
        self.assertNotIn("points", payload)
        gateway.health.assert_called_once_with(detail=True)

    def test_corpus_stats_accepts_latest_health_shape_without_status(self) -> None:
        gateway = MagicMock()
        gateway.health.return_value = {
            "collection": "equity_research_openai",
            "qdrant_alive": True,
            "qdrant_collection_healthy": True,
            "queue": {"depth": 0},
        }
        with patch(
            "backend.api.routes.corpus_stats.get_es_rag_gateway",
            return_value=gateway,
        ):
            response = TestClient(app).get("/api/corpus-stats")

        self.assertEqual(200, response.status_code)
        self.assertTrue(response.json()["ok"])

    def test_gateway_error_handler_returns_safe_503(self) -> None:
        gateway = MagicMock()
        gateway.health.side_effect = EsRagTransportError(
            "connection refused", operation="health"
        )
        with patch(
            "backend.api.routes.corpus_stats.get_es_rag_gateway",
            return_value=gateway,
        ):
            response = TestClient(app).get("/api/corpus-stats")

        self.assertEqual(503, response.status_code)
        self.assertEqual(
            {
                "detail": "ES RAG service unavailable",
                "operation": "health",
            },
            response.json(),
        )


class EsRagWriteIntegrationTests(unittest.TestCase):
    def test_pasted_text_uses_authenticated_gateway_without_direct_fallback(self) -> None:
        gateway = MagicMock()
        gateway.inject_fact.return_value = {
            "ok": True,
            "result": {"fact_id": "fact-9", "chunks": 1},
        }
        with patch(
            "backend.api.routes.paste.get_es_rag_gateway",
            return_value=gateway,
        ):
            response = TestClient(app).post(
                "/api/paste-text",
                data={
                    "text": "HBM demand is strengthening.",
                    "title": "HBM note",
                    "ticker": "MU",
                    "broker": "MS",
                    "date_pertains_to": "2026-09-16",
                    "sector": "memory_semiconductors",
                    "auto_tag": "false",
                },
            )

        self.assertEqual(200, response.status_code)
        payload = response.json()
        self.assertTrue(payload["ok"])
        self.assertEqual("http_authed", payload["via"])
        gateway.inject_fact.assert_called_once_with(
            "HBM demand is strengthening.",
            date="2026-09-16",
            sector="memory_semiconductors",
            ticker="MU",
            broker="MS",
            title="HBM note",
            company_tags=[],
            niche_sector_tags=["memory_semiconductors"],
            industry_tags=["memory_semiconductors"],
            kpi_tags=[],
            topic_tags=[],
            source="memory_chart_paste",
        )

    def test_preextracted_text_uses_gateway_without_direct_fallback(self) -> None:
        gateway = MagicMock()
        gateway.index_text.return_value = {"chunks": 3, "skipped": False}
        with patch(
            "backend.services.fact_pool.get_es_rag_gateway",
            return_value=gateway,
        ):
            count = _index_pdf_sync(
                "<<PAGE:1>>HBM supply",
                "MS memory.pdf",
                "MS",
                "2026-08-26",
            )

        self.assertEqual(3, count)
        gateway.index_text.assert_called_once_with(
            "<<PAGE:1>>HBM supply",
            filename="MS memory.pdf",
            ticker="MEMORY",
            broker="MS",
            date="2026-08-26",
            report_type="broker_research",
            force=False,
        )

    def test_preextracted_text_transport_failure_propagates(self) -> None:
        gateway = MagicMock()
        gateway.index_text.side_effect = EsRagTransportError(
            "ES unavailable", operation="index_text"
        )
        with patch(
            "backend.services.fact_pool.get_es_rag_gateway",
            return_value=gateway,
        ):
            with self.assertRaises(EsRagTransportError):
                _index_pdf_sync("text", "memory.pdf", "MS", "2026-08-26")

    def test_pasted_chart_uses_es_returned_asset_without_local_es_copy(self) -> None:
        gateway = MagicMock()
        gateway.inject_chart.return_value = {
            "ok": True,
            "result": {
                "indexed": 1,
                "chart_file": "v10/memory/chart-1.jpg",
                "image_url": "/api/analysis/chart-asset/v10/memory/chart-1.jpg",
            },
        }
        with patch(
            "backend.services.fact_pool.get_es_rag_gateway",
            return_value=gateway,
        ):
            result = index_pasted_chart_to_rag(
                b"image-bytes",
                chart_file="pasted.jpg",
                caption="HBM supply",
                broker="MS",
                date="2026-08-26",
                page=2,
            )

        self.assertEqual(1, result["indexed"])
        self.assertEqual("v10/memory/chart-1.jpg", result["chart_file"])
        self.assertEqual(
            "/api/charts/rag-asset/v10/memory/chart-1.jpg",
            result["image_url"],
        )
        gateway.inject_chart.assert_called_once()

    def test_upload_route_uses_public_gateway(self) -> None:
        gateway = MagicMock()
        gateway.ingest_pdf.return_value = {"job_id": "job-9", "status": "queued"}
        with patch(
            "backend.api.routes.upload.get_es_rag_gateway",
            return_value=gateway,
        ):
            response = TestClient(app).post(
                "/api/upload",
                files={"files": ("MS memory.pdf", b"%PDF-fake", "application/pdf")},
            )

        self.assertEqual(200, response.status_code)
        self.assertEqual("esrag:job-9", response.json()[0]["job_id"])
        gateway.ingest_pdf.assert_called_once()

    def test_upload_failure_uses_safe_global_503(self) -> None:
        gateway = MagicMock()
        gateway.ingest_pdf.side_effect = EsRagTransportError(
            "private upstream detail", operation="ingest_pdf"
        )
        with patch(
            "backend.api.routes.upload.get_es_rag_gateway",
            return_value=gateway,
        ):
            response = TestClient(app).post(
                "/api/upload",
                files={"files": ("memory.pdf", b"%PDF-fake", "application/pdf")},
            )

        self.assertEqual(503, response.status_code)
        self.assertEqual(
            {"detail": "ES RAG service unavailable", "operation": "ingest_pdf"},
            response.json(),
        )
        self.assertNotIn("private upstream detail", response.text)

    def test_chart_asset_failure_uses_safe_global_503(self) -> None:
        gateway = MagicMock()
        gateway.chart_asset.side_effect = EsRagTransportError(
            "private upstream detail", operation="chart_asset"
        )
        with patch(
            "backend.api.routes.charts.get_es_rag_gateway",
            return_value=gateway,
        ):
            response = TestClient(app).get("/api/charts/rag-asset/v10/chart.jpg")

        self.assertEqual(503, response.status_code)
        self.assertEqual("chart_asset", response.json()["operation"])
        self.assertNotIn("private upstream detail", response.text)

    def test_excel_gateway_failure_is_not_reported_as_local_success(self) -> None:
        with (
            patch("backend.api.routes.excel.EXCEL_UPLOAD_DIR", new=MagicMock()),
            patch("builtins.open", mock_open()),
            patch("backend.api.routes.excel.shutil.copyfileobj"),
            patch(
                "backend.api.routes.excel.index_excel_model",
                side_effect=EsRagTransportError(
                    "private upstream detail", operation="index_excel"
                ),
            ),
        ):
            response = TestClient(app).post(
                "/api/upload-excel",
                data={"broker": "MS", "force": "false"},
                files={
                    "file": (
                        "MS memory.xlsx",
                        b"fake-workbook",
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                },
            )

        self.assertEqual(503, response.status_code)
        self.assertEqual("index_excel", response.json()["operation"])


class DashboardHttpOnlyTests(unittest.TestCase):
    def setUp(self) -> None:
        dashboard_refresh._user_input_cache = None
        dashboard_refresh._user_input_cache_ts = 0.0

    def test_source_discovery_uses_gateway_inventory(self) -> None:
        gateway = MagicMock()
        gateway.sources.return_value = {
            "sources": [{
                "filename": "MS AI memory 20260910.pdf",
                "broker": "MS",
                "date": "2026-09-10",
                "doc_type": "sellside",
                "source_doc_type": "broker_research",
                "primary_niche_sector": "memory_semiconductors",
                "chunk_count": 4,
            }]
        }
        with patch(
            "backend.services.dashboard_refresh.get_es_rag_gateway",
            return_value=gateway,
        ):
            eligible, skipped = dashboard_refresh._sources_from_esrag_for_broker(
                "MS", ["broker_research"]
            )

        self.assertEqual([], skipped)
        self.assertEqual("MS AI memory 20260910.pdf", eligible[0]["filename"])
        gateway.sources.assert_called_once_with(
            broker="MS",
            theme="ai",
            source_doc_type="broker_research",
            pdf_only=True,
            limit=dashboard_refresh._ESRAG_SOURCES_LIMIT,
        )

    def test_chunk_loading_pages_through_gateway(self) -> None:
        gateway = MagicMock()
        gateway.chunks.side_effect = [
            {
                "chunks": [{
                    "id": "one",
                    "text": "HBM supply",
                    "metadata": {"filename": "MS memory.pdf", "page_number": 1},
                }],
                "next_offset": 1,
            },
            {
                "chunks": [{
                    "id": "two",
                    "text": "DRAM demand",
                    "metadata": {"filename": "MS memory.pdf", "page_number": 2},
                }],
                "next_offset": None,
            },
        ]
        with patch(
            "backend.services.dashboard_refresh.get_es_rag_gateway",
            return_value=gateway,
        ):
            rows = dashboard_refresh._chunks_from_esrag_for_filename("MS memory.pdf")

        self.assertEqual(["HBM supply", "DRAM demand"], [row["_text"] for row in rows])
        self.assertEqual([0, 1], [call.kwargs["offset"] for call in gateway.chunks.call_args_list])

    def test_source_discovery_pages_through_gateway(self) -> None:
        def source(filename: str) -> dict:
            return {
                "filename": filename,
                "broker": "MS",
                "date": "2026-09-10",
                "doc_type": "sellside",
                "source_doc_type": "broker_research",
                "primary_niche_sector": "memory_semiconductors",
                "chunk_count": 1,
            }

        gateway = MagicMock()
        gateway.sources.side_effect = [
            {"sources": [source("one.pdf")], "next_offset": 30},
            {"sources": [source("two.pdf")], "next_offset": None},
        ]
        with patch(
            "backend.services.dashboard_refresh.get_es_rag_gateway",
            return_value=gateway,
        ):
            eligible, skipped = dashboard_refresh._sources_from_esrag_for_broker(
                "MS", ["broker_research"]
            )

        self.assertEqual([], skipped)
        self.assertEqual(["one.pdf", "two.pdf"], [row["filename"] for row in eligible])
        self.assertNotIn("offset", gateway.sources.call_args_list[0].kwargs)
        self.assertEqual(30, gateway.sources.call_args_list[1].kwargs["offset"])

    def test_user_inputs_are_loaded_through_sources_and_chunks(self) -> None:
        gateway = MagicMock()
        gateway.sources.return_value = {
            "sources": [{"filename": "Memory note", "doc_type": "user_input"}]
        }
        gateway.chunks.return_value = {
            "chunks": [{
                "id": "note-1",
                "text": "MS expects stronger HBM demand",
                "metadata": {
                    "filename": "Memory note",
                    "title": "Memory note",
                    "doc_type": "user_input",
                    "broker": "MS",
                    "date": "2026-09-15",
                    "niche_sector_tags": ["memory_semiconductors"],
                },
            }],
            "next_offset": None,
        }
        with patch(
            "backend.services.dashboard_refresh.get_es_rag_gateway",
            return_value=gateway,
        ):
            rows = dashboard_refresh._load_user_input_chunks()

        self.assertEqual(1, len(rows))
        self.assertEqual("MS", rows[0]["broker"])
        gateway.sources.assert_called_once_with(
            doc_type="user_input", theme="ai", limit=200
        )

    def test_background_refresh_failure_reaches_terminal_state(self) -> None:
        dashboard_refresh._jobs["job-test"] = {
            "id": "job-test", "status": "running", "failed": 0
        }
        with patch.object(
            dashboard_refresh,
            "_run_job",
            new=AsyncMock(side_effect=EsRagTransportError(
                "private upstream detail", operation="sources"
            )),
        ):
            asyncio.run(dashboard_refresh._run_job_guarded(
                "job-test", ["metric"], ["MS"], "yearly"
            ))

        job = dashboard_refresh._jobs["job-test"]
        self.assertEqual("failed", job["status"])
        self.assertEqual("sources", job["errors"][0]["operation"])
        self.assertNotIn("private upstream detail", str(job))


if __name__ == "__main__":
    unittest.main()
