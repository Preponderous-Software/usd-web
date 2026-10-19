import json
import shutil
import subprocess
import sys

import numpy as np
import pytest

from usd_web import Tile, read_index
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


def test_python_module_entry_point():
    r = subprocess.run([sys.executable, "-m", "usd_web", "--version"], capture_output=True, text=True)
    assert r.returncode == 0 and "0.2.0" in r.stdout


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
