"""The ``UWT1`` binary tile format, and cutting a :class:`~usd_web.surface.Surface` into tiles.

Byte layout (little-endian; see ARCHITECTURE.md § Tile format)::

    offset  size     field
    0       4        magic  b"UWT1"
    4       4        n      uint32   number of quads
    8       4        x      int32    tile origin X (world cells)
    12      4        z      int32    tile origin Z (world cells)
    16      2        width  uint16   tile size along X (cells)
    18      2        depth  uint16   tile size along Z (cells)
    20      n        dir    uint8[n]
    20+n    n % 2    padding (zero), so the int16 columns are 2-byte aligned
    ...     2n each  x uint16, z uint16, y0 int16, du uint16, dv uint16, color uint16

Columns are stored one after the other (structure of arrays), which compresses well. Files are
gzip-compressed with ``mtime = 0`` so the same tile always produces the same bytes.
"""

from __future__ import annotations

import gzip
import hashlib
import struct
from dataclasses import dataclass

import numpy as np

from .mesh import greedy_mesh
from .surface import HOLE, Surface

MAGIC = b"UWT1"
HEADER = struct.Struct("<4sIiiHH")
_COLUMN_TYPES = (
    ("x", np.dtype("<u2")),
    ("z", np.dtype("<u2")),
    ("y0", np.dtype("<i2")),
    ("du", np.dtype("<u2")),
    ("dv", np.dtype("<u2")),
    ("color", np.dtype("<u2")),
)


@dataclass
class Tile:
    """One tile: an origin, a size, and its quads (``(n, 7)``, see :mod:`usd_web.mesh`)."""

    x: int
    z: int
    width: int
    depth: int
    quads: np.ndarray

    def __post_init__(self) -> None:
        self.quads = np.asarray(self.quads, dtype=np.int64).reshape(-1, 7)

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Tile)
            and (self.x, self.z, self.width, self.depth) == (other.x, other.z, other.width, other.depth)
            and np.array_equal(self.quads, other.quads)
        )

    @property
    def y_range(self) -> tuple[int, int]:
        if not len(self.quads):
            return (0, 0)
        tops = self.quads[:, 3] + np.where(self.quads[:, 0] == 0, 0, self.quads[:, 5])
        return int(self.quads[:, 3].min()), int(tops.max())

    def encode(self) -> bytes:
        """Raw (uncompressed) ``UWT1`` bytes. Raises ``ValueError`` if a value does not fit."""
        q = self.quads
        n = len(q)
        if not (0 < self.width <= 0xFFFF and 0 < self.depth <= 0xFFFF):
            raise ValueError("tile width and depth must be in 1..65535")
        out = [HEADER.pack(MAGIC, n, int(self.x), int(self.z), int(self.width), int(self.depth))]
        dirs = q[:, 0]
        if n and (dirs.min() < 0 or dirs.max() > 4):
            raise ValueError("quad dir must be 0..4")
        out.append(dirs.astype(np.uint8).tobytes())
        if n % 2:
            out.append(b"\0")
        for i, (name, dtype) in enumerate(_COLUMN_TYPES, start=1):
            col = q[:, i]
            info = np.iinfo(dtype)
            if n and (col.min() < info.min or col.max() > info.max):
                raise ValueError(f"quad column {name!r} out of range for {dtype.name}")
            out.append(col.astype(dtype).tobytes())
        return b"".join(out)

    def to_bytes(self) -> bytes:
        """Gzip-compressed, deterministic file bytes."""
        return gzip.compress(self.encode(), compresslevel=9, mtime=0)

    @classmethod
    def decode(cls, data: bytes) -> "Tile":
        """Read raw or gzip-compressed ``UWT1`` bytes."""
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        if len(data) < HEADER.size:
            raise ValueError("truncated tile header")
        magic, n, x, z, width, depth = HEADER.unpack_from(data, 0)
        if magic != MAGIC:
            raise ValueError(f"not a UWT1 tile (magic {magic!r})")
        expected = HEADER.size + n + (n % 2) + 12 * n
        if len(data) != expected:
            raise ValueError(f"tile is {len(data)} bytes, expected {expected}")
        off = HEADER.size
        cols = [np.frombuffer(data, dtype=np.uint8, count=n, offset=off).astype(np.int64)]
        off += n + (n % 2)
        for _name, dtype in _COLUMN_TYPES:
            cols.append(np.frombuffer(data, dtype=dtype, count=n, offset=off).astype(np.int64))
            off += 2 * n
        quads = np.stack(cols, axis=1) if n else np.zeros((0, 7), dtype=np.int64)
        return cls(x, z, width, depth, quads)


def tile_version(data: bytes) -> str:
    """Content version of a tile's file bytes: the first 12 hex digits of SHA-256."""
    return hashlib.sha256(data).hexdigest()[:12]


def tile_name(tx: int, tz: int) -> str:
    return f"t.{int(tx)}.{int(tz)}"


def build_tiles(
    surface: Surface,
    *,
    tile_size: int = 256,
    chunk_size: int = 16,
    skirt: int = 3,
) -> dict[str, Tile]:
    """Cut ``surface`` into world-aligned ``tile_size`` tiles and greedy-mesh each one.

    Tile ``t.<tx>.<tz>`` covers X ``[tx*tile_size, (tx+1)*tile_size)`` and the same along Z.
    ``tile_size`` must be a multiple of ``chunk_size`` so chunk breaks stay world-aligned.
    Tiles with no column are omitted.
    """
    if tile_size <= 0 or chunk_size <= 0:
        raise ValueError("tile_size and chunk_size must be positive")
    if tile_size % chunk_size:
        raise ValueError("tile_size must be a multiple of chunk_size")
    if tile_size > 0xFFFF:
        raise ValueError("tile_size must be at most 65535")
    x0, z0 = surface.origin
    x1, z1 = x0 + surface.width, z0 + surface.depth
    tiles: dict[str, Tile] = {}
    for tz in range(z0 // tile_size, (z1 - 1) // tile_size + 1):
        for tx in range(x0 // tile_size, (x1 - 1) // tile_size + 1):
            ox, oz = tx * tile_size, tz * tile_size
            H = np.full((tile_size, tile_size), HOLE, dtype=np.int64)
            C = np.zeros((tile_size, tile_size), dtype=np.int64)
            S = np.zeros((tile_size, tile_size), dtype=np.int64)
            # Overlap of the tile with the surface, in surface coordinates.
            sx0, sx1 = max(ox, x0) - x0, min(ox + tile_size, x1) - x0
            sz0, sz1 = max(oz, z0) - z0, min(oz + tile_size, z1) - z0
            if sx0 >= sx1 or sz0 >= sz1:
                continue
            tx0, tz0 = sx0 + x0 - ox, sz0 + z0 - oz
            sl_t = (slice(tz0, tz0 + sz1 - sz0), slice(tx0, tx0 + sx1 - sx0))
            sl_s = (slice(sz0, sz1), slice(sx0, sx1))
            H[sl_t] = surface.heights[sl_s]
            C[sl_t] = surface.colors[sl_s]
            S[sl_t] = surface.subsurface[sl_s]
            if not (H != HOLE).any():
                continue
            quads = greedy_mesh(H, C, S, chunk_size=chunk_size, skirt=skirt)
            tiles[tile_name(tx, tz)] = Tile(ox, oz, tile_size, tile_size, quads)
    return tiles


__all__ = ["HEADER", "MAGIC", "Tile", "build_tiles", "tile_name", "tile_version"]
