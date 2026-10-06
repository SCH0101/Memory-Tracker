import unittest
from unittest.mock import patch
from backend.services.es_rag_gateway import EsRagGateway, EsRagContractError


class CurrentContractTests(unittest.TestCase):
    def test_citations_use_public_es_origin(self):
        import os
        from backend.api.routes.chat_bridge import _qualify_es_markdown_links
        with patch.dict(os.environ,{'ESRAG_PUBLIC_URL':'https://research.example.test'}):
            self.assertEqual(_qualify_es_markdown_links('[source](/api/research-documents/doc/pages/1)','http://localhost:8080'),
                '[source](https://research.example.test/api/research-documents/doc/pages/1)')

    def test_ask_uses_current_json_contract_and_enforced_ai(self):
        gateway=EsRagGateway('http://127.0.0.1:8080','test-key')
        def request(method,path,**kwargs):
            if path.endswith('/capabilities'):
                return {'contract':'equity_swarm_external_capabilities_v1','operations':['answer']}
            self.assertEqual(path,'/api/external/answer')
            self.assertEqual(kwargs['payload']['filters'],{'themes':['ai'],'date_from':'2026-09-01'})
            self.assertFalse(kwargs['payload']['allow_web'])
            self.assertNotIn('session_id',kwargs['payload'])
            return {'contract':'equity_swarm_external_answer_v1','answer_markdown':'Answer',
                    'applied_filters':{'themes':['ai'],'date_from':'2026-09-01'}}
        with patch.object(gateway,'_request_json',side_effect=request):
            self.assertEqual(gateway.ask('Question',filters={'date_from':'2026-09-01'},theme='ai'),'Answer')

    def test_unsupported_filter_does_not_get_ignored(self):
        from backend.services.es_research_filters import memory_filters
        with self.assertRaises(EsRagContractError):
            memory_filters({'filename':'x.pdf'},None)

    def test_legacy_year_and_source_filters_translate(self):
        from backend.services.es_research_filters import memory_filters
        self.assertEqual(memory_filters({'doc_type':'sellside'},{'year':2026}),
            {'themes':['ai'],'document_types':['initiation','earnings_preview','earnings_review','company_update','primer','conference_notes'],
             'date_from':'2026-01-01','date_to':'2026-12-31'})
