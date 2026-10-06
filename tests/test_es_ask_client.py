from __future__ import annotations

import asyncio
import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from backend.api.routes.chat_bridge import _ask_es


class _AskHandler(BaseHTTPRequestHandler):
    request_json: dict[str, object] = {}
    request_path = ""
    request_api_key = ""

    def do_GET(self) -> None:  # noqa: N802 - stdlib HTTP handler API
        type(self).request_path = self.path
        type(self).request_api_key = self.headers.get("X-RAG-Ingest-Key", "")
        body = json.dumps({"contract_version": "2026-09-02"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - stdlib HTTP handler API
        length = int(self.headers.get("Content-Length", "0"))
        type(self).request_json = json.loads(self.rfile.read(length))
        type(self).request_path = self.path
        type(self).request_api_key = self.headers.get("X-RAG-Ingest-Key", "")

        body = (
            'event: token\n'
            'data: {"token":"draft"}\n\n'
            'event: done\n'
            'data: {"content":"**Final ES answer**"}\n\n'
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *args: object) -> None:
        return


class EsAskClientTests(unittest.TestCase):
    def test_ask_uses_configured_endpoint_and_fixed_ai_theme(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _AskHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"

        try:
            with patch.dict(
                os.environ,
                {
                    "ESRAG_API_URL": base_url,
                    "ES_RAG_API_KEY": "test-key",
                    "MEMORY_CHAT_ES_PROJECT": "memory-project",
                    "MEMORY_CHAT_ES_TICKER": "NVDA",
                    "MEMORY_CHAT_ES_CONTEXT_LABEL": "Memory",
                    "MEMORY_CHAT_ES_THEME": "not-ai",
                },
                clear=False,
            ):
                try:
                    answer = asyncio.run(
                        _ask_es(
                            "What changed?",
                            "session-123",
                            filters={"doc_type": "sellside"},
                            metadata_filters={"year": 2026},
                        )
                    )
                except Exception as exc:  # Make the legacy dependency a clear red assertion.
                    answer = f"ERROR: {exc}"
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual("**Final ES answer**", answer)
        self.assertEqual("/api/rag/ask", _AskHandler.request_path)
        self.assertEqual("test-key", _AskHandler.request_api_key)
        self.assertEqual(
            {
                "message": "What changed?",
                "session_id": "session-123",
                "project": "memory-project",
                "ticker": "NVDA",
                "context_label": "Memory",
                "filters": {"doc_type": "sellside"},
                "metadata_filters": {"year": 2026},
                "theme": "ai",
            },
            _AskHandler.request_json,
        )


if __name__ == "__main__":
    unittest.main()
