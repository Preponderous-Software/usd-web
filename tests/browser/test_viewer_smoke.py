"""Headless-browser smoke test: the demo page loads, draws every tile, hot-swaps a tile
that is republished while it is open, and the viewer's public methods (exposed by the page as
``window.usdWebViewer``) behave.

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
# The camera target's X and Z and the distance have reached [x, z, dist] (a fly-to finished).
TARGET_IS = """([x, z, d]) => { const s = window.usdWebViewer.state;
    return Math.abs(s.target[0] - x) < 1e-6 && Math.abs(s.target[2] - z) < 1e-6 && Math.abs(s.dist - d) < 1e-6; }"""
LABELS = "[...document.querySelectorAll('#labels .uw-label')]"


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
    from make_demo import heightmap, make_demo

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

        # heightAt reads the swapped tile: the demo heights with the plateau, null outside the world.
        heights = heightmap(bump=1)[0]
        for x, z in [(16, 16), (40, 100), (100, 40), (127, 127)]:
            assert page.evaluate(f"window.usdWebViewer.heightAt({x}, {z})") == int(heights[z, x])
        assert page.evaluate("window.usdWebViewer.heightAt(-1, 0)") is None
        assert page.evaluate("window.usdWebViewer.heightAt(0, 128)") is None

        # Legend fly-to: "North field" (cells 1..2 x 1..2 of 16) centres on (32, 32) at
        # max(60, sqrt(4) * 16 * 3.2) = 102.4.
        page.locator("#panel .uw-entry").first.click()
        page.wait_for_function(TARGET_IS, arg=[32, 32, 102.4], timeout=10_000)

        # flyTo from the module API.
        page.evaluate("window.usdWebViewer.flyTo(20, 6, 90, 150)")
        page.wait_for_function(TARGET_IS, arg=[20, 90, 150], timeout=10_000)

        # The layer checkbox and setLayerVisible both drop and restore the layer's two labels.
        page.locator("#panel input[type=checkbox]").first.uncheck()
        page.wait_for_function("window.__usdWeb.labels === 1", timeout=10_000)
        page.evaluate("window.usdWebViewer.setLayerVisible('regions', true)")
        page.wait_for_function("window.__usdWeb.labels === 3", timeout=10_000)

        # setLabelsVisible hides every label on the next frame and shows them again.
        page.evaluate("window.usdWebViewer.flyTo(32, 8, 32, 102.4)")
        page.wait_for_function(TARGET_IS, arg=[32, 32, 102.4], timeout=10_000)
        page.evaluate("window.usdWebViewer.setLabelsVisible(false)")
        page.wait_for_function(f"{LABELS}.length === 3 && {LABELS}.every(e => e.style.display === 'none')", timeout=10_000)
        page.evaluate("window.usdWebViewer.setLabelsVisible(true)")
        page.wait_for_function(f"{LABELS}.some(e => e.style.display !== 'none')", timeout=10_000)

        # setSpritesVisible toggles the sprite group.
        sprite_group = "window.usdWebViewer.scene.children.filter(c => c.isGroup)"
        assert page.evaluate(f"{sprite_group}.map(g => [g.visible, g.children.length])") == [[True, state["sprites"]]]
        page.evaluate("window.usdWebViewer.setSpritesVisible(false)")
        assert page.evaluate(f"{sprite_group}.map(g => g.visible)") == [False]
        page.evaluate("window.usdWebViewer.setSpritesVisible(true)")
        assert page.evaluate(f"{sprite_group}.map(g => g.visible)") == [True]

        assert page.evaluate("window.__usdWeb.errors") == []

        # dispose drops every tile mesh and stops polling.
        disposed = page.evaluate(
            """async () => { const v = window.usdWebViewer; v.dispose();
                return [await v.poll(), Object.keys(window.__usdWeb.versions).length,
                        v.scene.children.filter(c => c.isMesh).length]; }"""
        )
        assert disposed == [False, 0, 0]
        assert page.evaluate("window.__usdWeb.errors") == []
        assert errors == []
        browser.close()
