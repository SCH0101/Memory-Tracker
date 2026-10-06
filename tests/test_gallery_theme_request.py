from urllib.parse import urlparse, parse_qs
from backend.services.es_rag_gateway import EsRagGateway


def test_inventory_sends_theme_on_catalogue_and_pages(monkeypatch):
    gateway = EsRagGateway('http://example.test', 'test-key')
    paths = []
    def request(method, path, **kwargs):
        paths.append(path)
        return {'groups': [{'group': 'semiconductors', 'count': 1,
                            'charts': [{'chart_file': 'memory.jpg'}]}]}
    monkeypatch.setattr(gateway, '_request_json', request)
    result = gateway.chart_inventory(theme='ai')
    assert len(result['hits']) == 1
    assert len(paths) == 2
    assert all(parse_qs(urlparse(path).query).get('theme') == ['ai'] for path in paths)
