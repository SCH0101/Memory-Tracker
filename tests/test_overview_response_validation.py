from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from backend.api.routes import overview


class OverviewResponseValidationTests(unittest.TestCase):
    def test_latest_arete_source_prefers_recent_memory_report(self) -> None:
        class FakeGateway:
            def sources(self, **_kwargs):
                return {
                    "sources": [
                        {"broker": "Arete", "date": "2026-09-18", "filename": "20260918 Arete Sony Music.pdf"},
                        {"broker": "Arete", "date": "2026-09-17", "filename": "20260917 Arete Semis & Hardware Real-Time Views__bf546c9e4c06.pdf"},
                        {"broker": "Arete", "date": "2026-09-17", "filename": "20260917 Arete Semis & Hardware Real-Time Views.pdf"},
                        {"broker": "Arete", "date": "2026-09-14", "filename": "20260914 Arete AI Hardware.pdf"},
                    ]
                }

        with patch(
            "backend.services.es_rag_gateway.get_es_rag_gateway",
            return_value=FakeGateway(),
        ):
            source = asyncio.run(overview._latest_arete_source())

        self.assertEqual(
            "20260917 Arete Semis & Hardware Real-Time Views.pdf",
            source["filename"],
        )

    def test_arete_gather_scopes_topics_to_latest_source_filename(self) -> None:
        today = overview.date.today().isoformat()
        latest_filename = "20260917 Arete Semis & Hardware Real-Time Views.pdf"

        async def retrieval(_query: str, **kwargs):
            scoped = (kwargs.get("metadata_filters") or {}).get("filename", {}).get("$eq")
            filename = latest_filename if scoped == latest_filename else "20260908 Arete ASML Fire Risk.pdf"
            return [{
                "text": "pricing inventory DIO demand supply HBM wafer capacity capex strategy " * 3,
                "filename": filename,
                "page": 1,
                "broker": "Arete",
                "date": today,
                "date_int": int(today.replace("-", "")),
                "content_type": "narrative",
                "score": 1.0,
            }]

        with (
            patch.object(overview, "_latest_arete_source", new=AsyncMock(return_value={"filename": latest_filename, "date": "2026-09-17", "broker": "Arete"})),
            patch("backend.services.fact_pool.retrieve_for_section", new=AsyncMock(side_effect=retrieval)),
            patch.object(overview, "get_retrieval_settings", return_value={"chunks_get": 10, "rerank_keep": 4}),
            patch.object(overview, "get_tab_sources", return_value=["broker_research", "industry_report"]),
        ):
            context, source_index, _ = asyncio.run(overview._gather_arete_context_with_sources(1))

        self.assertGreater(len(source_index), 0)
        self.assertEqual({latest_filename}, {row["filename"] for row in source_index.values()})
        self.assertIn(f"Latest detected Arete source: {latest_filename}", context)

    def test_arete_gather_avoids_es_hard_filter_tokens_in_semantic_query(self) -> None:
        today = overview.date.today().isoformat()
        arete_hit = {
            "text": "pricing inventory DIO demand growth bit supply HBM wafer capacity capex strategy " * 3,
            "filename": "latest-arete.pdf",
            "page": 1,
            "broker": "Arete",
            "date": today,
            "date_int": int(today.replace("-", "")),
            "content_type": "text",
            "score": 1.0,
        }

        async def es_parser_sensitive_retrieval(query: str, **_kwargs):
            lowered = query.lower()
            if (
                "published since" in lowered
                or "broker research" in lowered
                or "arete research" in lowered
            ):
                return []
            return [arete_hit]

        with (
            patch.object(overview, "_latest_arete_source", new=AsyncMock(return_value={"filename": "latest-arete.pdf", "date": today, "broker": "Arete"})),
            patch("backend.services.fact_pool.retrieve_for_section", new=AsyncMock(side_effect=es_parser_sensitive_retrieval)),
            patch.object(overview, "get_retrieval_settings", return_value={"chunks_get": 10, "rerank_keep": 4}),
            patch.object(overview, "get_tab_sources", return_value=["broker_research", "industry_report"]),
        ):
            context, source_index, _ = asyncio.run(overview._gather_arete_context_with_sources(1))

        self.assertGreater(len(source_index), 0)
        self.assertIn("latest-arete.pdf", context)

    def test_local_recency_filter_still_rejects_old_chunks(self) -> None:
        old_date = (overview.date.today() - overview.timedelta(days=21)).isoformat()
        recent_date = (overview.date.today() - overview.timedelta(days=2)).isoformat()
        rows = [
            {"text": "old", "filename": "old.pdf", "broker": "Arete", "date": old_date},
            {"text": "recent", "filename": "recent.pdf", "broker": "Arete", "date": recent_date},
        ]

        result = overview._recent_only(rows, top_n=10)

        self.assertEqual(["recent.pdf"], [row["filename"] for row in result])

    def test_empty_section_uses_closest_evidence_within_forty_five_days(self) -> None:
        fallback_date = (overview.date.today() - overview.timedelta(days=27)).isoformat()
        too_old_date = (overview.date.today() - overview.timedelta(days=46)).isoformat()
        rows = [
            {"text": "too old", "filename": "too-old.pdf", "broker": "Broker", "date": too_old_date},
            {"text": "closest", "filename": "closest.pdf", "broker": "Broker", "date": fallback_date},
        ]

        result = overview._recent_or_closest(rows, top_n=4)

        self.assertEqual(["closest.pdf"], [row["filename"] for row in result])

    def test_arete_topic_signal_rejects_unrelated_chunks(self) -> None:
        unrelated = {"text": "ASML factory fire legislation and export controls " * 4}
        relevant = {"text": "HBM4 AI memory demand and stack capacity outlook " * 4}

        self.assertFalse(overview._topic_signal(unrelated, "HBM / AI Memory"))
        self.assertTrue(overview._topic_signal(relevant, "HBM / AI Memory"))

    def test_industry_normalizer_makes_summaries_bullets_and_drops_uncited_claims(self) -> None:
        generated = {
            "title": "Memory Industry Overview",
            "overall_bullets": [
                "Cited executive point [src:S1]",
                "Uncited executive point",
            ],
            "sections": [
                {
                    "group": "Demand",
                    "topic": "Inventory",
                    "summary": "Inventory remains lean [src:S1]",
                    "themes": [
                        {
                            "title": "Channel",
                            "bullets": ["Cited detail [src:S1]", "Uncited detail"],
                        }
                    ],
                },
                {
                    "group": "Demand",
                    "topic": "Spot Pricing",
                    "summary": "N/A - no relevant data in current chart pool",
                    "themes": [],
                },
            ],
        }

        result = overview._prepare_overview_result(generated, view="industry")

        self.assertEqual(["Cited executive point [src:S1]"], result["overall_bullets"])
        self.assertEqual(
            ["Inventory remains lean [src:S1]"],
            result["sections"][0]["summary_bullets"],
        )
        self.assertEqual(
            ["Cited detail [src:S1]"],
            result["sections"][0]["themes"][0]["bullets"],
        )
        self.assertEqual(
            ["No recent evidence retrieved."],
            result["sections"][1]["summary_bullets"],
        )

    def test_company_normalizer_makes_summary_and_views_bullet_only(self) -> None:
        generated = {
            "title": "Memory Company Overview",
            "companies": [
                {
                    "name": "SK Hynix",
                    "summary": "HBM leadership continues [src:S1]",
                    "segments": [
                        {
                            "topic": "HBM",
                            "views": [
                                {
                                    "broker": "Broker",
                                    "bullets": ["Cited view [src:S1]", "Uncited view"],
                                }
                            ],
                        }
                    ],
                    "strategic_updates": ["Cited update [src:S2]", "Uncited update"],
                }
            ],
        }

        result = overview._prepare_overview_result(generated, view="company")

        self.assertEqual(
            ["HBM leadership continues [src:S1]"],
            result["companies"][0]["summary_bullets"],
        )
        self.assertEqual(
            ["Cited view [src:S1]"],
            result["companies"][0]["segments"][0]["views"][0]["bullets"],
        )
        self.assertEqual(
            ["Cited update [src:S2]"],
            result["companies"][0]["strategic_updates"],
        )

    def test_arete_sanitizer_removes_placeholder_cards_without_arete_evidence(self) -> None:
        result = {
            "arete_latest": {
                "title": "ARETE Latest PDF Summary",
                "source": "N/A - no Arete evidence retrieved",
                "cards": [
                    {"topic": "Pricing", "bullets": ["N/A - no Arete evidence retrieved"]},
                    {"topic": "Inventory", "bullets": ["Other broker view [src:S1]"]},
                ],
            }
        }

        overview._sanitize_arete_latest_citations(
            result,
            {"S1": {"broker": "UBS", "filename": "ubs.pdf"}},
            {},
        )

        self.assertEqual([], result["arete_latest"]["cards"])
        self.assertEqual(
            ["No recent Arete evidence retrieved."],
            result["arete_latest"]["unavailable_bullets"],
        )


    def test_section_retrieval_keeps_four_chunks_per_section(self) -> None:
        today = overview.date.today().isoformat()
        hits = [
            {
                "text": f"evidence {i}",
                "filename": f"source-{i}.pdf",
                "page": i + 1,
                "broker": "Broker",
                "date": today,
                "content_type": "text",
            }
            for i in range(10)
        ]

        with (
            patch.object(overview, "_effective_section_queries", return_value={"Demand": "demand", "Supply": "supply"}),
            patch.object(overview, "_effective_section_sources", return_value={"Demand": ["broker_research"], "Supply": ["broker_research"]}),
            patch.object(overview, "get_retrieval_settings", return_value={"chunks_get": 10, "rerank_keep": 4}),
            patch("backend.services.fact_pool.retrieve_for_section", new=AsyncMock(return_value=hits)),
        ):
            context, source_index = asyncio.run(overview._gather_retrieved_context_with_sources())

        self.assertEqual(8, len(source_index))
        self.assertEqual(8, sum(context.count(f"[src:{source_id}]") for source_id in source_index))

    def test_industry_normalizer_unwraps_and_flattens_keyed_sections(self) -> None:
        generated = {
            "industry_overview": {
                "title": "Memory Industry Overview",
                "overall_summary": "N/A",
                "sections": {
                    "HBM Demand & Capacity": {
                        "group": "Demand",
                        "summary": "HBM remains constrained [src:S1]",
                        "themes": [],
                    },
                    "Bit Supply": {
                        "group": "Supply",
                        "summary": "Supply remains disciplined [src:S2]",
                        "themes": [],
                    },
                },
            }
        }

        result = overview._prepare_overview_result(generated, view="industry")

        self.assertNotIn("error", result)
        self.assertEqual(2, len(result["sections"]))
        self.assertEqual("HBM Demand & Capacity", result["sections"][0]["topic"])
        self.assertEqual(
            ["HBM remains constrained [src:S1]", "Supply remains disciplined [src:S2]"],
            result["overall_bullets"],
        )

    def test_company_normalizer_unwraps_and_flattens_keyed_companies(self) -> None:
        generated = {
            "company_overview": {
                "title": "Memory Company Overview",
                "companies": {
                    "SK Hynix": {"summary": "HBM leadership continues [src:S1]", "segments": []},
                    "Micron": {"summary": "Capacity is expanding [src:S2]", "segments": []},
                },
            }
        }

        result = overview._prepare_overview_result(generated, view="company")

        self.assertNotIn("error", result)
        self.assertEqual(2, len(result["companies"]))
        self.assertEqual("SK Hynix", result["companies"][0]["name"])
        self.assertEqual(
            ["HBM leadership continues [src:S1]", "Capacity is expanding [src:S2]"],
            result["overall_bullets"],
        )





if __name__ == "__main__":
    unittest.main()
