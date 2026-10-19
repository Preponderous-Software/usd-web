"""Capture README images of the replay player from a served replay.

    usd-web replay run.usdc -o site/replay
    cp viewer/replay.html viewer/usd-web-replay.js site/
    python -m http.server -d site 8000 &
    python docs/capture_replay.py "http://127.0.0.1:8000/replay.html?data=replay/" docs/images

Writes replay-desktop.png (1280x800), replay-phone.png (390x844 at 2x) and replay.gif (the
whole run, one frame per step of the timeline, seeked while paused so the GIF is reproducible).
Needs `playwright` (with a Chromium) and Pillow; WebGL runs on SwiftShader, so no GPU is needed.
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

ARGS = ["--no-sandbox", "--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]
HOOK = "window.__usdWebReplay"


def open_page(browser, url, width, height, scale=1, mobile=False):
    page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=scale,
                            is_mobile=mobile, has_touch=mobile)
    sep = "&" if "?" in url else "?"
    page.goto(url + sep + "autoplay=0", wait_until="networkidle")
    page.wait_for_function(f"{HOOK} && {HOOK}.ready && {HOOK}.loaded === {HOOK}.chunks", timeout=120_000)
    return page


def seek(page, sample: int) -> None:
    page.evaluate(f"s => {{ window.usdWebReplay.seekSample(s); {HOOK}.render(); }}", sample)
    page.wait_for_timeout(120)


def main(url: str, out: Path, frames: int = 48, at: float = 0.2, width: int = 560) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(args=ARGS, executable_path=os.environ.get("USD_WEB_CHROMIUM") or None)
        page = open_page(browser, url, 1280, 800)
        n = page.evaluate(f"{HOOK}.samples")
        seek(page, int((n - 1) * at))
        page.screenshot(path=str(out / "replay-desktop.png"))
        shots = []
        for i in range(frames):
            seek(page, round(i * (n - 1) / (frames - 1)))
            img = Image.open(io.BytesIO(page.screenshot())).convert("RGB")
            shots.append(img.resize((width, round(width * img.height / img.width)), Image.LANCZOS))
        pal = shots[len(shots) // 2].quantize(colors=200, method=Image.Quantize.FASTOCTREE)
        q = [s.quantize(palette=pal, dither=Image.Dither.NONE) for s in shots]
        q[0].save(out / "replay.gif", save_all=True, append_images=q[1:], duration=120, loop=0, optimize=True)
        phone = open_page(browser, url, 390, 844, scale=2, mobile=True)
        seek(phone, int((n - 1) * at))
        phone.screenshot(path=str(out / "replay-phone.png"))
        browser.close()


if __name__ == "__main__":
    main(sys.argv[1], Path(sys.argv[2]), *(int(a) for a in sys.argv[3:4]))
