import gzip
import struct

import numpy as np
import pytest

from usd_web import MAGIC, Palette, Surface, Tile, build_tiles, tile_version
from usd_web.surface import HOLE


def sample_tile(n=7):
    rng = np.random.default_rng(n)
    q = np.stack(
        [
            rng.integers(0, 5, n),
            rng.integers(0, 256, n),
            rng.integers(0, 256, n),
            rng.integers(-64, 320, n),
            rng.integers(1, 17, n),
            rng.integers(1, 17, n),
            rng.integers(0, 600, n),
        ],
        axis=1,
    )
    return Tile(-512, 256, 256, 256, q)


@pytest.mark.parametrize("n", [0, 1, 2, 7, 100])
def test_round_trip(n):
    t = sample_tile(n)
    assert Tile.decode(t.encode()) == t
    assert Tile.decode(t.to_bytes()) == t


def test_header_layout():
    t = sample_tile(3)
    raw = t.encode()
    assert raw[:4] == MAGIC == b"UWT1"
    assert struct.unpack_from("<IiiHH", raw, 4) == (3, -512, 256, 256, 256)
    assert len(raw) == 20 + 3 + 1 + 12 * 3  # odd count is padded to 2-byte alignment


def test_gzip_is_deterministic():
    t = sample_tile(50)
    a, b = t.to_bytes(), t.to_bytes()
    assert a == b
    assert gzip.decompress(a) == t.encode()
    assert tile_version(a) == tile_version(b) and len(tile_version(a)) == 12


def test_rejects_out_of_range_and_garbage():
    t = sample_tile(2)
    t.quads[0, 3] = 40000  # y0 is int16
    with pytest.raises(ValueError):
        t.encode()
    with pytest.raises(ValueError):
        Tile.decode(b"NOPE" + b"\0" * 16)
    with pytest.raises(ValueError):
        Tile.decode(sample_tile(4).encode()[:-1])


def test_build_tiles_world_aligned_with_negative_origin():
    h = np.full((40, 40), 3)
    surface = Surface.from_heightmap(h, np.zeros_like(h), origin=(-20, 10))
    tiles = build_tiles(surface, tile_size=32, chunk_size=16)
    assert sorted(tiles) == ["t.-1.0", "t.-1.1", "t.0.0", "t.0.1"]
    assert (tiles["t.-1.0"].x, tiles["t.-1.0"].z) == (-32, 0)
    # Every column appears exactly once as a top face across the tiles.
    top_area = sum(int((t.quads[t.quads[:, 0] == 0][:, 4] * t.quads[t.quads[:, 0] == 0][:, 5]).sum()) for t in tiles.values())
    assert top_area == 40 * 40


def test_build_tiles_checks_sizes():
    s = Surface.from_heightmap(np.ones((2, 2)), np.zeros((2, 2), dtype=int))
    with pytest.raises(ValueError):
        build_tiles(s, tile_size=20, chunk_size=16)


def test_surface_from_points_top_and_subsurface():
    pal = Palette()
    stone, grass = pal.add("#808080"), pal.add((0.2, 0.6, 0.2))
    pos = np.array([[0, 0, 0], [0, 1, 0], [0, 2, 0], [1, 0, 0], [1, 5, 0]], dtype=float)
    idx = np.array([stone, stone, grass, stone, grass])
    s = Surface.from_points(pos, idx)
    assert s.origin == (0, 0) and s.heights.shape == (1, 2)
    assert s.heights.tolist() == [[3, 6]]
    assert s.colors.tolist() == [[grass, grass]]
    # Column 0's top sits on stone; column 1's top floats, so its sub-surface is its own colour.
    assert s.subsurface.tolist() == [[stone, grass]]


def test_palette_is_append_only_and_normalised():
    p = Palette(["#AABBCC"])
    assert p.add("#aabbcc") == 0
    assert p.add((1, 0, 0)) == 1
    assert p.colors == ["#aabbcc", "#ff0000"]
    with pytest.raises(ValueError):
        p.add("red")


def test_heightmap_mask_and_float_heights():
    s = Surface.from_heightmap(np.array([[1.4, 2.6]]), np.array([[0, 0]]), mask=np.array([[True, False]]))
    assert s.heights.tolist() == [[1, HOLE]]
