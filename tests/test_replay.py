import json
import shutil
import subprocess

import numpy as np
import pytest

pytest.importorskip("usd_recorder.replay")

from replay_fixture import write_replay  # noqa: E402
from usd_recorder.replay import linear_to_srgb8, read_replay  # noqa: E402

from usd_web.cli import main  # noqa: E402
from usd_web.replay import build_replay, decode_chunk, read_stage  # noqa: E402


@pytest.fixture
def stage(tmp_path):
    return write_replay(tmp_path / "fixture.usdc")


def decode_all(out, doc):
    samples = []
    for c in doc["chunks"]:
        got = decode_chunk((out / c["file"]).read_bytes(), doc)
        assert len(got) == c["count"]
        samples += got
    return samples


def test_round_trip(stage, tmp_path):
    out = tmp_path / "web"
    doc = build_replay(stage, out)
    src = read_replay(stage)
    assert doc["format"] == "usd-web-replay" and doc["version"] == 1
    assert doc["ticks"] == src.ticks and doc["hashes"] == src.hashes
    assert doc["title"] == "Fixture replay" and doc["meta"]["seed"] == "3" and doc["playbackRate"] == 20
    assert [s["id"] for s in doc["sets"]] == ["movers", "blinkers"]
    movers, blinkers = doc["sets"]
    assert movers["color"] == "#e0553d" and movers["width"] == pytest.approx(0.8) and not movers["vertexColor"]
    assert blinkers["vertexColor"] and blinkers["vertexWidth"] and blinkers["shape"] == "cube"
    samples = decode_all(out, doc)
    assert len(samples) == len(src.ticks)
    step = np.asarray(doc["quant"]["step"])
    for si, s in enumerate(src.sets):
        dense: dict[int, int] = {}
        for k, sample in enumerate(samples):
            e = sample["sets"][si]
            assert len(e["ids"]) == len(s.ids[k])
            for orig, d in zip(s.ids[k].tolist(), e["ids"].tolist(), strict=True):
                assert dense.setdefault(orig, d) == d  # one dense id per entity, for the whole replay
            assert np.all(np.abs(e["points"] - s.points[k]) <= step / 2 + 1e-9)
            if s.vertex_color:
                np.testing.assert_array_equal(e["colors"], linear_to_srgb8(s.colors[k]))
                np.testing.assert_allclose(e["widths"], s.widths[k], atol=blinkers["widthMax"] / 255)
    (heat,) = doc["fields"]
    assert heat["ticks"] == read_stage(stage)["fields"][0].ticks == [0, 25, 50, 75, 100]
    assert [k for k, s in enumerate(samples) if s["fields"][0] is not None] == [0, 5, 10, 15, 20]
    np.testing.assert_array_equal(samples[10]["fields"][0], np.full((48, 3), 120))
    assert doc["stats"]["movers"] == [3 + t // 20 for t in doc["ticks"]]
    assert doc["stats"]["tenth"][:3] == [0.0, 0.0, 1.0]  # held between its samples
    (rocks,) = doc["scene"]
    assert rocks["colors"] == ["#777777", "#999999"] and rocks["widths"] == [1.0, 1.5]
    assert doc["bounds"]["min"][0] <= -4 and doc["bounds"]["max"][0] >= 4.0


def test_chunks_stand_alone(stage, tmp_path):
    one = build_replay(stage, tmp_path / "one")
    many = build_replay(stage, tmp_path / "many", chunk_bytes=200)
    assert len(one["chunks"]) == 1 and len(many["chunks"]) > 3
    a, b = decode_all(tmp_path / "one", one), decode_all(tmp_path / "many", many)
    for c in many["chunks"]:
        assert b[c["first"]]["fields"][0] is not None  # every chunk restates the field
    for x, y in zip(a, b, strict=True):
        for ex, ey in zip(x["sets"], y["sets"], strict=True):
            np.testing.assert_array_equal(ex["points"], ey["points"])


def test_thinning_keeps_the_last_sample_and_held_fields(stage, tmp_path):
    doc = build_replay(stage, tmp_path / "w", every=3)
    assert doc["ticks"] == [0, 15, 30, 45, 60, 75, 90, 100] and doc["keptEvery"] == 3
    samples = decode_all(tmp_path / "w", doc)
    assert [s["fields"][0] is not None for s in samples] == [True, False, True, False, True, True, False, True]
    doc = build_replay(stage, tmp_path / "m", max_samples=5)
    assert doc["samples"] <= 5 and doc["ticks"][-1] == 100


def test_old_chunks_are_removed(stage, tmp_path):
    out = tmp_path / "web"
    first = build_replay(stage, out, chunk_bytes=200)
    second = build_replay(stage, out)
    assert {p.name for p in out.glob("*.uwr")} == {c["file"] for c in second["chunks"]}
    assert len(first["chunks"]) > 1


def test_rejects_a_non_replay(tmp_path):
    from pxr import Usd

    s = Usd.Stage.CreateNew(str(tmp_path / "plain.usda"))
    s.DefinePrim("/World")
    s.Save()
    with pytest.raises(ValueError, match="usd-replay/1"):
        build_replay(tmp_path / "plain.usda", tmp_path / "o")


def test_cli(stage, tmp_path, capsys):
    assert main(["replay", str(stage), "-o", str(tmp_path / "o"), "--max-samples", "8", "--title", "T"]) == 0
    assert "sample(s)" in capsys.readouterr().out
    doc = json.loads((tmp_path / "o" / "replay.json").read_text())
    assert doc["title"] == "T" and doc["samples"] <= 8


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_js_decoder_matches_python(stage, tmp_path, root):
    out = tmp_path / "web"
    doc = build_replay(stage, out, chunk_bytes=400)
    c = doc["chunks"][1]
    script = tmp_path / "decode.mjs"
    module = (root / "viewer" / "usd-web-replay.js").as_uri()
    script.write_text(
        f"""import {{ readFileSync }} from "node:fs";
import {{ gunzipSync }} from "node:zlib";
import {{ decodeChunk, floorIndex }} from "{module}";
const doc = JSON.parse(readFileSync(process.argv[2], "utf8"));
const c = decodeChunk(new Uint8Array(gunzipSync(readFileSync(process.argv[3]))), doc);
console.log(JSON.stringify({{first: c.first, count: c.count, floor: [floorIndex([0, 5, 10], 7), floorIndex([0, 5, 10], -1)],
  samples: c.samples.map(s => ({{sets: s.sets.map(e => ({{ids: [...e.ids], q: [...e.q], colors: e.colors ? [...e.colors] : null,
  widths: e.widths ? [...e.widths] : null}})), fields: s.fields.map(f => f ? [...f] : null)}}))}}));
"""
    )
    r = subprocess.run(["node", str(script), str(out / "replay.json"), str(out / c["file"])], capture_output=True, text=True, check=True)
    got = json.loads(r.stdout)
    assert (got["first"], got["count"]) == (c["first"], c["count"]) and got["floor"] == [1, -1]
    want = decode_chunk((out / c["file"]).read_bytes(), doc)
    lo, step = np.asarray(doc["quant"]["min"]), np.asarray(doc["quant"]["step"])
    for gs, ws in zip(got["samples"], want, strict=True):
        for ge, we in zip(gs["sets"], ws["sets"], strict=True):
            assert ge["ids"] == we["ids"].tolist()
            np.testing.assert_allclose(lo + np.asarray(ge["q"]).reshape(-1, 3) * step, we["points"])
            if "colors" in we:
                assert ge["colors"] == we["colors"].ravel().tolist()
        for gf, wf in zip(gs["fields"], ws["fields"], strict=True):
            assert (gf is None) == (wf is None) and (gf is None or gf == wf.ravel().tolist())
