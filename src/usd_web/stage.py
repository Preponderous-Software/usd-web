"""Read a USD stage's ``PointInstancer`` prims into a :class:`~usd_web.surface.Surface`.

Every ``UsdGeom.PointInstancer`` on the stage contributes its instance positions, moved by the
instancer's local-to-world transform and converted to Y-up when the stage is Z-up
(``(x, y, z) -> (x, z, -y)``, applied to whole cells). Each instance is one filled cell whose minimum corner is its
position (the convention of voxel-style exporters); per-instance orientations and scales are
ignored. A prototype's colour is the first ``displayColor`` found on the prototype prim or its
descendants, as composed (so colours inherited from a class prim are seen).
"""

from __future__ import annotations

from typing import Callable, Iterable

import numpy as np
from pxr import Tf, Usd, UsdGeom

from .surface import Palette, Surface

DEFAULT_COLOR = (0.5, 0.5, 0.5)

PrototypeFilter = Callable[[Usd.Prim], bool]


def prototype_color(prim: Usd.Prim, time: Usd.TimeCode = Usd.TimeCode.Default()) -> tuple[float, float, float]:
    for p in Usd.PrimRange(prim):
        gprim = UsdGeom.Gprim(p)
        if not gprim:
            continue
        value = gprim.GetDisplayColorAttr().Get(time)
        if value is not None and len(value):
            c = value[0]
            return (float(c[0]), float(c[1]), float(c[2]))
    return DEFAULT_COLOR


def exclude_names(names: Iterable[str]) -> PrototypeFilter:
    """A filter dropping prototypes whose prim name is in ``names``."""
    banned = frozenset(names)
    return lambda prim: prim.GetName() not in banned


def collect_points(
    stage: Usd.Stage,
    palette: Palette,
    *,
    include: PrototypeFilter | None = None,
    cell_size: float = 1.0,
    time: Usd.TimeCode = Usd.TimeCode.Default(),
) -> tuple[np.ndarray, np.ndarray]:
    """Y-up world minimum corners ``(N, 3)`` and palette indices ``(N,)`` of every instance.

    Converting a Z-up cell to Y-up flips its old Y extent ``[y, y + cell)`` to new Z
    ``(-y - cell, -y]``, so the minimum corner's new Z is ``-y - cell_size``.
    """
    z_up = UsdGeom.GetStageUpAxis(stage) == UsdGeom.Tokens.z
    all_pos, all_idx = [], []
    for prim in stage.Traverse():
        if not prim.IsA(UsdGeom.PointInstancer):
            continue
        inst = UsdGeom.PointInstancer(prim)
        positions = inst.GetPositionsAttr().Get(time)
        indices = inst.GetProtoIndicesAttr().Get(time)
        if positions is None or indices is None or len(positions) == 0:
            continue
        pos = np.asarray(positions, dtype=np.float64).reshape(-1, 3)
        idx = np.asarray(indices, dtype=np.int64).reshape(-1)
        if len(pos) != len(idx):
            raise ValueError(f"{prim.GetPath()}: positions and protoIndices differ in length")
        targets = inst.GetPrototypesRel().GetTargets()
        if not targets:
            continue
        lut = np.full(len(targets), -1, dtype=np.int64)
        for i, target in enumerate(targets):
            proto = stage.GetPrimAtPath(target)
            if not proto or (include is not None and not include(proto)):
                continue
            lut[i] = palette.add(prototype_color(proto, time))
        valid = (idx >= 0) & (idx < len(targets))
        pidx = np.where(valid, lut[np.clip(idx, 0, len(targets) - 1)], -1)
        keep = pidx >= 0
        if not keep.any():
            continue
        pos, pidx = pos[keep], pidx[keep]
        m = np.asarray(UsdGeom.Xformable(prim).ComputeLocalToWorldTransform(time), dtype=np.float64)
        pos = np.c_[pos, np.ones(len(pos))] @ m  # USD uses row vectors: p' = p * M
        pos = pos[:, :3]
        if z_up:
            pos = np.stack([pos[:, 0], pos[:, 2], -pos[:, 1] - float(cell_size)], axis=1)
        all_pos.append(pos)
        all_idx.append(pidx)
    if not all_pos:
        return np.zeros((0, 3)), np.zeros(0, dtype=np.int64)
    return np.concatenate(all_pos), np.concatenate(all_idx)


def surface_from_stage(
    stage: Usd.Stage | str,
    *,
    palette: Palette | None = None,
    include: PrototypeFilter | None = None,
    cell_size: float = 1.0,
    time: Usd.TimeCode = Usd.TimeCode.Default(),
) -> tuple[Surface, Palette]:
    """Open ``stage`` (a path or a stage) and reduce its instancers to a top surface.

    Pass the ``palette`` of an earlier publish to keep colour indices stable across publishes.
    Raises ``ValueError`` if the stage has no instances.
    """
    if not isinstance(stage, Usd.Stage):
        try:
            opened = Usd.Stage.Open(str(stage))
        except Tf.ErrorException as exc:
            raise ValueError(f"cannot open stage {stage}: {str(exc).strip()}") from None
        if opened is None:
            raise ValueError(f"cannot open stage {stage}")
        stage = opened
    palette = palette if palette is not None else Palette()
    pos, idx = collect_points(stage, palette, include=include, cell_size=cell_size, time=time)
    if not len(pos):
        raise ValueError("the stage has no PointInstancer instances")
    return Surface.from_points(pos, idx, cell_size=cell_size), palette


__all__ = ["DEFAULT_COLOR", "collect_points", "exclude_names", "prototype_color", "surface_from_stage"]
