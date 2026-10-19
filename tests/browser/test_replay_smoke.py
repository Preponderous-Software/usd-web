"""Headless-browser smoke test of the replay player: the page loads a converted replay, plays,
scrubs, changes speed and pauses, and lays the controls out for a phone.

Run with ``pytest -m browser`` (see test_viewer_smoke.py). Needs usd-recorder for the fixture.
"""

from __future__ import annotations

import functools
import http.server
import os
import shutil
import sys
import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser
playwright = pytest.importorskip("playwright.sync_api")
pytest.importorskip("usd_recorder.replay")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from replay_fixture import write_replay  # noqa: E402

from usd_web.replay import build_replay  # noqa: E402

ARGS = ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture
def site(tmp_path, root):
    for name in ("replay.html", "usd-web-replay.js"):
        shutil.copy(root / "viewer" / name, tmp_path / name)
    doc = build_replay(write_replay(tmp_path / "fixture.usdc"), tmp_path / "replay", chunk_bytes=600)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(tmp_path)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield doc, f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()


def launch(p):
    return p.chromium.launch(executable_path=os.environ.get("USD_WEB_CHROMIUM") or None, args=ARGS)


def test_replay_plays_scrubs_and_pauses(site):
    doc, url = site
    with playwright.sync_playwright() as p:
        browser = launch(p)
        page = browser.new_page(viewport={"width": 900, "height": 600})
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(url + "replay.html?speed=4")
        page.wait_for_function("window.__usdWebReplay && window.__usdWebReplay.ready", timeout=60_000)
        hook = "window.__usdWebReplay"
        assert page.evaluate(f"{hook}.samples") == doc["samples"] and page.evaluate(f"{hook}.chunks") > 1
        page.wait_for_function(f"{hook}.loaded === {hook}.chunks", timeout=30_000)
        page.wait_for_function(f"{hook}.playing && {hook}.tick > 20", timeout=30_000)  # it plays
        assert page.locator("#title").text_content() == "Fixture replay"
        assert "seed 3" in page.locator("#sub").text_content()

        # Pause, then scrub: the tick and the state hash follow the scrubber.
        page.locator(".uwr-play").click()
        assert page.evaluate(f"{hook}.playing") is False
        assert page.locator(".uwr-play").text_content() == "Play"
        page.locator(".uwr-range").fill("10")
        page.wait_for_function(f"{hook}.tick === {doc['ticks'][10]}")
        assert page.evaluate(f"{hook}.hash") == doc["hashes"][10]
        assert doc["hashes"][10] in page.locator("#hash").text_content()
        assert page.locator(".uwr-tick").text_content().startswith(f"tick {doc['ticks'][10]}")
        # The arrow keys step one sample.
        page.locator("#view").focus()
        page.keyboard.press("ArrowRight")
        page.wait_for_function(f"{hook}.tick === {doc['ticks'][11]}")

        # Speed cycles through the steps; space resumes.
        speed = page.locator(".uwr-speed")
        assert speed.text_content() == "4×"
        speed.click()
        assert speed.text_content() == "8×"
        page.locator("#view").focus()
        page.keyboard.press(" ")
        page.wait_for_function(f"{hook}.playing")

        distinct = page.evaluate(
            # Render and read back in one task: the player does not preserve the drawing buffer.
            """() => { window.__usdWebReplay.render(); const c = document.getElementById('view');
                const g = c.getContext('webgl2') || c.getContext('webgl');
                const px = new Uint8Array(4 * 64 * 64);
                g.readPixels(Math.floor(c.width / 2) - 32, Math.floor(c.height / 2) - 32, 64, 64, g.RGBA, g.UNSIGNED_BYTE, px);
                const s = new Set(); for (let i = 0; i < px.length; i += 4) s.add(px[i] + ',' + px[i+1] + ',' + px[i+2]);
                return s.size; }"""
        )
        assert distinct > 3
        assert page.evaluate(f"{hook}.errors") == []
        assert errors == []

        # Phone: the scrubber gets its own row and every button is at least 44 px tall.
        phone = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
        phone.goto(url + "replay.html?autoplay=0")
        phone.wait_for_function(f"{hook} && {hook}.ready", timeout=60_000)
        assert phone.evaluate(f"{hook}.playing") is False
        rng, btn = phone.locator(".uwr-range").bounding_box(), phone.locator(".uwr-play").bounding_box()
        assert rng["width"] > 300 and rng["y"] < btn["y"]
        assert btn["height"] >= 44 and phone.locator(".uwr-speed").bounding_box()["height"] >= 44
        phone.locator(".uwr-play").tap()
        phone.wait_for_function(f"{hook}.playing && {hook}.tick > 0", timeout=30_000)
        browser.close()
