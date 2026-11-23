"""Real-Chrome integration tests for pixcell.browser (skipped unless PIXCELL_REAL_BROWSER=1)."""

from __future__ import annotations

import http.server
import os
import socketserver
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from PIL import Image as PILImage

import pixcell.textual as pixcell_textual
from pixcell.browser import Browser, BrowserError, BrowserView, find_browser, open_html
from pixcell.browser.policy import navigation_gate
from pixcell.browser.view import CloseButton
from pixcell.docs.types import HTML
from pixcell.live import LiveImage
from pixcell.probe import Capabilities

pytestmark = pytest.mark.skipif(
    os.environ.get("PIXCELL_REAL_BROWSER") != "1",
    reason="set PIXCELL_REAL_BROWSER=1 to run real-Chrome integration tests",
)


@pytest.fixture
def chrome_executable() -> str:
    found = find_browser()
    if found is None:
        pytest.skip("no Chrome, Chromium, Edge or Brave installed")
    return found


def _settle(condition, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def test_headless_browser_eval_and_screenshot(chrome_executable: str, tmp_path: Path):
    page = tmp_path / "hello.html"
    page.write_text(
        "<!DOCTYPE html><html><head><title>Real Page</title></head>"
        "<body style='margin:0;background:#113355;color:#fff'><h1>Hello</h1></body></html>",
        "utf-8",
    )
    with Browser(chrome_executable, size=(320, 200)) as browser:
        browser.navigate(page.as_uri())
        assert browser.eval("document.title") == "Real Page"
        image = browser.screenshot()
        assert isinstance(image, PILImage.Image) and image.size == (320, 200)
        png = browser.screenshot(format="png")
        assert png.startswith(b"\x89PNG")


def test_open_html_renders_docs_html_offline(chrome_executable: str):
    doc = HTML(
        html="<!DOCTYPE html><html><head><title>Offline Doc</title></head>"
        "<body><p id='msg'>Rendered without network</p></body></html>",
        title="Offline Doc",
    )
    with open_html(doc, executable=chrome_executable, size=(400, 240)) as browser:
        assert browser.eval("document.getElementById('msg').textContent") == "Rendered without network"
        shot = browser.screenshot()
        assert shot.size == (400, 240)
        with pytest.raises(BrowserError):
            browser.navigate("https://example.com")


async def test_the_view_shows_a_page_and_closes(chrome_executable: str, tmp_path: Path):
    from textual.app import App, ComposeResult

    page = tmp_path / "page.html"
    page.write_text("<h1>A page</h1>", "utf-8")
    pixcell_textual.configure(Capabilities("kitty", (8, 16), terminal="rex"))

    class One(App):
        CSS = "BrowserView > LiveImage { width: 40; height: 10; }"

        def compose(self) -> ComposeResult:
            yield BrowserView(page.as_uri(), executable=chrome_executable)

    try:
        async with One().run_test(size=(80, 30)) as pilot:
            shown = pilot.app.query_one(BrowserView)
            for _ in range(300):
                if shown.started is not None and shown.started.done():
                    break
                await pilot.pause(0.1)
            assert shown.started.result()[0], shown.started.result()
            live = pilot.app.query_one(LiveImage)
            for _ in range(150):
                if live.frames_shown > 0:
                    break
                await pilot.pause(0.1)
            assert live.frames_shown > 0
            await pilot.click(CloseButton)
            for _ in range(100):
                if not pilot.app.query(BrowserView):
                    break
                await pilot.pause(0.1)
            assert not pilot.app.query(BrowserView)
    finally:
        pixcell_textual.configure(Capabilities("halfblock"))


@pytest.fixture
def local_site():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            query = parse_qs(urlparse(self.path).query)
            if self.path.startswith("/to"):
                self.send_response(302)
                self.send_header("Location", query["u"][0])
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(f"<title>{self.path}</title><h1>{self.path}</h1>".encode())

        def log_message(self, *args):
            pass

    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_gated_page_blocks_disallowed_redirects_and_watches_fields(chrome_executable: str, tmp_path: Path, local_site: str):
    page = tmp_path / "report.html"
    page.write_text(
        "<input type=password autofocus>"
        f"<script>setTimeout(() => {{ location = '{local_site}/to?u=' + encodeURIComponent('https://example.com/') }}, 250)</script>",
        "utf-8",
    )
    notes: list[str] = []
    fields: list[str] = []
    allow = navigation_gate(page.as_uri(), lambda: "ask", set(), tmp_path)
    running = Browser(
        chrome_executable,
        on_frame=lambda image: None,
        on_note=notes.append,
        allow=allow,
        on_field=fields.append,
    )
    running.start(page.as_uri(), (400, 300))
    try:
        assert _settle(lambda: "password" in fields), fields
        assert _settle(lambda: bool(notes)), "redirect to external site was not refused"
        assert "didn't open" in notes[0]
        assert "example.com" not in running.url
    finally:
        running.stop()


def test_can_scroll_detects_boundaries_and_listeners(chrome_executable: str, tmp_path: Path):
    tall = "".join(f"<p style='height:40px;margin:0'>row {i}</p>" for i in range(60))
    plain = tmp_path / "plain.html"
    plain.write_text(f"<body style='margin:0'>{tall}</body>", "utf-8")
    wheel = tmp_path / "wheel.html"
    wheel.write_text(
        "<body style='margin:0;height:100vh;overflow:hidden'><div id=m style='height:100%'>map</div>"
        "<script>m.addEventListener('wheel', e => e.preventDefault(), {passive: false})</script></body>",
        "utf-8",
    )
    with Browser(chrome_executable, size=(800, 600)) as browser:
        browser.navigate(plain.as_uri())
        assert browser.can_scroll(400, 300, -60) is False
        assert browser.can_scroll(400, 300, 60) is True
        browser.navigate(wheel.as_uri())
        assert browser.can_scroll(400, 300, -60) is True
