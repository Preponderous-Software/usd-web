"""Headless-browser smoke test: the demo page loads, draws every tile, and hot-swaps a tile
that is republished while it is open.

Run with ``pytest -m browser`` after ``pip install playwright`` and
``python -m playwright install chromium``. ``USD_WEB_CHROMIUM`` may point at an existing
Chromium executable instead. WebGL comes from SwiftShader (software), so no GPU is needed.
"""

from __future__ import annotations

import functools
import http.server
import os
import shutil
import threading

import pytest

pytestmark = pytest.mark.browser
playwright = pytest.importorskip("playwright.sync_api")

ARGS = ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture
def site(tmp_path, root):
    from make_demo import make_demo

    for name in ("index.html", "usd-web-viewer.js"):
        shutil.copy(root / "viewer" / name, tmp_path / name)
    make_demo(tmp_path / "demo")
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(tmp_path)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield tmp_path, f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()


def test_viewer_loads_and_hot_swaps(site):
    from make_demo import make_demo

    directory, url = site
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("USD_WEB_CHROMIUM") or None, args=ARGS)
        page = browser.new_page(viewport={"width": 900, "height": 600})
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url + "index.html?poll=0.5")
        page.wait_for_function("window.__usdWeb && window.__usdWeb.ready", timeout=60_000)
        state = page.evaluate("({...window.__usdWeb, render: undefined, poll: undefined})")
        assert state["tiles"] == 4
        assert state["revision"] == 1
        assert state["frames"] >= 1
        assert state["labels"] == 3  # two region labels + one explicit label
        assert state["sprites"] > 0
        assert state["errors"] == []
        assert page.locator("#panel .uw-entry").count() == 2
        assert page.locator("#panel .uw-title").text_content() == "usd-web demo"
        before = state["versions"]

        # Something was drawn: the canvas is not a single flat colour.
        distinct = page.evaluate(
            """() => { const c = document.getElementById('view');
                const g = c.getContext('webgl2') || c.getContext('webgl');
                const px = new Uint8Array(4 * 64 * 64);
                g.readPixels(Math.floor(c.width / 2) - 32, Math.floor(c.height / 2) - 32, 64, 64, g.RGBA, g.UNSIGNED_BYTE, px);
                const s = new Set(); for (let i = 0; i < px.length; i += 4) s.add(px[i] + ',' + px[i+1] + ',' + px[i+2]);
                return s.size; }"""
        )
        assert distinct > 4

        # Republish with one tile changed; the poller must swap exactly that tile.
        doc = make_demo(directory / "demo", bump=1)
        page.wait_for_function("window.__usdWeb.revision === 2 && window.__usdWeb.swaps.length > 0", timeout=30_000)
        after = page.evaluate("window.__usdWeb")
        changed = [n for n in before if before[n] != doc["tiles"][n]["version"]]
        assert changed == ["t.0.0"]
        assert [s["tile"] for s in after["swaps"]] == ["t.0.0"]
        assert after["versions"]["t.0.0"] == doc["tiles"]["t.0.0"]["version"]
        assert after["tiles"] == 4

        # Legend fly-to and a layer toggle run without errors.
        page.locator("#panel .uw-entry").first.click()
        page.locator("#panel input[type=checkbox]").first.uncheck()
        page.wait_for_timeout(1200)
        assert page.evaluate("window.__usdWeb.errors") == []
        assert errors == []
        browser.close()
