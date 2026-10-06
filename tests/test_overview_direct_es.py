import asyncio
import unittest
from unittest.mock import AsyncMock, patch
from backend.api.routes import overview


class DirectOverviewTests(unittest.TestCase):
    def test_cache_reuses_answer_and_failed_refresh_does_not_overwrite(self):
        from backend.services.es_rag_gateway import EsRagContractError
        from unittest.mock import Mock
        gateway=Mock(base_url='https://es.example.test')
        gateway.ask.return_value='## Summary\nCited answer'
        overview._cache.clear()
        with (patch('backend.services.es_rag_gateway.get_es_rag_gateway',return_value=gateway),
              patch.object(overview,'_ensure_cache_table',new=AsyncMock()),
              patch.object(overview,'_load_db_cache',new=AsyncMock(return_value=None)),
              patch.object(overview,'_save_db_cache',new=AsyncMock()) as save):
            first=asyncio.run(overview.get_industry_overview(refresh=True))
            gateway.ask.side_effect=AssertionError('cached answer should not regenerate')
            self.assertEqual(first,asyncio.run(overview.get_industry_overview()))
            gateway.ask.side_effect=None; gateway.ask.return_value=''
            with self.assertRaises(EsRagContractError):
                asyncio.run(overview.get_industry_overview(refresh=True))
            self.assertEqual(save.await_count,1)
            self.assertEqual(first,asyncio.run(overview.get_industry_overview()))

    def test_both_views_use_es_without_local_generation(self):
        for view in ('industry','company'):
            class Gateway:
                def ask(self,question,**kwargs):
                    assert kwargs['filters']['date_from']
                    assert kwargs['filters']['date_to']
                    assert kwargs['theme']=='ai'
                    return '## Pricing\n- Supported claim [source](/api/research-documents/d/pages/1)'
                base_url='https://es.example.test'
            with (patch('backend.services.es_rag_gateway.get_es_rag_gateway',return_value=Gateway()),
                  patch.object(overview,'_ensure_cache_table',new=AsyncMock()),
                  patch.object(overview,'_save_db_cache',new=AsyncMock()),
                  patch.object(overview,'_gather_overview_inputs',side_effect=AssertionError('retired retrieval invoked')),
                  patch.object(overview,'_call_llm',side_effect=AssertionError('second answering model invoked'))):
                result=asyncio.run(getattr(overview,'get_'+view+'_overview')(refresh=True))
                self.assertIn('Supported claim',result['answer_markdown'])
                self.assertEqual(result['_answer_provider'],'ES')
