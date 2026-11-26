"""Pruebas del descubrimiento de favicons externos."""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, external, external_icons  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\nexample"


class IconTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.configure(str(Path(self.tmp.name) / "portal.db"))
        db.init_db()
        with db.db() as conn:
            conn.execute(
                "INSERT INTO external_projects (name, company, url) VALUES (?, ?, ?)",
                ("Ejemplo", "Empresa", "https://example.test/app/"),
            )

    def tearDown(self):
        db.configure(None)
        self.tmp.cleanup()

    def test_discovers_relative_icon_and_serves_saved_image(self):
        def respond(request):
            if request.url.path == "/app/":
                return httpx.Response(200, headers={"content-type": "text/html"},
                                      text='<link rel="shortcut icon" href="img/logo.png">')
            self.assertEqual(request.url.path, "/app/img/logo.png")
            return httpx.Response(200, content=PNG)

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                data, mime = await external_icons.discover(client, external.projects()[0])
                self.assertEqual((data, mime), (PNG, "image/png"))
                with patch.object(external_icons, "discover", return_value=(data, mime)):
                    await external_icons.refresh(external.projects(), set())

        asyncio.run(run())
        self.assertEqual(external.icon(1)["icon_data"], PNG)
        with db.db() as conn:
            conn.execute("UPDATE external_projects SET url = 'https://new.test/' WHERE id = 1")
        self.assertIsNone(external.icon(1))

    def test_falls_back_to_root_and_rejects_cross_domain(self):
        requests = []

        def respond(request):
            requests.append(str(request.url))
            if request.url.path == "/app/":
                return httpx.Response(200, headers={"content-type": "text/html"},
                                      text='<link rel="icon" href="https://other.test/logo.png">')
            return httpx.Response(200, content=PNG)

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                return await external_icons.discover(client, external.projects()[0])

        self.assertEqual(asyncio.run(run()), (PNG, "image/png"))
        self.assertEqual(requests, ["https://example.test/app/", "https://example.test/favicon.ico"])

    def test_discovers_apple_touch_icon_in_large_page(self):
        def respond(request):
            if request.url.path == "/app/":
                return httpx.Response(
                    200, headers={"content-type": "text/html"},
                    content=b'<link rel="apple-touch-icon" href="/touch.png">' + b"x" * (external_icons.MAX_HTML + 1),
                )
            self.assertEqual(request.url.path, "/touch.png")
            return httpx.Response(200, content=PNG)

        async def run():
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                return await external_icons.discover(client, external.projects()[0])

        self.assertEqual(asyncio.run(run()), (PNG, "image/png"))

    def test_failed_refresh_retains_previous_icon_and_local_icon_skips_search(self):
        with db.db() as conn:
            conn.execute(
                "UPDATE external_projects SET icon_data = ?, icon_type = ?, icon_checked_url = url, local_slug = 'local'",
                (PNG, "image/png"),
            )
        with patch.object(external_icons, "discover", return_value=(None, None)) as find:
            asyncio.run(external_icons.refresh(external.projects(), {"local"}))
            find.assert_not_called()
            asyncio.run(external_icons.refresh(external.projects(), set()))
            find.assert_awaited_once()
            asyncio.run(external_icons.refresh(external.projects(), set()))
            find.assert_awaited_once()
        self.assertEqual(external.icon(1)["icon_data"], PNG)


if __name__ == "__main__":
    unittest.main()
