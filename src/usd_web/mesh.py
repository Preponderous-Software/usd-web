"""Greedy meshing of a top-surface heightmap into chunk-aligned quads.

Output is an ``(n, 7)`` int64 array, one row per quad, columns
``dir, x, z, y0, du, dv, color`` in tile-local cell coordinates:

======  ===========  ==========================================================
dir     facing       geometry
======  ===========  ==========================================================
0       +Y (top)     plane ``y = y0``; spans ``x .. x+du`` and ``z .. z+dv``
1       +X           plane ``x``;  spans ``z .. z+du`` and ``y0 .. y0+dv``
2       -X           plane ``x``;  spans ``z .. z+du`` and ``y0 .. y0+dv``
3       +Z           plane ``z``;  spans ``x .. x+du`` and ``y0 .. y0+dv``
4       -Z           plane ``z``;  spans ``x .. x+du`` and ``y0 .. y0+dv``
======  ===========  ==========================================================

Top faces merge greedily in both directions; side faces merge along their plane. No quad
crosses a multiple of ``chunk_size`` along X or Z, so every quad belongs to exactly one
``chunk_size`` x ``chunk_size`` cell of the tile - which is what lets a viewer tint quads by a
per-chunk overlay. A side face is split into a one-cell *top band* (top colour) and a *body*
(sub-surface colour). Columns outside the tile, and holes, count as a floor ``skirt`` cells
below the lowest column, so a tile is closed at its edges.
"""

from __future__ import annotations

import numpy as np

from .surface import HOLE

TOP, POS_X, NEG_X, POS_Z, NEG_Z = range(5)
COLUMNS = ("dir", "x", "z", "y0", "du", "dv", "color")


def _runs(key: np.ndarray, brk: int):
    """Run-length encode each row of a 2D int64 ``key`` array, also breaking at every multiple
    of ``brk``. Returns ``(row, start, length, key)`` for runs whose key is not 0."""
    rows, n = key.shape
    if key.size == 0:
        e = np.zeros(0, dtype=np.int64)
        return e, e, e, e
    start = np.ones_like(key, dtype=bool)
    start[:, 1:] = key[:, 1:] != key[:, :-1]
    start[:, ::brk] = True
    r, s = np.nonzero(start)
    flat = r * n + s
    ends = np.append(flat[1:], rows * n)
    length = np.minimum(ends, (r + 1) * n) - flat
    k = key[r, s]
    keep = k != 0
    return r[keep].astype(np.int64), s[keep].astype(np.int64), length[keep].astype(np.int64), k[keep]


def _merge_rows(r, s, length, k, brk: int):
    """Merge identical runs (same start, length and key) on consecutive rows, never across a
    multiple of ``brk``. Returns ``(row, start, length, key, depth)``."""
    if len(r) == 0:
        return r, s, length, k, np.zeros(0, dtype=np.int64)
    order = np.lexsort((r, k, length, s))
    r, s, length, k = r[order], s[order], length[order], k[order]
    new = np.ones(len(r), dtype=bool)
    if len(r) > 1:
        same = (
            (s[1:] == s[:-1])
            & (length[1:] == length[:-1])
            & (k[1:] == k[:-1])
            & (r[1:] == r[:-1] + 1)
            & (r[1:] % brk != 0)
        )
        new[1:] = ~same
    gid = np.cumsum(new) - 1
    first = np.nonzero(new)[0]
    depth = np.bincount(gid)
    return r[first], s[first], length[first], k[first], depth


def _keys(columns: list[np.ndarray], mask: np.ndarray):
    """Dense run keys for the tuples ``columns`` where ``mask``: 0 elsewhere, ``1..`` per
    distinct tuple. Returns ``(key, table)`` with ``table[key - 1]`` the tuple."""
    key = np.zeros(mask.shape, dtype=np.int64)
    if not mask.any():
        return key, np.zeros((0, len(columns)), dtype=np.int64)
    stacked = np.stack([c[mask].astype(np.int64) for c in columns], axis=1)
    table, inverse = np.unique(stacked, axis=0, return_inverse=True)
    key[mask] = inverse.reshape(-1) + 1
    return key, table


def floor_of(heights: np.ndarray, skirt: int = 3) -> int:
    present = heights != HOLE
    return int(heights[present].min()) - int(skirt) if present.any() else 0


def greedy_mesh(
    heights: np.ndarray,
    colors: np.ndarray,
    subsurface: np.ndarray | None = None,
    *,
    chunk_size: int = 16,
    skirt: int = 3,
    floor: int | None = None,
) -> np.ndarray:
    """Greedy-mesh a ``[z, x]`` heightmap (see the module docstring for the output)."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    H = np.asarray(heights, dtype=np.int64)
    C = np.asarray(colors, dtype=np.int64)
    S = C if subsurface is None else np.asarray(subsurface, dtype=np.int64)
    if H.ndim != 2 or C.shape != H.shape or S.shape != H.shape:
        raise ValueError("heights, colors and subsurface must be 2D arrays of one shape")
    present = H != HOLE
    if floor is None:
        floor = floor_of(H, skirt)
    brk = int(chunk_size)
    quads: list[np.ndarray] = []

    key, table = _keys([H, C], present)
    r, s, ln, k = _runs(key, brk)  # rows = z, runs along x
    z0, x0, w, k, d = _merge_rows(r, s, ln, k, brk)
    if len(k):
        hc = table[k - 1]
        quads.append(np.stack([np.full_like(x0, TOP), x0, z0, hc[:, 0], w, d, hc[:, 1]], axis=1))

    for axis in (0, 1):  # 0: planes across x (faces +X / -X); 1: planes across z
        h = H if axis == 0 else H.T
        c = C if axis == 0 else C.T
        sc = S if axis == 0 else S.T
        hp = np.where(h == HOLE, floor, h)
        rows = h.shape[0]
        pad_h = np.full((rows, 1), floor, dtype=np.int64)
        pad_c = np.zeros((rows, 1), dtype=np.int64)
        left = np.concatenate([pad_h, hp], 1)  # column p-1 at plane p
        right = np.concatenate([hp, pad_h], 1)  # column p at plane p
        cl, cr = np.concatenate([pad_c, c], 1), np.concatenate([c, pad_c], 1)
        sl, sr = np.concatenate([pad_c, sc], 1), np.concatenate([sc, pad_c], 1)
        for facing, hi, lo, ctop, csub in ((POS_X, left, right, cl, sl), (NEG_X, right, left, cr, sr)):
            mask = hi > lo
            for band in (0, 1):
                if band == 0:  # top band: one cell, top colour
                    b_lo, b_hi, col = np.maximum(lo, hi - 1), hi, ctop
                else:  # body: sub-surface colour
                    b_lo, b_hi, col = lo, hi - 1, csub
                m = mask & (b_hi > b_lo)
                kk, tab = _keys([b_lo, b_hi, col], m)
                rr, ss, ll, kq = _runs(np.ascontiguousarray(kk.T), brk)  # rr = plane
                if not len(kq):
                    continue
                vals = tab[kq - 1]
                y0, y1, colr = vals[:, 0], vals[:, 1], vals[:, 2]
                dirc = np.full_like(rr, facing if axis == 0 else facing + 2)
                if axis == 0:  # plane x = rr, spans z
                    quads.append(np.stack([dirc, rr, ss, y0, ll, y1 - y0, colr], axis=1))
                else:  # plane z = rr, spans x
                    quads.append(np.stack([dirc, ss, rr, y0, ll, y1 - y0, colr], axis=1))
    if not quads:
        return np.zeros((0, 7), dtype=np.int64)
    q = np.concatenate(quads).astype(np.int64)
    # Deterministic order regardless of numpy's sort internals.
    order = np.lexsort(tuple(q[:, i] for i in range(6, -1, -1)))
    return q[order]


def unit_faces(quads: np.ndarray) -> set[tuple[int, int, int, int, int]]:
    """Expand quads into unit faces ``(dir, a, b, c, color)`` - ``(x, z, y)`` for a top face
    cell and ``(plane, along, y)`` for a side face cell. Used to compare meshes."""
    out: set[tuple[int, int, int, int, int]] = set()
    for dirc, x, z, y0, du, dv, color in np.asarray(quads).tolist():
        if dirc == TOP:
            for i in range(du):
                for j in range(dv):
                    out.add((dirc, x + i, z + j, y0, color))
        elif dirc in (POS_X, NEG_X):
            for i in range(du):
                for j in range(dv):
                    out.add((dirc, x, z + i, y0 + j, color))
        else:
            for i in range(du):
                for j in range(dv):
                    out.add((dirc, z, x + i, y0 + j, color))
    return out


def reference_faces(
    heights: np.ndarray,
    colors: np.ndarray,
    subsurface: np.ndarray | None = None,
    *,
    skirt: int = 3,
    floor: int | None = None,
) -> set[tuple[int, int, int, int, int]]:
    """The faces :func:`greedy_mesh` must cover, computed naively one cell at a time."""
    H = np.asarray(heights, dtype=np.int64)
    C = np.asarray(colors, dtype=np.int64)
    S = C if subsurface is None else np.asarray(subsurface, dtype=np.int64)
    if floor is None:
        floor = floor_of(H, skirt)
    depth, width = H.shape
    out: set[tuple[int, int, int, int, int]] = set()

    def col(x: int, z: int):
        if 0 <= x < width and 0 <= z < depth and H[z, x] != HOLE:
            return int(H[z, x]), int(C[z, x]), int(S[z, x])
        return floor, 0, 0

    for z in range(depth):
        for x in range(width):
            if H[z, x] != HOLE:
                out.add((TOP, x, z, int(H[z, x]), int(C[z, x])))
    for z in range(depth):
        for p in range(width + 1):
            (hl, cl, sl), (hr, cr, sr) = col(p - 1, z), col(p, z)
            for facing, hi, lo, ct, cs in ((POS_X, hl, hr, cl, sl), (NEG_X, hr, hl, cr, sr)):
                for y in range(lo, hi):
                    out.add((facing, p, z, y, ct if y == hi - 1 else cs))
    for x in range(width):
        for p in range(depth + 1):
            (hl, cl, sl), (hr, cr, sr) = col(x, p - 1), col(x, p)
            for facing, hi, lo, ct, cs in ((POS_Z, hl, hr, cl, sl), (NEG_Z, hr, hl, cr, sr)):
                for y in range(lo, hi):
                    out.add((facing, p, x, y, ct if y == hi - 1 else cs))
    return out


__all__ = ["COLUMNS", "NEG_X", "NEG_Z", "POS_X", "POS_Z", "TOP", "floor_of", "greedy_mesh", "reference_faces", "unit_faces"]
