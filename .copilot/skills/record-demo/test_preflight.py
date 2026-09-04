#!/usr/bin/env python3
"""Tests for the record-demo HTTP asset preflight."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import TestCase

from preflight import main


class DemoHandler(BaseHTTPRequestHandler):
    """Serve a complete page and one deliberately missing asset."""

    def do_GET(self) -> None:
        responses = {
            "/": (
                200,
                b'<link rel="stylesheet" href="/app.css"><script src="/app.js"></script>'
                b'<img src="/avatar.png"><main>Demo ready</main>',
            ),
            "/app.css": (200, b"body { color: black; }"),
            "/app.js": (200, b"document.body.dataset.ready = 'true'"),
            "/avatar.png": (200, b"not-a-real-png-but-non-empty"),
            "/broken": (
                200,
                b'<link rel="stylesheet" href="/missing.css"><script src="/app.js"></script>',
            ),
        }
        status, body = responses.get(self.path, (404, b"missing"))
        self.send_response(status)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class PreflightTest(TestCase):
    """Verify observable pass and failure outcomes through the CLI entry point."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), DemoHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_passes_for_complete_page(self) -> None:
        result = main(
            [
                "--url",
                f"{self.base_url}/",
                "--expect-text",
                "Demo ready",
                "--require-frontend",
                "--require-image",
            ]
        )

        self.assertEqual(0, result)

    def test_fails_when_required_asset_is_missing(self) -> None:
        result = main(
            ["--url", f"{self.base_url}/broken", "--require-frontend"]
        )

        self.assertEqual(1, result)
