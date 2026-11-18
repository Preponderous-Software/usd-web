import gzip
import struct

import numpy as np
import pytest

from usd_web import MAGIC, Palette, Surface, Tile, build_tiles, tile_version
from usd_web.surface import HOLE
from usd_web.tile import tile_name


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


@pytest.mark.parametrize("width, depth", [(0, 16), (16, 0), (65536, 16), (16, 65536), (-1, 16)])
def test_encode_rejects_bad_tile_size(width, depth):
    with pytest.raises(ValueError, match="width and depth"):
        Tile(0, 0, width, depth, np.zeros((0, 7))).encode()


def test_encode_accepts_extreme_tile_size():
    t = Tile(0, 0, 65535, 1, np.zeros((0, 7)))
    assert Tile.decode(t.encode()) == t


@pytest.mark.parametrize("bad", [-1, 5])
def test_encode_rejects_bad_dir(bad):
    t = sample_tile(3)
    t.quads[1, 0] = bad
    with pytest.raises(ValueError, match="dir must be 0..4"):
        t.encode()


@pytest.mark.parametrize(
    "col, name, value",
    [
        (1, "x", -1),
        (1, "x", 65536),
        (2, "z", 65536),
        (3, "y0", -32769),
        (3, "y0", 32768),
        (4, "du", -1),
        (5, "dv", 65536),
        (6, "color", 65536),
    ],
)
def test_encode_names_the_out_of_range_column(col, name, value):
    t = sample_tile(3)
    t.quads[2, col] = value
    with pytest.raises(ValueError, match=f"column '{name}' out of range"):
        t.encode()


def test_encode_accepts_column_limits():
    q = np.array([[4, 65535, 0, -32768, 65535, 0, 65535], [0, 0, 65535, 32767, 0, 65535, 0]])
    t = Tile(0, 0, 1, 1, q)
    assert Tile.decode(t.encode()) == t


def test_decode_rejects_truncated_header():
    with pytest.raises(ValueError, match="truncated tile header"):
        Tile.decode(b"UWT1" + b"\0" * 15)
    with pytest.raises(ValueError, match="truncated tile header"):
        Tile.decode(gzip.compress(b"UWT1"))


def test_decode_names_the_wrong_magic():
    raw = b"UWT2" + sample_tile(2).encode()[4:]
    with pytest.raises(ValueError, match="not a UWT1 tile"):
        Tile.decode(raw)


def test_decode_rejects_trailing_bytes_and_reports_sizes():
    raw = sample_tile(4).encode()
    with pytest.raises(ValueError, match=f"tile is {len(raw) + 1} bytes, expected {len(raw)}"):
        Tile.decode(raw + b"\0")
    with pytest.raises(ValueError, match=f"tile is {len(raw) - 1} bytes, expected {len(raw)}"):
        Tile.decode(gzip.compress(raw[:-1]))


def test_decode_empty_tile_has_seven_columns():
    t = Tile.decode(Tile(8, -8, 4, 4, np.zeros((0, 7))).to_bytes())
    assert (t.x, t.z, t.width, t.depth) == (8, -8, 4, 4)
    assert t.quads.shape == (0, 7) and t.quads.dtype == np.int64


def test_tile_coerces_flat_quads_and_compares_by_value():
    flat = [0, 1, 2, 3, 4, 5, 6, 1, 0, 0, -2, 1, 3, 7]
    t = Tile(0, 0, 16, 16, flat)
    assert t.quads.shape == (2, 7) and t.quads.dtype == np.int64
    assert t == Tile(0, 0, 16, 16, np.array(flat).reshape(2, 7))
    assert t != Tile(1, 0, 16, 16, flat)
    assert t != Tile(0, 0, 16, 16, flat[:7])
    assert t != "not a tile"


def test_y_range_empty_and_top_vs_side_quads():
    assert Tile(0, 0, 1, 1, np.zeros((0, 7))).y_range == (0, 0)
    # A top quad's dv is a Z extent, so it does not raise the top; a side quad's dv is its height.
    top = [0, 0, 0, 10, 1, 50, 0]
    side = [1, 0, 0, -4, 1, 6, 0]
    assert Tile(0, 0, 1, 1, [top]).y_range == (10, 10)
    assert Tile(0, 0, 1, 1, [top, side]).y_range == (-4, 10)
    assert Tile(0, 0, 1, 1, [[3, 0, 0, 7, 1, 9, 0], top]).y_range == (7, 16)


def test_tile_name_and_version():
    assert tile_name(-1, 0) == "t.-1.0"
    assert tile_name(np.int64(3), 2.0) == "t.3.2"
    assert tile_version(b"") == "e3b0c44298fc"  # SHA-256 of the empty string


@pytest.mark.parametrize(
    "tile_size, chunk_size, message",
    [
        (0, 16, "must be positive"),
        (16, 0, "must be positive"),
        (-16, 16, "must be positive"),
        (65536, 16, "at most 65535"),
    ],
)
def test_build_tiles_rejects_bad_sizes(tile_size, chunk_size, message):
    s = Surface.from_heightmap(np.ones((2, 2)), np.zeros((2, 2), dtype=int))
    with pytest.raises(ValueError, match=message):
        build_tiles(s, tile_size=tile_size, chunk_size=chunk_size)


def test_build_tiles_omits_all_hole_tiles():
    h = np.full((8, 40), 2)
    h[:, 16:32] = HOLE  # the middle 16-wide tile has no column
    s = Surface.from_heightmap(h, np.zeros_like(h))
    assert sorted(build_tiles(s, tile_size=16, chunk_size=16)) == ["t.0.0", "t.2.0"]


def test_build_tiles_places_an_inner_surface_at_its_world_offset():
    s = Surface.from_heightmap(np.full((1, 1), 5), np.zeros((1, 1), dtype=int), origin=(37, -3))
    tiles = build_tiles(s, tile_size=32, chunk_size=16, skirt=0)
    assert sorted(tiles) == ["t.1.-1"]
    t = tiles["t.1.-1"]
    assert (t.x, t.z, t.width, t.depth) == (32, -32, 32, 32)
    tops = t.quads[t.quads[:, 0] == 0]
    assert tops[:, 1:6].tolist() == [[5, 29, 5, 1, 1]]  # world (37, -3) is local (5, 29)
