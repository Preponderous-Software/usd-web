"""Command-line entry point: ``usd-web``."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

from . import __version__
from .publish import INDEX_NAME, publish, read_index
from .surface import Palette, Surface
from .tile import Tile, build_tiles


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("-o", "--out", required=True, metavar="DIR", help="tile directory (index.json + *.uwt)")
    p.add_argument("--tile-size", type=int, default=256, metavar="N", help="cells per tile side (default 256)")
    p.add_argument("--chunk-size", type=int, default=16, metavar="N", help="greedy-merge break interval (default 16)")
    p.add_argument("--skirt", type=int, default=3, metavar="N", help="cells below the lowest column the tile edge walls reach (default 3)")
    p.add_argument("--title", default=None, help="title shown by the viewer")
    p.add_argument("--replace", action="store_true", help="start a fresh index instead of updating the existing one")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="usd-web", description="Build and inspect usd-web tiles.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    t = sub.add_parser("tiles", help="tile the PointInstancers of a USD stage")
    t.add_argument("stage", help="stage to read (.usda/.usdc/.usd)")
    _common(t)
    t.add_argument("--exclude", action="append", default=[], metavar="NAME", help="skip prototypes with this prim name (repeatable)")
    t.add_argument("--cell-size", type=float, default=1.0, help="stage units per cell (default 1)")

    h = sub.add_parser("heightmap", help="tile a heightmap .npz (heights, colors, palette[, subsurface, origin])")
    h.add_argument("npz")
    _common(h)

    i = sub.add_parser("inspect", help="summarise an index.json, a tile directory, or a .uwt tile")
    i.add_argument("path")
    return parser


def _publish(args, surface: Surface, palette: Palette) -> int:
    tiles = build_tiles(surface, tile_size=args.tile_size, chunk_size=args.chunk_size, skirt=args.skirt)
    doc = publish(args.out, tiles, palette, title=args.title, replace=args.replace)
    quads = sum(m["quads"] for m in doc["tiles"].values())
    print(f"published {len(tiles)} tile(s) to {args.out} (revision {doc['revision']}, {quads} quads in index)")
    return 0


def _existing_palette(args) -> Palette:
    if args.replace:
        return Palette()
    doc = read_index(args.out) if Path(args.out).exists() else None
    return Palette(doc["palette"]) if doc else Palette()


def cmd_tiles(args) -> int:
    from .stage import exclude_names, surface_from_stage

    include = exclude_names(args.exclude) if args.exclude else None
    surface, palette = surface_from_stage(args.stage, palette=_existing_palette(args), include=include, cell_size=args.cell_size)
    return _publish(args, surface, palette)


def cmd_heightmap(args) -> int:
    data = np.load(args.npz, allow_pickle=False)
    palette = _existing_palette(args)
    remap = np.array([palette.add(str(c)) for c in data["palette"]], dtype=np.int64)
    colors = remap[np.asarray(data["colors"], dtype=np.int64)]
    sub = remap[np.asarray(data["subsurface"], dtype=np.int64)] if "subsurface" in data else None
    origin = tuple(int(v) for v in data["origin"]) if "origin" in data else (0, 0)
    surface = Surface.from_heightmap(data["heights"], colors, sub, origin=origin)
    return _publish(args, surface, palette)


def cmd_inspect(args) -> int:
    path = Path(args.path)
    if path.is_dir():
        path = path / INDEX_NAME
    if path.name.endswith(".json"):
        doc = read_index(path.parent)
        assert doc is not None
        summary = {
            "format": doc["format"],
            "version": doc["version"],
            "revision": doc["revision"],
            "tiles": len(doc["tiles"]),
            "quads": sum(m["quads"] for m in doc["tiles"].values()),
            "bytes": sum(m.get("bytes", 0) for m in doc["tiles"].values()),
            "palette": len(doc["palette"]),
            "layers": [layer["id"] for layer in doc.get("layers", [])],
            "labels": len(doc.get("labels", [])),
            "sprites": len(doc.get("sprites", {}).get("items", [])),
        }
    else:
        tile = Tile.decode(path.read_bytes())
        dirs = np.bincount(tile.quads[:, 0], minlength=5).tolist() if len(tile.quads) else [0] * 5
        summary = {
            "origin": [tile.x, tile.z],
            "size": [tile.width, tile.depth],
            "quads": len(tile.quads),
            "byDir": dict(zip(("top", "+x", "-x", "+z", "-z"), dirs)),
            "yRange": list(tile.y_range),
        }
    print(json.dumps(summary, indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return {"tiles": cmd_tiles, "heightmap": cmd_heightmap, "inspect": cmd_inspect}[args.command](args)
    except (ValueError, OSError, KeyError) as exc:
        print(f"usd-web: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
