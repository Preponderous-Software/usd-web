"""Replays: a ``usd-replay/1`` stage (see usd-recorder's docs/REPLAY.md) to a web replay that
``viewer/usd-web-replay.js`` plays.

Output (ARCHITECTURE.md § Replays)::

    replay.json                 format "usd-web-replay", metadata, sample ticks and hashes, sets,
                                fields, stats, static scene, chunk list (written last)
    r.<first>.<hash>.uwr        gzip'd UWR1 chunks of consecutive samples, content-addressed

Positions are quantized to uint16 over the replay's bounds, entity ids are renumbered densely per
set, colours are sRGB bytes. The stage's own sample ticks and state hashes are carried through,
so a player can show the hash of the tick on screen.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .publish import atomic_write_bytes

REPLAY_SCHEMA = "usd-replay/1"
REPLAY_FORMAT = "usd-web-replay"
REPLAY_VERSION = 1
REPLAY_INDEX = "replay.json"
CHUNK_MAGIC = b"UWR1"
CHUNK_SUFFIX = ".uwr"


def _srgb8(lin: Any) -> np.ndarray:
    a = np.clip(np.asarray(lin, dtype=np.float64), 0.0, 1.0)
    s = np.where(a <= 0.0031308, a * 12.92, 1.055 * a ** (1 / 2.4) - 0.055)
    return np.round(s * 255.0).astype(np.uint8)


def _hex(rgb8: Any) -> str:
    r, g, b = (int(v) for v in np.asarray(rgb8).ravel()[:3])
    return f"#{r:02x}{g:02x}{b:02x}"


@dataclass
class _Set:
    name: str
    label: str
    shape: str
    color: str | None
    width: float | None
    ids: list[np.ndarray]
    points: list[np.ndarray]
    colors: list[np.ndarray] | None
    widths: list[np.ndarray] | None


@dataclass
class _Field:
    name: str
    label: str
    cols: int
    rows: int
    origin: tuple[float, float]
    cell_size: float
    z: float
    ticks: list[int]
    colors: list[np.ndarray]  # sRGB bytes (cols*rows, 3)


def _string(prim, name: str, default: str) -> str:
    attr = prim.GetAttribute(name)
    value = attr.Get() if attr else None
    return str(value) if value else default


def read_stage(path: str | Path) -> dict[str, Any]:
    """Read a ``usd-replay/1`` stage into plain arrays. Raises ``ValueError`` on anything that is
    not a replay (run ``usd-recorder replay validate`` for the full rule check)."""
    from pxr import Sdf, Usd, UsdGeom

    path = Path(path)
    layer = Sdf.Layer.FindOrOpen(str(path))
    if layer is None:
        raise ValueError(f"cannot open {path}")
    meta = layer.customLayerData.get("usdReplay")
    if not isinstance(meta, dict) or meta.get("schema") != REPLAY_SCHEMA:
        raise ValueError(f"{path} is not a {REPLAY_SCHEMA} replay (customLayerData.usdReplay.schema)")
    stage = Usd.Stage.Open(layer)
    if UsdGeom.GetStageUpAxis(stage) != UsdGeom.Tokens.z:
        raise ValueError("a replay is Z-up")
    root = stage.GetPrimAtPath("/Replay")
    hash_attr = root.GetAttribute("replay:stateHash") if root else None
    if not hash_attr:
        raise ValueError("missing /Replay.replay:stateHash")
    ticks = [int(t) for t in hash_attr.GetTimeSamples()]
    if not ticks:
        raise ValueError("a replay needs at least one sample")
    out: dict[str, Any] = {
        "meta": dict(meta),
        "playbackRate": stage.GetTimeCodesPerSecond(),
        "ticks": ticks,
        "hashes": [str(hash_attr.Get(t)) for t in ticks],
        "sets": [],
        "fields": [],
        "stats": {},
        "scene": [],
    }
    ents = stage.GetPrimAtPath("/Replay/Entities")
    for prim in ents.GetChildren() if ents else []:
        pts = UsdGeom.Points(prim)
        pv = UsdGeom.PrimvarsAPI(prim).GetPrimvar("displayColor")
        vc = bool(pv) and pv.GetInterpolation() == UsdGeom.Tokens.vertex
        vw = pts.GetWidthsInterpolation() == UsdGeom.Tokens.vertex and pts.GetWidthsAttr().GetNumTimeSamples() > 0
        ids = [np.asarray(pts.GetIdsAttr().Get(t) or [], dtype=np.int64) for t in ticks]
        points = [np.asarray(pts.GetPointsAttr().Get(t) or [], dtype=np.float64).reshape(-1, 3) for t in ticks]
        for k, t in enumerate(ticks):
            if len(ids[k]) != len(points[k]):
                raise ValueError(f"{prim.GetPath()}@{t}: {len(ids[k])} ids but {len(points[k])} points")
        const_color = None
        if not vc:
            c = pv.Get() if pv else None
            const_color = _hex(_srgb8(np.asarray(c, dtype=np.float64).reshape(-1, 3)[0])) if c else "#9aa6b2"
        w = None if vw else (pts.GetWidthsAttr().Get() or [1.0])
        out["sets"].append(
            _Set(
                name=prim.GetName(),
                label=_string(prim, "replay:label", prim.GetName()),
                shape=_string(prim, "replay:shape", "sphere"),
                color=const_color,
                width=None if w is None else float(w[0]),
                ids=ids,
                points=points,
                colors=[_srgb8(np.asarray(pv.Get(t) or [], dtype=np.float64).reshape(-1, 3)) for t in ticks] if vc else None,
                widths=[np.asarray(pts.GetWidthsAttr().Get(t) or [], dtype=np.float64) for t in ticks] if vw else None,
            )
        )
    flds = stage.GetPrimAtPath("/Replay/Fields")
    for prim in flds.GetChildren() if flds else []:
        cols, rows = prim.GetAttribute("replay:field:size").Get()
        origin = prim.GetAttribute("replay:field:origin").Get()
        pv = UsdGeom.PrimvarsAPI(prim).GetPrimvar("displayColor")
        fticks = [int(t) for t in pv.GetAttr().GetTimeSamples()]
        mesh_pts = UsdGeom.Mesh(prim).GetPointsAttr().Get()
        colors = [_srgb8(np.asarray(pv.Get(t), dtype=np.float64).reshape(-1, 3)) for t in fticks]
        if any(len(c) != cols * rows for c in colors):
            raise ValueError(f"{prim.GetPath()}: a sample does not have cols*rows colours")
        out["fields"].append(
            _Field(
                name=prim.GetName(),
                label=_string(prim, "replay:label", prim.GetName()),
                cols=int(cols),
                rows=int(rows),
                origin=(float(origin[0]), float(origin[1])),
                cell_size=float(prim.GetAttribute("replay:field:cellSize").Get()),
                z=float(mesh_pts[0][2]) if mesh_pts else 0.0,
                ticks=fticks,
                colors=colors,
            )
        )
    for attr in root.GetAttributes():
        name = attr.GetName()
        if name.startswith("replay:stat:"):
            ts = [int(t) for t in attr.GetTimeSamples()]
            out["stats"][name[len("replay:stat:") :]] = (ts, [float(attr.Get(t)) for t in ts])
    scene = stage.GetPrimAtPath("/Replay/Scene")
    for prim in scene.GetChildren() if scene else []:
        pts = UsdGeom.Points(prim)
        p = np.asarray(pts.GetPointsAttr().Get() or [], dtype=np.float64).reshape(-1, 3)
        pv = UsdGeom.PrimvarsAPI(prim).GetPrimvar("displayColor")
        c = _srgb8(np.asarray(pv.Get() if pv and pv.Get() else [(0.5, 0.5, 0.5)], dtype=np.float64).reshape(-1, 3))
        w = np.asarray(pts.GetWidthsAttr().Get() or [1.0], dtype=np.float64)
        out["scene"].append(
            {
                "id": prim.GetName(),
                "label": _string(prim, "replay:label", prim.GetName()),
                "shape": _string(prim, "replay:shape", "sphere"),
                "points": [[round(float(v), 4) for v in row] for row in p],
                "colors": [_hex(row) for row in c],
                "widths": [round(float(v), 4) for v in w],
            }
        )
    return out


def _held(ticks: list[int], values: list, tick: int):
    """The value sampled at the latest tick <= ``tick`` (index into ``values``), or None."""
    k = int(np.searchsorted(np.asarray(ticks), tick, side="right")) - 1
    return None if k < 0 else k


def _round(v: float) -> float:
    return float(f"{v:.6g}")


def build_replay(
    stage_path: str | Path,
    out_dir: str | Path,
    *,
    every: int = 1,
    max_samples: int | None = None,
    chunk_bytes: int = 1 << 20,
    title: str | None = None,
    view: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Convert ``stage_path`` into ``out_dir`` and return the ``replay.json`` document.

    ``every`` keeps one sample in N (and always the last); ``max_samples`` raises ``every`` until
    at most that many samples remain. Chunks hold about ``chunk_bytes`` of raw sample data each.
    Files of a previous conversion in ``out_dir`` that the new index does not name are removed.
    """
    if every < 1:
        raise ValueError("every must be >= 1")
    data = read_stage(stage_path)
    all_ticks = data["ticks"]
    if max_samples is not None:
        if max_samples < 2:
            raise ValueError("max_samples must be >= 2")
        every = max(every, math.ceil((len(all_ticks) - 1) / (max_samples - 1)))
    keep = list(range(0, len(all_ticks), every))
    if keep[-1] != len(all_ticks) - 1:
        keep.append(len(all_ticks) - 1)
    ticks = [all_ticks[k] for k in keep]

    sets: list[_Set] = data["sets"]
    fields: list[_Field] = data["fields"]

    # Bounds over everything drawn: kept samples, fields and scene points.
    lo = np.full(3, np.inf)
    hi = np.full(3, -np.inf)
    for s in sets:
        for k in keep:
            if len(s.points[k]):
                lo = np.minimum(lo, s.points[k].min(axis=0))
                hi = np.maximum(hi, s.points[k].max(axis=0))
    for f in fields:
        lo = np.minimum(lo, [f.origin[0], f.origin[1], f.z])
        hi = np.maximum(hi, [f.origin[0] + f.cols * f.cell_size, f.origin[1] + f.rows * f.cell_size, f.z])
    for sc in data["scene"]:
        if sc["points"]:
            p = np.asarray(sc["points"])
            lo = np.minimum(lo, p.min(axis=0))
            hi = np.maximum(hi, p.max(axis=0))
    if not np.all(np.isfinite(lo)):
        lo, hi = np.zeros(3), np.ones(3)
    lo = np.array([_round(v) for v in lo])
    hi = np.array([_round(v) for v in hi])
    span = hi - lo
    step = np.where(span > 0, span / 65535.0, 0.0)

    # Dense ids per set, in first-seen order over the kept samples.
    id_maps = []
    for s in sets:
        m: dict[int, int] = {}
        for k in keep:
            for i in s.ids[k].tolist():
                if i not in m:
                    m[i] = len(m)
        id_maps.append(m)
    id_bytes = 2 if max((len(m) for m in id_maps), default=0) <= 65536 else 4
    width_max = []
    for s in sets:
        wm = max((float(s.widths[k].max()) for k in keep if s.widths and len(s.widths[k])), default=1.0) if s.widths else 0.0
        width_max.append(wm if wm > 0 else 1.0)

    def encode_sample(k: int, prev_field: list[int | None]) -> tuple[bytes, list[int | None]]:
        parts: list[bytes] = []
        for si, s in enumerate(sets):
            ids = s.ids[k]
            n = len(ids)
            parts.append(struct.pack("<I", n))
            m = id_maps[si]
            dense = np.fromiter((m[i] for i in ids.tolist()), dtype=np.uint32 if id_bytes == 4 else np.uint16, count=n)
            parts.append(dense.astype("<u4" if id_bytes == 4 else "<u2").tobytes())
            q = np.zeros((n, 3), dtype="<u2")
            if n:
                with np.errstate(invalid="ignore", divide="ignore"):
                    qq = np.where(step > 0, np.round((s.points[k] - lo) / np.where(step > 0, step, 1)), 0)
                q = np.clip(qq, 0, 65535).astype("<u2")
            parts.append(q.tobytes())
            if s.colors is not None:
                parts.append(np.asarray(s.colors[k], dtype=np.uint8).reshape(-1, 3).tobytes())
            if s.widths is not None:
                wq = np.clip(np.round(s.widths[k] / width_max[si] * 255), 0, 255).astype(np.uint8)
                parts.append(wq.tobytes())
        nxt = list(prev_field)
        for fi, f in enumerate(fields):
            h = _held(f.ticks, f.colors, all_ticks[k])
            if h is not None and h != prev_field[fi]:
                parts.append(b"\x01")
                parts.append(f.colors[h].tobytes())
                nxt[fi] = h
            else:
                parts.append(b"\x00")
        return b"".join(parts), nxt

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    chunks: list[dict[str, Any]] = []
    prev: list[int | None] = [None] * len(fields)
    field_ticks: list[list[int]] = [[] for _ in fields]
    buf: list[bytes] = []
    first = 0
    size = 0

    def flush(end: int) -> None:
        nonlocal buf, size, first
        if not buf:
            return
        raw = CHUNK_MAGIC + struct.pack("<II", end - first, first) + b"".join(buf)
        digest = hashlib.sha256(raw).hexdigest()[:12]
        name = f"r.{first:06d}.{digest}{CHUNK_SUFFIX}"
        payload = gzip.compress(raw, compresslevel=9, mtime=0)
        atomic_write_bytes(out_dir / name, payload)
        chunks.append({"file": name, "first": first, "count": end - first, "bytes": len(payload), "raw": len(raw)})
        buf, size, first = [], 0, end

    for j, k in enumerate(keep):
        before = list(prev)
        blob, prev = encode_sample(k, prev)
        for fi in range(len(fields)):
            if prev[fi] != before[fi]:
                field_ticks[fi].append(ticks[j])
        buf.append(blob)
        size += len(blob)
        if size >= chunk_bytes:
            flush(j + 1)
            prev = [None] * len(fields)  # a chunk decodes on its own: the next one restates each field
    flush(len(keep))

    meta = data["meta"]
    stats = {}
    for name, (sts, vals) in sorted(data["stats"].items()):
        series = []
        for t in ticks:
            h = _held(sts, vals, t)
            series.append(None if h is None else _round(vals[h]))
        stats[name] = series
    doc: dict[str, Any] = {
        "format": REPLAY_FORMAT,
        "version": REPLAY_VERSION,
        "title": title or meta.get("title") or meta.get("sim", "replay"),
        "meta": {k: v for k, v in meta.items() if isinstance(v, str | int | float | bool | dict)},
        "playbackRate": _round(data["playbackRate"]),
        "samples": len(ticks),
        "ticks": ticks,
        "hashes": [data["hashes"][k] for k in keep],
        "keptEvery": every,
        "sourceSamples": len(all_ticks),
        "bounds": {"min": lo.tolist(), "max": hi.tolist()},
        "quant": {"min": lo.tolist(), "step": [float(v) for v in step], "idBytes": id_bytes},
        "sets": [
            {
                "id": s.name,
                "label": s.label,
                "shape": s.shape if s.shape in ("sphere", "cube") else "sphere",
                "color": s.color,
                "width": s.width,
                "vertexColor": s.colors is not None,
                "vertexWidth": s.widths is not None,
                "widthMax": width_max[i] if s.widths is not None else None,
                "maxCount": max((len(s.ids[k]) for k in keep), default=0),
                "ids": len(id_maps[i]),
            }
            for i, s in enumerate(sets)
        ],
        "fields": [
            {
                "id": f.name,
                "label": f.label,
                "cols": f.cols,
                "rows": f.rows,
                "origin": [f.origin[0], f.origin[1]],
                "cellSize": f.cell_size,
                "z": f.z,
                "ticks": field_ticks[i],
            }
            for i, f in enumerate(fields)
        ],
        "stats": stats,
        "scene": data["scene"],
        "chunks": chunks,
    }
    if view:
        doc["view"] = view
    atomic_write_bytes(out_dir / REPLAY_INDEX, (json.dumps(doc, separators=(",", ":")) + "\n").encode())
    names = {c["file"] for c in chunks}
    for old in out_dir.glob(f"r.*{CHUNK_SUFFIX}"):
        if old.name not in names:
            old.unlink()
    return doc


def decode_chunk(raw: bytes, doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Decode one (gunzipped or gzip'd) UWR1 chunk against its ``replay.json`` (tests, tools)."""
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    if raw[:4] != CHUNK_MAGIC:
        raise ValueError("not a UWR1 chunk")
    count, first = struct.unpack_from("<II", raw, 4)
    off = 12
    lo = np.asarray(doc["quant"]["min"])
    step = np.asarray(doc["quant"]["step"])
    id_dtype = "<u4" if doc["quant"]["idBytes"] == 4 else "<u2"
    id_size = 4 if doc["quant"]["idBytes"] == 4 else 2
    samples = []
    for _ in range(count):
        sample: dict[str, Any] = {"sets": [], "fields": []}
        for s in doc["sets"]:
            (n,) = struct.unpack_from("<I", raw, off)
            off += 4
            ids = np.frombuffer(raw, dtype=id_dtype, count=n, offset=off).astype(np.int64)
            off += id_size * n
            q = np.frombuffer(raw, dtype="<u2", count=3 * n, offset=off).reshape(n, 3)
            off += 6 * n
            entry: dict[str, Any] = {"ids": ids, "points": lo + q * step}
            if s["vertexColor"]:
                entry["colors"] = np.frombuffer(raw, dtype=np.uint8, count=3 * n, offset=off).reshape(n, 3)
                off += 3 * n
            if s["vertexWidth"]:
                entry["widths"] = np.frombuffer(raw, dtype=np.uint8, count=n, offset=off) / 255.0 * s["widthMax"]
                off += n
            sample["sets"].append(entry)
        for f in doc["fields"]:
            present = raw[off]
            off += 1
            if present:
                k = f["cols"] * f["rows"] * 3
                sample["fields"].append(np.frombuffer(raw, dtype=np.uint8, count=k, offset=off).reshape(-1, 3))
                off += k
            else:
                sample["fields"].append(None)
        samples.append(sample)
    if off != len(raw):
        raise ValueError(f"chunk has {len(raw) - off} trailing bytes")
    return samples


__all__ = ["REPLAY_FORMAT", "REPLAY_VERSION", "build_replay", "decode_chunk", "read_stage"]
