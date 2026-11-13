import json
import shutil
import subprocess
import sys

import numpy as np
import pytest

from usd_web import Tile, __version__, read_index
from usd_web.cli import main


def test_heightmap_cli_and_inspect(tmp_path, capsys):
    npz = tmp_path / "map.npz"
    h = np.arange(64 * 64).reshape(64, 64) % 7
    np.savez(npz, heights=h, colors=(h > 3).astype(int), palette=np.array(["#00aa00", "#888888"]), origin=np.array([-32, 0]))
    out = tmp_path / "tiles"
    assert main(["heightmap", str(npz), "-o", str(out), "--tile-size", "32", "--title", "Demo"]) == 0
    doc = read_index(out)
    assert doc["title"] == "Demo" and len(doc["tiles"]) == 4 and doc["palette"] == ["#00aa00", "#888888"]
    capsys.readouterr()
    assert main(["inspect", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["tiles"] == 4
    one = next(iter(doc["tiles"].values()))["file"]
    assert main(["inspect", str(out / one)]) == 0
    assert "byDir" in json.loads(capsys.readouterr().out)


def test_tiles_cli_from_stage(tmp_path):
    from test_stage import make_stage

    path = tmp_path / "s.usda"
    stage = make_stage("Y")  # keep the stage alive while its layer is exported
    stage.GetRootLayer().Export(str(path))
    out = tmp_path / "tiles"
    assert main(["tiles", str(path), "-o", str(out), "--tile-size", "16", "--exclude", "flower"]) == 0
    assert "#ff0000" not in read_index(out)["palette"]


def test_cli_reports_errors(tmp_path, capsys):
    assert main(["tiles", str(tmp_path / "missing.usda"), "-o", str(tmp_path / "o")]) == 2
    assert "error" in capsys.readouterr().err


def test_inspect_reads_the_named_json_and_reports_bad_paths(tmp_path, capsys):
    npz = tmp_path / "map.npz"
    h = np.arange(16 * 16).reshape(16, 16) % 5
    np.savez(npz, heights=h, colors=(h > 2).astype(int), palette=np.array(["#00aa00", "#888888"]))
    out = tmp_path / "tiles"
    assert main(["heightmap", str(npz), "-o", str(out)]) == 0
    doc = read_index(out)
    doc["revision"] = 99
    (out / "copy.json").write_text(json.dumps(doc))
    capsys.readouterr()
    assert main(["inspect", str(out / "copy.json")]) == 0
    assert json.loads(capsys.readouterr().out)["revision"] == 99

    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "replay.json").write_text(json.dumps({"format": "usd-web-replay", "version": 1}))
    assert main(["inspect", str(empty)]) == 2
    assert "usd-web: error:" in capsys.readouterr().err
    assert main(["inspect", str(empty / "replay.json")]) == 2
    assert "invalid index: format" in capsys.readouterr().err

    doc["labels"] = [1]
    (out / "bad-labels.json").write_text(json.dumps(doc))
    assert main(["inspect", str(out / "bad-labels.json")]) == 2
    assert "invalid index: label must be an object" in capsys.readouterr().err


def _tile_quads(out, doc):
    (meta,) = doc["tiles"].values()
    return Tile.decode((out / meta["file"]).read_bytes()).quads


def test_heightmap_cli_extends_the_published_palette_unless_replace(tmp_path, capsys):
    h = np.full((16, 16), 4)
    first, second = tmp_path / "a.npz", tmp_path / "b.npz"
    np.savez(first, heights=h, colors=np.zeros_like(h), palette=np.array(["#00aa00", "#888888"]))
    np.savez(second, heights=h, colors=np.ones_like(h), palette=np.array(["#888888", "#0000ff"]))
    out = tmp_path / "tiles"
    assert main(["heightmap", str(first), "-o", str(out), "--tile-size", "16"]) == 0
    assert main(["heightmap", str(second), "-o", str(out), "--tile-size", "16"]) == 0
    doc = read_index(out)
    assert doc["palette"] == ["#00aa00", "#888888", "#0000ff"] and doc["revision"] == 2
    quads = _tile_quads(out, doc)
    assert set(quads[quads[:, 0] == 0, 6].tolist()) == {2}  # npz index 1 remapped onto the published palette
    assert main(["heightmap", str(second), "-o", str(out), "--tile-size", "16", "--replace"]) == 0
    doc = read_index(out)
    assert doc["palette"] == ["#888888", "#0000ff"] and doc["revision"] == 3
    quads = _tile_quads(out, doc)
    assert set(quads[quads[:, 0] == 0, 6].tolist()) == {1}
    capsys.readouterr()
    assert main(["inspect", str(out)]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert (summary["palette"], summary["layers"], summary["labels"], summary["sprites"]) == (2, [], 0, 0)


def test_heightmap_cli_uses_the_subsurface_array_for_side_bodies(tmp_path):
    h = np.full((4, 4), 5)
    palette = np.array(["#00aa00", "#884400"])
    with_sub, without_sub = tmp_path / "sub.npz", tmp_path / "plain.npz"
    np.savez(with_sub, heights=h, colors=np.zeros_like(h), subsurface=np.ones_like(h), palette=palette)
    np.savez(without_sub, heights=h, colors=np.zeros_like(h), palette=palette)
    for npz, want in ((with_sub, {0, 1}), (without_sub, {0})):
        out = tmp_path / npz.stem
        assert main(["heightmap", str(npz), "-o", str(out), "--tile-size", "16"]) == 0
        doc = read_index(out)
        assert doc["tiles"]["t.0.0"]["x"] == 0 and doc["tiles"]["t.0.0"]["z"] == 0  # origin defaults to (0, 0)
        quads = _tile_quads(out, doc)
        assert set(quads[quads[:, 0] != 0, 6].tolist()) == want


def test_heightmap_cli_reports_a_missing_array(tmp_path, capsys):
    npz = tmp_path / "map.npz"
    h = np.full((4, 4), 1)
    np.savez(npz, heights=h, colors=np.zeros_like(h))
    assert main(["heightmap", str(npz), "-o", str(tmp_path / "o")]) == 2
    err = capsys.readouterr().err
    assert err.startswith("usd-web: error:") and "palette" in err
    assert not (tmp_path / "o").exists()


def test_inspect_summarises_a_tile_by_direction(tmp_path, capsys):
    quads = [[0, 0, 0, 5, 2, 2, 0], [0, 2, 0, 7, 1, 1, 0], [1, 0, 0, 3, 1, 4, 1], [3, 0, 0, 2, 2, 6, 1]]
    raw = tmp_path / "raw.uwt"
    raw.write_bytes(Tile(-16, 32, 16, 16, quads).encode())  # uncompressed bytes are read too
    assert main(["inspect", str(raw)]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "origin": [-16, 32],
        "size": [16, 16],
        "quads": 4,
        "byDir": {"top": 2, "+x": 1, "-x": 0, "+z": 1, "-z": 0},
        "yRange": [2, 8],
    }
    empty = tmp_path / "empty.uwt"
    empty.write_bytes(Tile(0, 0, 8, 8, np.zeros((0, 7))).to_bytes())
    assert main(["inspect", str(empty)]) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["quads"] == 0 and set(got["byDir"].values()) == {0} and got["yRange"] == [0, 0]
    (tmp_path / "junk.uwt").write_bytes(b"UWT9" + bytes(16))
    assert main(["inspect", str(tmp_path / "junk.uwt")]) == 2
    assert "not a UWT1 tile" in capsys.readouterr().err


def test_every_argument_has_help():
    from usd_web.cli import build_parser

    parser = build_parser()
    commands = next(a for a in parser._actions if a.dest == "command").choices
    for name, sub in commands.items():
        for action in sub._actions:
            assert action.help, f"usd-web {name}: {action.dest} has no help"


def test_python_module_entry_point():
    r = subprocess.run([sys.executable, "-m", "usd_web", "--version"], capture_output=True, text=True)
    assert r.returncode == 0 and r.stdout.strip() == f"usd-web {__version__}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_js_decoder_matches_python(tmp_path, root):
    rng = np.random.default_rng(1)
    q = np.stack([rng.integers(0, 5, 9), rng.integers(0, 64, 9), rng.integers(0, 64, 9), rng.integers(-60, 300, 9),
                  rng.integers(1, 16, 9), rng.integers(1, 16, 9), rng.integers(0, 300, 9)], axis=1)
    tile = Tile(-64, 128, 64, 64, q)
    path = tmp_path / "t.uwt"
    path.write_bytes(tile.to_bytes())
    script = tmp_path / "decode.mjs"
    module = (root / "viewer" / "usd-web-viewer.js").as_uri()
    script.write_text(
        f"""import {{ readFileSync }} from "node:fs";
import {{ decodeTile, maybeGunzip, tileArrays }} from "{module}";
const t = decodeTile(await maybeGunzip(readFileSync(process.argv[2])));
const a = tileArrays(t);
const cols = ["dir", "qx", "qz", "y0", "du", "dv", "color"];
const quads = [...Array(t.n).keys()].map(i => cols.map(c => t[c][i]));
console.log(JSON.stringify({{x: t.x, z: t.z, width: t.width, depth: t.depth, quads, verts: a.pos.length}}));
"""
    )
    r = subprocess.run(["node", str(script), str(path)], capture_output=True, text=True, check=True)
    got = json.loads(r.stdout)
    assert (got["x"], got["z"], got["width"], got["depth"]) == (-64, 128, 64, 64)
    assert got["quads"] == q.tolist()
    assert got["verts"] == 9 * 12


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_js_format_constants_match_python(tmp_path, root):
    from usd_web import INDEX_FORMAT, INDEX_VERSION, MAGIC
    from usd_web.replay import CHUNK_MAGIC, REPLAY_FORMAT, REPLAY_VERSION

    script = tmp_path / "constants.mjs"
    viewer = (root / "viewer" / "usd-web-viewer.js").as_uri()
    replay = (root / "viewer" / "usd-web-replay.js").as_uri()
    script.write_text(
        f"""import * as v from "{viewer}";
import * as r from "{replay}";
console.log(JSON.stringify({{index: [v.INDEX_FORMAT, v.INDEX_VERSION, v.TILE_MAGIC],
  replay: [r.REPLAY_FORMAT, r.REPLAY_VERSION, r.CHUNK_MAGIC]}}));
"""
    )
    r = subprocess.run(["node", str(script)], capture_output=True, text=True, check=True)
    got = json.loads(r.stdout)
    assert got["index"] == [INDEX_FORMAT, INDEX_VERSION, MAGIC.decode("ascii")]
    assert got["replay"] == [REPLAY_FORMAT, REPLAY_VERSION, CHUNK_MAGIC.decode("ascii")]
