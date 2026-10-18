"""Capture the README images of the viewer from a served tile set.

    usd-web tiles world.usda -o site/tiles --tile-size 128 --exclude short_grass --title "..."
    cp viewer/index.html viewer/usd-web-viewer.js site/
    python -m http.server -d site 8000 &
    python docs/capture_readme.py "http://127.0.0.1:8000/?data=tiles/" docs/images

Writes viewer-desktop.png (1280x800), viewer-phone.png (390x844) and viewer-orbit.gif (a drag
orbit). Needs `playwright` (with a Chromium) and Pillow; WebGL runs on SwiftShader, so no GPU is
required.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

ARGS = ["--no-sandbox", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]


def open_page(browser, url, width, height, scale=1, mobile=False):
    page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=scale,
                            is_mobile=mobile, has_touch=mobile)
    page.goto(url, wait_until="networkidle")
    page.wait_for_selector("canvas")
    page.wait_for_timeout(2000)  # tiles are fetched and meshed after the first frame
    return page


def main(url: str, out: Path, executable: str | None = None) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(args=ARGS, executable_path=executable)
        page = open_page(browser, url, 1280, 800)
        page.screenshot(path=str(out / "viewer-desktop.png"))

        # orbit: drag left in small steps, grabbing a frame after each
        frames = []
        page.mouse.move(640, 400)
        page.mouse.down()
        for i in range(30):
            page.mouse.move(640 - (i + 1) * 14, 400, steps=2)
            page.wait_for_timeout(60)
            shot = Image.open(io.BytesIO(page.screenshot())).convert("RGB")
            frames.append(shot.resize((560, 350), Image.LANCZOS))
        page.mouse.up()
        frames = [f.quantize(colors=96, method=Image.Quantize.MEDIANCUT) for f in frames]
        frames[0].save(out / "viewer-orbit.gif", save_all=True, append_images=frames[1:],
                       duration=80, loop=0, optimize=True)

        phone = open_page(browser, url, 390, 844, scale=2, mobile=True)
        phone.screenshot(path=str(out / "viewer-phone.png"))
        browser.close()


if __name__ == "__main__":
    main(sys.argv[1], Path(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else None)
