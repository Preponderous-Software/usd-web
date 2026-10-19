"""usd-web: OpenUSD stages and heightmaps to greedy-meshed web tiles, plus a three.js viewer."""

from .mesh import greedy_mesh
from .publish import INDEX_FORMAT, INDEX_VERSION, atomic_write_bytes, publish, read_index, validate_index
from .surface import HOLE, Palette, Surface
from .tile import MAGIC, Tile, build_tiles, tile_version

__version__ = "0.2.0"

__all__ = [
    "HOLE",
    "INDEX_FORMAT",
    "INDEX_VERSION",
    "MAGIC",
    "Palette",
    "Surface",
    "Tile",
    "__version__",
    "atomic_write_bytes",
    "build_tiles",
    "greedy_mesh",
    "publish",
    "read_index",
    "surface_from_stage",
    "tile_version",
    "validate_index",
]


def __getattr__(name: str):
    # Lazy: importing pxr is slow, and the heightmap path does not need it.
    if name == "surface_from_stage":
        from .stage import surface_from_stage

        return surface_from_stage
    raise AttributeError(name)
