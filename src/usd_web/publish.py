"""Atomic publishing of tiles plus a versioned ``index.json``.

Order of a publish (ARCHITECTURE.md § Atomic publish):

1. every changed tile is written under a **content-addressed** name
   (``<tile>.<version>.uwt``) via a temporary file in the same directory and ``os.replace``;
2. ``index.json`` is written the same way, **last** - so a poller can never read an index
   that names a tile file which is not fully on disk;
3. tile files referenced by neither the new nor the previous index are deleted (the previous
   generation is kept so a client that fetched the old index a moment ago still finds its tiles).

A crash at any point leaves the previous ``index.json`` and every file it names intact.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping

from .surface import Palette
from .tile import Tile, tile_version

INDEX_NAME = "index.json"
INDEX_FORMAT = "usd-web-index"
INDEX_VERSION = 1
TILE_SUFFIX = ".uwt"
LAYER_KINDS = ("cells",)


def atomic_write_bytes(path: str | Path, data: bytes) -> None:
    """Write ``data`` to ``path`` through a temporary file in the same directory, fsync it,
    and move it into place with ``os.replace``. Readers see the old file or the new one."""
    path = Path(path)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def read_index(out_dir: str | Path) -> dict[str, Any] | None:
    path = Path(out_dir) / INDEX_NAME
    if not path.exists():
        return None
    doc = json.loads(path.read_text(encoding="utf-8"))
    validate_index(doc)
    return doc


def _is_color(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        Palette.normalize(value)
        return True
    except ValueError:
        return False


def _xyz(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 3
        and all(v is None or (isinstance(v, (int, float)) and not isinstance(v, bool)) for v in value)
        and value[0] is not None
        and value[2] is not None
    )


def validate_index(doc: Any) -> None:
    """Raise ``ValueError`` unless ``doc`` is a valid version-1 index (see ARCHITECTURE.md)."""

    def need(cond: bool, msg: str) -> None:
        if not cond:
            raise ValueError(f"invalid index: {msg}")

    need(isinstance(doc, dict), "not an object")
    need(doc.get("format") == INDEX_FORMAT, f"format must be {INDEX_FORMAT!r}")
    version = doc.get("version")
    need(isinstance(version, int) and not isinstance(version, bool), "version must be an integer")
    need(version <= INDEX_VERSION, f"version {version} is newer than supported ({INDEX_VERSION})")
    need(isinstance(doc.get("revision"), int), "revision must be an integer")
    palette = doc.get("palette")
    need(isinstance(palette, list) and all(_is_color(c) for c in palette), "palette must be '#rrggbb' strings")
    tiles = doc.get("tiles")
    need(isinstance(tiles, dict), "tiles must be an object")
    for name, meta in tiles.items():
        need(isinstance(meta, dict), f"tile {name} must be an object")
        for key in ("file", "version"):
            need(isinstance(meta.get(key), str), f"tile {name}: {key} must be a string")
        for key in ("x", "z", "width", "depth", "quads"):
            need(isinstance(meta.get(key), int), f"tile {name}: {key} must be an integer")
        need("/" not in meta["file"] and "\\" not in meta["file"] and not meta["file"].startswith("."), f"tile {name}: bad file name")
    layers = doc.get("layers", [])
    need(isinstance(layers, list), "layers must be a list")
    for layer in layers:
        need(isinstance(layer, dict), "layer must be an object")
        need(isinstance(layer.get("id"), str) and layer["id"], "layer id must be a non-empty string")
        need(layer.get("kind") in LAYER_KINDS, f"layer {layer.get('id')}: kind must be one of {LAYER_KINDS}")
        need(isinstance(layer.get("cellSize"), int) and layer["cellSize"] > 0, "layer cellSize must be a positive integer")
        entries = layer.get("entries", [])
        need(isinstance(entries, list), f"layer {layer['id']}: entries must be a list")
        for entry in entries:
            need(isinstance(entry, dict), f"layer {layer['id']}: entry must be an object")
            need(isinstance(entry.get("label"), str), "layer entry label must be a string")
            need(_is_color(entry.get("color")), "layer entry color must be '#rrggbb'")
            cells = entry.get("cells")
            need(
                isinstance(cells, list) and all(isinstance(c, list) and len(c) == 2 and all(isinstance(v, int) for v in c) for c in cells),
                "layer entry cells must be [[cx, cz], ...]",
            )
    labels = doc.get("labels", [])
    need(isinstance(labels, list), "labels must be a list")
    for label in labels:
        need(isinstance(label, dict), "label must be an object")
        need(isinstance(label.get("text"), str), "label text must be a string")
        need(_xyz(label.get("position")), "label position must be [x, y|null, z]")
    sprites = doc.get("sprites")
    if sprites is not None:
        need(isinstance(sprites, dict), "sprites must be an object")
        images = sprites.get("images", {})
        need(isinstance(images, dict) and all(isinstance(v, str) for v in images.values()), "sprites.images must map names to URLs")
        items = sprites.get("items", [])
        need(isinstance(items, list), "sprites.items must be a list")
        for item in items:
            need(isinstance(item, dict), "sprite item must be an object")
            image = item.get("image")
            need(isinstance(image, str) and image in images, f"sprite image {image!r} is not in sprites.images")
            need(_xyz(item.get("position")), "sprite position must be [x, y|null, z]")


def tile_file_name(name: str, version: str) -> str:
    return f"{name}.{version}{TILE_SUFFIX}"


def publish(
    out_dir: str | Path,
    tiles: Mapping[str, Tile],
    palette: Palette,
    *,
    layers: list[dict[str, Any]] | None = None,
    labels: list[dict[str, Any]] | None = None,
    sprites: dict[str, Any] | None = None,
    title: str | None = None,
    view: dict[str, Any] | None = None,
    replace: bool = False,
    remove: tuple[str, ...] = (),
    generated: float | None = None,
) -> dict[str, Any]:
    """Publish ``tiles`` into ``out_dir`` and return the new index document.

    Without ``replace`` the publish is incremental: tiles of the existing index that are not
    in ``tiles`` (or ``remove``) are kept, and ``palette`` must extend the existing palette
    (append-only, so kept tiles keep their colours). ``layers``/``labels``/``sprites``/``title``
    /``view`` given as ``None`` keep the existing sections.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    previous = read_index(out)
    if previous is not None and not replace:
        old = previous["palette"]
        if palette.colors[: len(old)] != old:
            raise ValueError("palette must extend the published palette (append-only); pass replace=True to start over")
        tile_meta: dict[str, Any] = {k: v for k, v in previous["tiles"].items() if k not in remove}
    else:
        tile_meta = {}
    for name in sorted(tiles):
        tile = tiles[name]
        data = tile.to_bytes()
        version = tile_version(data)
        file = tile_file_name(name, version)
        target = out / file
        if not target.exists():
            atomic_write_bytes(target, data)
        lo, hi = tile.y_range
        tile_meta[name] = {
            "file": file,
            "version": version,
            "x": int(tile.x),
            "z": int(tile.z),
            "width": int(tile.width),
            "depth": int(tile.depth),
            "quads": int(len(tile.quads)),
            "bytes": len(data),
            "yMin": lo,
            "yMax": hi,
        }
    doc: dict[str, Any] = {
        "format": INDEX_FORMAT,
        "version": INDEX_VERSION,
        "revision": (previous["revision"] + 1) if previous is not None else 1,
        "generated": time.time() if generated is None else generated,
        "palette": list(palette.colors),
        "tiles": {k: tile_meta[k] for k in sorted(tile_meta)},
    }
    carry = previous if (previous is not None and not replace) else {}
    for key, value in (("title", title), ("view", view), ("layers", layers), ("labels", labels), ("sprites", sprites)):
        chosen = value if value is not None else carry.get(key)
        if chosen is not None:
            doc[key] = chosen
    validate_index(doc)
    atomic_write_bytes(out / INDEX_NAME, (json.dumps(doc, indent=1, sort_keys=True) + "\n").encode("utf-8"))
    prune(out, doc, previous)
    return doc


def prune(out_dir: Path, current: Mapping[str, Any], previous: Mapping[str, Any] | None) -> list[str]:
    """Delete ``*.uwt`` files named by neither ``current`` nor ``previous``; return their names."""
    referenced = {m["file"] for m in current["tiles"].values()}
    if previous is not None:
        referenced |= {m["file"] for m in previous["tiles"].values()}
    removed = []
    for path in sorted(Path(out_dir).glob(f"*{TILE_SUFFIX}")):
        if path.name not in referenced:
            path.unlink(missing_ok=True)
            removed.append(path.name)
    return removed


__all__ = [
    "INDEX_FORMAT",
    "INDEX_NAME",
    "INDEX_VERSION",
    "atomic_write_bytes",
    "prune",
    "publish",
    "read_index",
    "tile_file_name",
    "validate_index",
]
