"""Top-surface column heightmaps: the one intermediate every input is reduced to.

A :class:`Surface` is a grid of columns over the X/Z plane (Y is up). Arrays are indexed
``[z, x]`` relative to ``origin = (x0, z0)``. For every column it holds:

* ``heights`` - the Y of the column's top face (one past the top cell), or :data:`HOLE`;
* ``colors`` - palette index of the top cell;
* ``subsurface`` - palette index of the cell directly under the top one (used for the body of
  side faces), equal to ``colors`` where nothing is known.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np

HOLE = np.iinfo(np.int32).min
"""Height of a column with nothing in it."""


def _hex(rgb: Sequence[float]) -> str:
    r, g, b = (int(round(max(0.0, min(1.0, float(c))) * 255)) for c in rgb[:3])
    return f"#{r:02x}{g:02x}{b:02x}"


class Palette:
    """An append-only list of ``#rrggbb`` colours addressed by index.

    Append-only matters for incremental publishing: tiles already published keep pointing at
    the same indices, so a palette loaded from an existing ``index.json`` may only grow.
    """

    def __init__(self, colors: Iterable[str] = ()):  # noqa: D107
        self.colors: list[str] = []
        self._index: dict[str, int] = {}
        for color in colors:
            self.add(color)

    @staticmethod
    def normalize(color: str | Sequence[float]) -> str:
        if isinstance(color, str):
            text = color.strip().lower()
            if len(text) != 7 or text[0] != "#" or any(c not in "0123456789abcdef" for c in text[1:]):
                raise ValueError(f"colour must be '#rrggbb', got {color!r}")
            return text
        return _hex(color)

    def add(self, color: str | Sequence[float]) -> int:
        """Index of ``color`` (a ``#rrggbb`` string or an RGB triple in [0, 1]), appending it."""
        key = self.normalize(color)
        if key not in self._index:
            if len(self.colors) >= 0xFFFF:
                raise ValueError("a palette holds at most 65535 colours")
            self._index[key] = len(self.colors)
            self.colors.append(key)
        return self._index[key]

    def __len__(self) -> int:
        return len(self.colors)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Palette) and self.colors == other.colors

    def __repr__(self) -> str:
        return f"Palette({self.colors!r})"


@dataclass
class Surface:
    heights: np.ndarray
    colors: np.ndarray
    subsurface: np.ndarray
    origin: tuple[int, int] = (0, 0)

    def __post_init__(self) -> None:
        self.heights = np.asarray(self.heights, dtype=np.int32)
        self.colors = np.asarray(self.colors, dtype=np.int32)
        self.subsurface = np.asarray(self.subsurface, dtype=np.int32)
        if self.heights.ndim != 2:
            raise ValueError("heights must be a 2D [z, x] array")
        if self.colors.shape != self.heights.shape or self.subsurface.shape != self.heights.shape:
            raise ValueError("heights, colors and subsurface must have the same shape")
        self.origin = (int(self.origin[0]), int(self.origin[1]))

    @property
    def depth(self) -> int:  # along Z (rows)
        return int(self.heights.shape[0])

    @property
    def width(self) -> int:  # along X (columns)
        return int(self.heights.shape[1])

    @property
    def present(self) -> np.ndarray:
        return self.heights != HOLE

    @classmethod
    def from_heightmap(
        cls,
        heights: np.ndarray,
        colors: np.ndarray,
        subsurface: np.ndarray | None = None,
        *,
        origin: tuple[int, int] = (0, 0),
        mask: np.ndarray | None = None,
    ) -> "Surface":
        """Wrap plain ``[z, x]`` arrays. ``mask`` (``True`` = present) punches holes; so does a
        height equal to :data:`HOLE`."""
        heights = np.asarray(heights)
        if not np.issubdtype(heights.dtype, np.integer):
            if not np.all(np.isfinite(heights)):
                raise ValueError("heights must be finite")
            heights = np.round(heights)
        heights = heights.astype(np.int64)
        if mask is not None:
            heights = np.where(np.asarray(mask, dtype=bool), heights, HOLE)
        colors = np.asarray(colors)
        if colors.shape != heights.shape:
            raise ValueError("colors must have the same shape as heights")
        if np.any(colors < 0):
            raise ValueError("colour indices must not be negative")
        sub = colors if subsurface is None else np.asarray(subsurface)
        return cls(heights.astype(np.int32), colors, sub, origin)

    @classmethod
    def from_points(
        cls,
        positions: np.ndarray,
        color_indices: np.ndarray,
        *,
        cell_size: float = 1.0,
    ) -> "Surface":
        """Reduce Y-up cell positions (``(N, 3)``, each the minimum corner of a filled cell) to
        their top surface: per (x, z) column the highest cell gives the height and top colour,
        and the cell directly below it (if filled) the sub-surface colour."""
        pos = np.asarray(positions, dtype=np.float64).reshape(-1, 3)
        idx = np.asarray(color_indices, dtype=np.int64).reshape(-1)
        if len(pos) != len(idx):
            raise ValueError("positions and color_indices differ in length")
        if len(pos) == 0:
            raise ValueError("no points")
        if not np.all(np.isfinite(pos)):
            raise ValueError("positions must be finite")
        cells = np.floor(pos / float(cell_size) + 1e-6).astype(np.int64)
        x, y, z = cells[:, 0], cells[:, 1], cells[:, 2]
        x0, z0 = int(x.min()), int(z.min())
        width, depth = int(x.max()) - x0 + 1, int(z.max()) - z0 + 1
        col = (z - z0) * width + (x - x0)
        order = np.lexsort((y, col))
        col_s, y_s, c_s = col[order], y[order], idx[order]
        last = np.ones(len(col_s), dtype=bool)
        last[:-1] = col_s[1:] != col_s[:-1]
        top = np.nonzero(last)[0]
        heights = np.full(width * depth, HOLE, dtype=np.int64)
        colors = np.zeros(width * depth, dtype=np.int64)
        heights[col_s[top]] = y_s[top] + 1
        colors[col_s[top]] = c_s[top]
        sub = colors.copy()
        below = top - 1
        ok = (below >= 0) & (col_s[np.maximum(below, 0)] == col_s[top]) & (y_s[np.maximum(below, 0)] == y_s[top] - 1)
        sub[col_s[top[ok]]] = c_s[below[ok]]
        if heights[heights != HOLE].size and (heights[heights != HOLE].max() > 2**31 - 1):
            raise ValueError("heights out of range")
        return cls(
            heights.reshape(depth, width).astype(np.int32),
            colors.reshape(depth, width),
            sub.reshape(depth, width),
            (x0, z0),
        )


__all__ = ["HOLE", "Palette", "Surface"]
