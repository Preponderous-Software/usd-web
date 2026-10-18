import numpy as np
import pytest

from usd_web.mesh import NEG_X, NEG_Z, POS_X, POS_Z, TOP, greedy_mesh, reference_faces, unit_faces
from usd_web.surface import HOLE


def random_map(rng, depth, width, holes=True):
    h = rng.integers(0, 6, size=(depth, width))
    c = rng.integers(0, 3, size=(depth, width))
    s = rng.integers(0, 3, size=(depth, width))
    if holes:
        h = np.where(rng.random((depth, width)) < 0.1, HOLE, h)
    return h, c, s


@pytest.mark.parametrize("seed", range(12))
def test_greedy_covers_exactly_the_reference_faces(seed):
    rng = np.random.default_rng(seed)
    depth, width = int(rng.integers(1, 40)), int(rng.integers(1, 40))
    h, c, s = random_map(rng, depth, width, holes=seed % 2 == 0)
    if not (h != HOLE).any():
        h[0, 0] = 1
    chunk = int(rng.choice([1, 4, 8, 16]))
    quads = greedy_mesh(h, c, s, chunk_size=chunk)
    faces = unit_faces(quads)
    assert faces == reference_faces(h, c, s)
    # No overlaps: total quad area equals the number of distinct unit faces.
    assert int((quads[:, 4] * quads[:, 5]).sum()) == len(faces)


def test_flat_map_merges_to_one_top_quad_per_chunk():
    h = np.full((32, 32), 5)
    c = np.zeros((32, 32), dtype=int)
    q = greedy_mesh(h, c, chunk_size=16)
    tops = q[q[:, 0] == TOP]
    assert len(tops) == 4
    assert set(map(tuple, tops[:, [1, 2, 4, 5]].tolist())) == {(0, 0, 16, 16), (16, 0, 16, 16), (0, 16, 16, 16), (16, 16, 16, 16)}


@pytest.mark.parametrize("chunk", [4, 8, 16])
def test_no_quad_crosses_a_chunk_boundary(chunk):
    rng = np.random.default_rng(3)
    h = rng.integers(0, 2, size=(48, 48))
    c = np.zeros_like(h)
    q = greedy_mesh(h, c, chunk_size=chunk)
    for dirc, x, z, y0, du, dv, color in q.tolist():
        if dirc == TOP:
            assert x // chunk == (x + du - 1) // chunk
            assert z // chunk == (z + dv - 1) // chunk
        elif dirc in (POS_X, NEG_X):
            assert z // chunk == (z + du - 1) // chunk
        else:
            assert x // chunk == (x + du - 1) // chunk


def test_side_faces_of_a_step():
    # Column x=0 is 5 high, x=1 is 2 high: one +X wall at plane x=1 from y=2 to 5.
    h = np.array([[5, 2]])
    c = np.array([[1, 2]])
    s = np.array([[7, 8]])
    q = greedy_mesh(h, c, s, chunk_size=16, skirt=3)
    step = [row for row in q.tolist() if row[0] == POS_X and row[1] == 1]
    # Top band (one cell, top colour) and body (sub-surface colour).
    assert sorted(step) == sorted([[POS_X, 1, 0, 4, 1, 1, 1], [POS_X, 1, 0, 2, 1, 2, 7]])
    floor = 2 - 3
    # The tile edge walls reach the floor: -X at x=0 spans floor..5.
    west = [row for row in q.tolist() if row[0] == NEG_X and row[1] == 0]
    assert sum(r[5] for r in west) == 5 - floor
    assert all(r[0] != NEG_X or r[1] != 1 for r in q.tolist())  # nothing faces -X between them
    assert {r[0] for r in q.tolist()} == {TOP, POS_X, NEG_X, POS_Z, NEG_Z}


def test_empty_map():
    h = np.full((4, 4), HOLE)
    assert greedy_mesh(h, np.zeros((4, 4), dtype=int)).shape == (0, 7)


def test_deterministic_order():
    rng = np.random.default_rng(9)
    h, c, s = random_map(rng, 20, 20)
    a = greedy_mesh(h, c, s)
    b = greedy_mesh(h.copy(), c.copy(), s.copy())
    assert np.array_equal(a, b)


def test_rejects_bad_input():
    with pytest.raises(ValueError):
        greedy_mesh(np.zeros((2, 2)), np.zeros((2, 3)))
    with pytest.raises(ValueError):
        greedy_mesh(np.zeros((2, 2)), np.zeros((2, 2)), chunk_size=0)
