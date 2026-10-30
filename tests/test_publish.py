import json
import os

import numpy as np
import pytest

from usd_web import Palette, Surface, atomic_write_bytes, build_tiles, publish, read_index, validate_index
from usd_web.publish import INDEX_NAME
from usd_web.tile import Tile


def tiles_for(height=3, size=32):
    h = np.full((size, size), height)
    h[:16, :16] += 1
    return build_tiles(Surface.from_heightmap(h, np.zeros_like(h)), tile_size=16, chunk_size=16)


def files(path):
    return sorted(p.name for p in path.iterdir())


def test_publish_writes_index_and_content_addressed_tiles(tmp_path):
    doc = publish(tmp_path, tiles_for(), Palette(["#808080"]), generated=1.0)
    assert doc["format"] == "usd-web-index" and doc["version"] == 1 and doc["revision"] == 1
    assert sorted(doc["tiles"]) == ["t.0.0", "t.0.1", "t.1.0", "t.1.1"]
    for name, meta in doc["tiles"].items():
        assert meta["file"] == f"{name}.{meta['version']}.uwt"
        assert Tile.decode((tmp_path / meta["file"]).read_bytes()).x == meta["x"]
    assert read_index(tmp_path) == doc
    assert not [n for n in files(tmp_path) if n.startswith(".")]  # no temp files left


def test_publish_is_deterministic(tmp_path):
    a = publish(tmp_path / "a", tiles_for(), Palette(["#808080"]), generated=1.0)
    b = publish(tmp_path / "b", tiles_for(), Palette(["#808080"]), generated=1.0)
    assert a == b
    assert (tmp_path / "a" / INDEX_NAME).read_bytes() == (tmp_path / "b" / INDEX_NAME).read_bytes()


def test_incremental_publish_changes_only_the_changed_tile(tmp_path):
    first = publish(tmp_path, tiles_for(), Palette(["#808080"]))
    changed = {"t.0.0": tiles_for(height=9)["t.0.0"]}
    second = publish(tmp_path, changed, Palette(["#808080"]))
    assert second["revision"] == 2
    diff = [n for n in first["tiles"] if first["tiles"][n]["version"] != second["tiles"][n]["version"]]
    assert diff == ["t.0.0"]
    # The previous generation's file is kept for clients that just read the old index ...
    assert first["tiles"]["t.0.0"]["file"] in files(tmp_path)
    third = publish(tmp_path, {"t.0.0": tiles_for(height=11)["t.0.0"]}, Palette(["#808080"]))
    # ... and pruned one publish later.
    assert first["tiles"]["t.0.0"]["file"] not in files(tmp_path)
    assert second["tiles"]["t.0.0"]["file"] in files(tmp_path)
    assert third["tiles"]["t.0.0"]["file"] in files(tmp_path)


def test_index_is_written_last(tmp_path, monkeypatch):
    order = []
    real = os.replace

    def spy(src, dst):
        order.append(os.path.basename(dst))
        return real(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    publish(tmp_path, tiles_for(), Palette(["#808080"]))
    assert order[-1] == INDEX_NAME
    assert order.count(INDEX_NAME) == 1 and len(order) == 5


def test_crash_mid_publish_keeps_previous_index_valid(tmp_path, monkeypatch):
    first = publish(tmp_path, tiles_for(), Palette(["#808080"]))
    before = (tmp_path / INDEX_NAME).read_bytes()
    real = os.replace
    calls = {"n": 0}

    def crash_on_second_tile(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real(src, dst)

    monkeypatch.setattr(os, "replace", crash_on_second_tile)
    with pytest.raises(OSError):
        publish(tmp_path, tiles_for(height=20), Palette(["#808080"]))
    monkeypatch.setattr(os, "replace", real)
    assert (tmp_path / INDEX_NAME).read_bytes() == before
    doc = read_index(tmp_path)
    for meta in doc["tiles"].values():
        assert (tmp_path / meta["file"]).exists()
    assert doc == first
    assert not [n for n in files(tmp_path) if n.endswith(".tmp")]


def test_crash_while_writing_index_keeps_previous_index(tmp_path, monkeypatch):
    publish(tmp_path, tiles_for(), Palette(["#808080"]))
    before = (tmp_path / INDEX_NAME).read_bytes()
    real = os.replace

    def crash_on_index(src, dst):
        if os.path.basename(dst) == INDEX_NAME:
            raise OSError("power cut")
        return real(src, dst)

    monkeypatch.setattr(os, "replace", crash_on_index)
    with pytest.raises(OSError):
        publish(tmp_path, tiles_for(height=20), Palette(["#808080"]))
    assert (tmp_path / INDEX_NAME).read_bytes() == before


def test_atomic_write_bytes_replaces_the_file(tmp_path):
    path = tmp_path / "f.bin"
    atomic_write_bytes(path, b"old")
    atomic_write_bytes(path, b"new")
    assert path.read_bytes() == b"new" and files(tmp_path) == ["f.bin"]
    assert path.stat().st_mode & 0o777 == 0o644


def test_atomic_write_bytes_failure_keeps_old_file_and_no_temp(tmp_path, monkeypatch):
    path = tmp_path / "f.bin"
    atomic_write_bytes(path, b"old")

    def crash(src, dst):
        raise OSError("power cut")

    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError):
        atomic_write_bytes(path, b"new")
    assert path.read_bytes() == b"old" and files(tmp_path) == ["f.bin"]


def test_palette_must_be_append_only(tmp_path):
    publish(tmp_path, tiles_for(), Palette(["#808080", "#00ff00"]))
    publish(tmp_path, {}, Palette(["#808080", "#00ff00", "#0000ff"]))
    with pytest.raises(ValueError):
        publish(tmp_path, {}, Palette(["#00ff00"]))
    doc = publish(tmp_path, tiles_for(), Palette(["#00ff00"]), replace=True)
    assert doc["palette"] == ["#00ff00"] and doc["revision"] == 3  # the refused publish wrote nothing


def test_sections_are_validated_and_carried(tmp_path):
    layers = [{"id": "r", "kind": "cells", "cellSize": 16, "entries": [{"label": "A", "color": "#ff0000", "cells": [[0, 0]]}]}]
    sprites = {"images": {"tree": "tree.png"}, "items": [{"image": "tree", "position": [1, None, 2]}]}
    publish(tmp_path, tiles_for(), Palette(["#808080"]), layers=layers, labels=[{"text": "x", "position": [0, 1, 0]}], sprites=sprites, title="T")
    doc = publish(tmp_path, {}, Palette(["#808080"]))
    assert doc["layers"] == layers and doc["sprites"] == sprites and doc["title"] == "T"
    with pytest.raises(ValueError):
        publish(tmp_path, {}, Palette(["#808080"]), sprites={"images": {}, "items": [{"image": "nope", "position": [0, 0, 0]}]})
    with pytest.raises(ValueError):
        publish(tmp_path, {}, Palette(["#808080"]), layers=[{"id": "r", "kind": "blobs", "cellSize": 16}])


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(format="other"),
        lambda d: d.update(version=2),
        lambda d: d.update(palette=["red"]),
        lambda d: d["tiles"]["t.0.0"].update(file="../x"),
        lambda d: d["tiles"]["t.0.0"].pop("version"),
        # malformed nested shapes must be ValueError too, never AttributeError/TypeError
        lambda d: d.update(palette=[5]),
        lambda d: d.update(layers=5),
        lambda d: d.update(layers=[{"id": "r", "kind": "cells", "cellSize": 16, "entries": {"a": 1}}]),
        lambda d: d.update(layers=[{"id": "r", "kind": "cells", "cellSize": 16, "entries": ["x"]}]),
        lambda d: d.update(labels={"text": "x"}),
        lambda d: d.update(labels=[1]),
        lambda d: d.update(sprites=[]),
        lambda d: d.update(sprites={"images": {}, "items": {"image": "x"}}),
        lambda d: d.update(sprites={"images": {}, "items": [1]}),
        lambda d: d.update(sprites={"images": {}, "items": [{"image": [1], "position": [0, 0, 0]}]}),
    ],
)
def test_validate_index_rejects(tmp_path, mutate):
    doc = publish(tmp_path, tiles_for(), Palette(["#808080"]))
    doc = json.loads(json.dumps(doc))
    mutate(doc)
    with pytest.raises(ValueError):
        validate_index(doc)
