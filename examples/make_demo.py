"""Generate a demo tile set from a synthetic heightmap.

    python examples/make_demo.py viewer/demo          # then serve viewer/ and open index.html
    python examples/make_demo.py viewer/demo --bump 1 # republish with one tile changed (hot swap)

The terrain is a few overlapping sine waves with a water level; two overlay regions, labels and
a handful of billboard sprites show the optional index sections.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import numpy as np

from usd_web import Palette, Surface, build_tiles, publish

SIZE = 128  # 2 x 2 tiles of 64
TILE = 64
CHUNK = 16
TREE_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="32" height="48" viewBox="0 0 32 48">
<rect x="13" y="32" width="6" height="16" fill="#6b4a2b"/>
<polygon points="16,0 30,34 2,34" fill="#2f7d3b"/>
</svg>
"""


def heightmap(bump: int = 0) -> tuple[np.ndarray, np.ndarray, Palette]:
    z, x = np.mgrid[0:SIZE, 0:SIZE].astype(np.float64)
    h = 8 + 5 * np.sin(x / 9.0) * np.cos(z / 13.0) + 3 * np.sin((x + 2 * z) / 23.0)
    h = np.round(h).astype(np.int64)
    if bump:
        # A raised plateau in tile t.0.0 only: republishing changes exactly one tile.
        h[8:24, 8:24] += 4 * bump
    palette = Palette(["#3b6fb6", "#d8c98a", "#5a9e4b", "#7d7f80", "#f2f4f5"])
    water, sand, grass, rock, snow = range(5)
    colors = np.select([h <= 4, h <= 5, h <= 11, h <= 14], [water, sand, grass, rock], snow)
    h = np.maximum(h, 4)  # flat water
    return h, colors, palette


def make_demo(out_dir: str | Path, bump: int = 0, generated: float | None = None) -> dict:
    out = Path(out_dir)
    h, colors, palette = heightmap(bump)
    sub = np.where(colors == 2, 3, colors)  # grass over rock
    surface = Surface.from_heightmap(h, colors, sub)
    tiles = build_tiles(surface, tile_size=TILE, chunk_size=CHUNK)
    layers = [
        {
            "id": "regions",
            "label": "Regions",
            "kind": "cells",
            "cellSize": CHUNK,
            "visible": True,
            "tint": 0.5,
            "labels": True,
            "entries": [
                {"label": "North field", "color": "#e0553d", "cells": [[1, 1], [2, 1], [1, 2], [2, 2]]},
                {"label": "East hills", "color": "#8a4fd8", "cells": [[5, 4], [6, 4], [5, 5], [6, 5], [6, 6]]},
            ],
        }
    ]
    labels = [{"text": "Origin", "position": [0, None, 0]}]
    rng = np.random.default_rng(7)
    items = []
    for _ in range(12):
        x, z = (int(v) for v in rng.integers(4, SIZE - 4, size=2))
        if colors[z, x] == 2:
            items.append({"image": "tree", "position": [x + 0.5, None, z + 0.5], "scale": 3})
    sprites = {"images": {"tree": "tree.svg"}, "items": items}
    out.mkdir(parents=True, exist_ok=True)
    (out / "tree.svg").write_text(TREE_SVG, encoding="utf-8")
    return publish(
        out,
        tiles,
        palette,
        layers=layers,
        labels=labels,
        sprites=sprites,
        title="usd-web demo",
        view={"target": [SIZE / 2, 8, SIZE / 2], "distance": 190, "theta": math.radians(40), "phi": 0.95},
        generated=generated,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("out", nargs="?", default="viewer/demo")
    parser.add_argument("--bump", type=int, default=0, help="raise a plateau in one tile (hot-swap demo)")
    args = parser.parse_args()
    doc = make_demo(args.out, args.bump)
    print(f"revision {doc['revision']}: {len(doc['tiles'])} tiles in {args.out}")


if __name__ == "__main__":
    main()
