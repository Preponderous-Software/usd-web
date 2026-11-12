import numpy as np
import pytest

from usd_web import HOLE, Palette, Surface


# --- Palette ---------------------------------------------------------------------------------


def test_palette_normalize_strips_and_lowercases():
    assert Palette.normalize("  #AbCdEf ") == "#abcdef"


@pytest.mark.parametrize("bad", ["#abc", "abcdefg", "#gggggg", "#12345", "#1234567", ""])
def test_palette_normalize_rejects_malformed_strings(bad):
    with pytest.raises(ValueError, match="#rrggbb"):
        Palette.normalize(bad)


def test_palette_rgb_triples_are_clamped_and_extra_channels_ignored():
    assert Palette.normalize((1.5, -0.2, 0.0)) == "#ff0000"
    assert Palette.normalize((0.0, 0.0, 1.0, 0.5)) == "#0000ff"
    assert Palette.normalize(np.array([0.2, 0.6, 0.2])) == "#339933"


def test_palette_add_returns_existing_index_without_growing():
    p = Palette()
    assert p.add("#112233") == 0
    assert p.add("#445566") == 1
    assert p.add("#112233") == 0
    assert len(p) == 2


def test_palette_holds_at_most_65535_colours():
    p = Palette(f"#{i:06x}" for i in range(0xFFFF))
    assert len(p) == 0xFFFF
    # A colour already present is still found once the palette is full.
    assert p.add("#000000") == 0
    with pytest.raises(ValueError, match="65535"):
        p.add("#ffffff")
    assert len(p) == 0xFFFF


def test_palette_equality_and_repr():
    assert Palette(["#aabbcc"]) == Palette(["#AABBCC"])
    assert Palette(["#aabbcc", "#000000"]) != Palette(["#000000", "#aabbcc"])
    assert Palette(["#aabbcc"]) != ["#aabbcc"]
    assert repr(Palette(["#AABBCC"])) == "Palette(['#aabbcc'])"


# --- Surface ---------------------------------------------------------------------------------


def test_surface_coerces_arrays_and_origin():
    s = Surface(
        heights=[[1, 2, 3], [4, 5, HOLE]],
        colors=[[0, 0, 0], [1, 1, 1]],
        subsurface=[[0, 0, 0], [1, 1, 1]],
        origin=(np.int64(-4), 7.0),
    )
    assert s.heights.dtype == np.int32
    assert s.colors.dtype == np.int32
    assert s.subsurface.dtype == np.int32
    assert s.origin == (-4, 7)
    assert all(type(v) is int for v in s.origin)
    assert (s.depth, s.width) == (2, 3)
    assert s.present.tolist() == [[True, True, True], [True, True, False]]


def test_surface_rejects_non_2d_heights():
    with pytest.raises(ValueError, match="2D"):
        Surface(np.zeros(3), np.zeros(3), np.zeros(3))


def test_surface_rejects_mismatched_shapes():
    h = np.zeros((2, 2))
    with pytest.raises(ValueError, match="same shape"):
        Surface(h, np.zeros((2, 3)), np.zeros((2, 2)))
    with pytest.raises(ValueError, match="same shape"):
        Surface(h, np.zeros((2, 2)), np.zeros((3, 2)))


# --- Surface.from_heightmap ------------------------------------------------------------------


def test_from_heightmap_subsurface_defaults_to_colors():
    colors = np.array([[1, 2], [3, 4]])
    s = Surface.from_heightmap(np.ones((2, 2), dtype=int), colors, origin=(5, -6))
    assert s.subsurface.tolist() == colors.tolist()
    assert s.origin == (5, -6)


def test_from_heightmap_keeps_explicit_subsurface():
    s = Surface.from_heightmap(np.ones((1, 2), dtype=int), np.array([[1, 1]]), np.array([[0, 2]]))
    assert s.colors.tolist() == [[1, 1]]
    assert s.subsurface.tolist() == [[0, 2]]


def test_from_heightmap_hole_height_is_a_hole():
    s = Surface.from_heightmap(np.array([[3, HOLE]]), np.zeros((1, 2), dtype=int))
    assert s.heights.tolist() == [[3, HOLE]]
    assert s.present.tolist() == [[True, False]]


def test_from_heightmap_mask_on_integer_heights():
    s = Surface.from_heightmap(
        np.array([[1, 2], [3, 4]]),
        np.zeros((2, 2), dtype=int),
        mask=np.array([[1, 0], [0, 1]]),
    )
    assert s.heights.tolist() == [[1, HOLE], [HOLE, 4]]


@pytest.mark.parametrize("bad", [np.inf, -np.inf, np.nan])
def test_from_heightmap_rejects_non_finite_heights(bad):
    with pytest.raises(ValueError, match="finite"):
        Surface.from_heightmap(np.array([[1.0, bad]]), np.zeros((1, 2), dtype=int))


def test_from_heightmap_rejects_colour_shape_mismatch():
    with pytest.raises(ValueError, match="same shape"):
        Surface.from_heightmap(np.ones((2, 2)), np.zeros((2, 3), dtype=int))


def test_from_heightmap_rejects_negative_colour_indices():
    with pytest.raises(ValueError, match="negative"):
        Surface.from_heightmap(np.ones((1, 2)), np.array([[0, -1]]))


# --- Surface.from_points ---------------------------------------------------------------------


def test_from_points_rejects_length_mismatch():
    with pytest.raises(ValueError, match="differ in length"):
        Surface.from_points(np.zeros((2, 3)), np.zeros(3, dtype=int))


def test_from_points_rejects_no_points():
    with pytest.raises(ValueError, match="no points"):
        Surface.from_points(np.zeros((0, 3)), np.zeros(0, dtype=int))


def test_from_points_rejects_non_finite_positions():
    with pytest.raises(ValueError, match="finite"):
        Surface.from_points(np.array([[0.0, np.nan, 0.0]]), np.array([0]))


def test_from_points_negative_coordinates_set_the_origin():
    pos = np.array([[-3, 0, -5], [-2, 1, -5]], dtype=float)
    s = Surface.from_points(pos, np.array([1, 2]))
    assert s.origin == (-3, -5)
    assert (s.depth, s.width) == (1, 2)
    assert s.heights.tolist() == [[1, 2]]
    assert s.colors.tolist() == [[1, 2]]


def test_from_points_cell_size_scales_and_leaves_holes():
    pos = np.array([[0, 0, 0], [2, 4, 2]], dtype=float)
    s = Surface.from_points(pos, np.array([3, 4]), cell_size=2.0)
    assert s.origin == (0, 0)
    assert s.heights.tolist() == [[1, HOLE], [HOLE, 3]]
    # Hole columns carry colour 0.
    assert s.colors.tolist() == [[3, 0], [0, 4]]


def test_from_points_is_independent_of_input_order():
    pos = np.array([[0, 0, 0], [0, 1, 0], [1, 0, 1], [1, 3, 1], [0, 2, 1]], dtype=float)
    idx = np.array([5, 6, 7, 8, 9])
    a = Surface.from_points(pos, idx)
    perm = np.array([4, 2, 0, 3, 1])
    b = Surface.from_points(pos[perm], idx[perm])
    assert a.heights.tolist() == b.heights.tolist() == [[2, HOLE], [3, 4]]
    assert a.colors.tolist() == b.colors.tolist() == [[6, 0], [9, 8]]
    # Column (0, 0) stacks 1 on 0; column (1, 1) has a gap under its top cell.
    assert a.subsurface.tolist() == b.subsurface.tolist() == [[5, 0], [9, 8]]


def test_from_points_snaps_positions_just_below_a_cell_boundary():
    s = Surface.from_points(np.array([[0.0, 2.9999999, 0.0]]), np.array([0]))
    assert s.heights.tolist() == [[4]]


def test_from_points_rejects_heights_beyond_int32():
    with pytest.raises(ValueError, match="out of range"):
        Surface.from_points(np.array([[0.0, float(2**31 - 1), 0.0]]), np.array([0]))
