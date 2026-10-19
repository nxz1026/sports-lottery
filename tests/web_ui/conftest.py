"""Playwright pytest fixtures for dashboard browser tests.

Skips the entire module if playwright / pytest-playwright is not installed
so the rest of the test suite still passes on dev machines without browsers.

Run locally:
    pip install playwright pytest-playwright
    playwright install chromium

Then either:
    pytest tests/web_ui/ -v
or with the bundled CLI:
    pytest tests/web_ui/ -v --browser chromium
"""
from __future__ import annotations

import importlib
import socket
import threading
from contextlib import closing
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
STATIC_DIR = REPO_ROOT / "static"


def _try_import_playwright():
    """Return playwright.sync_api or None; never raises."""
    try:
        importlib.import_module("playwright")
        return importlib.import_module("playwright.sync_api")
    except ImportError:
        return None


SYNC_API = _try_import_playwright()
PLAYWRIGHT_OK = SYNC_API is not None


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _SilentHandler(SimpleHTTPRequestHandler):
    """Static handler that swallows access logs (test output stays clean)."""

    def log_message(self, fmt, *args):  # noqa: D401 - signature mandated
        return


def _start_server(directory: Path) -> HTTPServer:
    handler = lambda *a, **kw: _SilentHandler(*a, directory=str(directory), **kw)
    server = HTTPServer(("127.0.0.1", _free_port()), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


@pytest.fixture(scope="session")
def dashboard_url() -> str:
    """Serve static/ on a background HTTP server for the test session.

    Returns the base URL (without trailing slash). Tests can join with
    paths like f"{dashboard_url}/dashboard.html".
    """
    if not PLAYWRIGHT_OK:
        pytest.skip("playwright not installed (pip install playwright; playwright install chromium)")
    if not STATIC_DIR.exists():
        pytest.skip(f"static dir not found at {STATIC_DIR}")
    server = _start_server(STATIC_DIR)
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()


@pytest.fixture(scope="session")
def browser():
    """Yield a Chromium browser; skip cleanly if the binary is missing."""
    if not PLAYWRIGHT_OK:
        pytest.skip("playwright not installed (pip install playwright; playwright install chromium)")
    try:
        with SYNC_API.sync_playwright() as p:
            b = p.chromium.launch(headless=True)
            try:
                yield b
            finally:
                b.close()
    except Exception as exc:  # noqa: BLE001 - playwright raises broad on missing driver
        pytest.skip(f"playwright chromium unavailable: {exc.__class__.__name__}: {exc}")


@pytest.fixture()
def page(browser, dashboard_url):
    """Fresh isolated page per test; clears localStorage so tests don't leak."""
    ctx = browser.new_context()
    page = ctx.new_page()
    try:
        page.goto(f"{dashboard_url}/dashboard.html", wait_until="domcontentloaded")
        # Stub network so JS doesn't blow up trying to reach a real API.
        page.route("**/api/**", lambda route: route.fulfill(status=503, body="{}"))
        yield page
    finally:
        ctx.close()
