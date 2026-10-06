from __future__ import annotations

import asyncio
import unittest

from backend.api.server import _serve_index, spa_entry


class FrontendServingTests(unittest.TestCase):
    def test_missing_frontend_asset_returns_404_instead_of_spa_html(self) -> None:
        response = asyncio.run(spa_entry("assets/definitely-missing.js"))

        self.assertEqual(404, response.status_code)
        self.assertEqual("no-store", response.headers.get("cache-control"))
        self.assertNotEqual("text/html; charset=utf-8", response.headers.get("content-type"))

    def test_spa_index_disables_browser_caching(self) -> None:
        response = _serve_index()

        self.assertEqual("no-store", response.headers.get("cache-control"))


if __name__ == "__main__":
    unittest.main()
