import numpy as np
import pytest
from pxr import Gf, Sdf, Usd, UsdGeom, Vt

from usd_web import Palette, build_tiles
from usd_web.stage import exclude_names, surface_from_stage

CELLS = [(0, 0, 0), (0, 1, 0), (1, 0, 0), (2, 0, 1), (0, 0, 3)]  # Y-up minimum corners


def make_stage(up: str, translate=(0.0, 0.0, 0.0), with_class=True) -> Usd.Stage:
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.y if up == "Y" else UsdGeom.Tokens.z)
    world = UsdGeom.Xform.Define(stage, "/World")
    world.AddTranslateOp().Set(Gf.Vec3d(*translate))
    # Colours come from a class prim the prototypes inherit (composition must be honoured).
    lib = stage.CreateClassPrim("/Library")
    for name, rgb in (("stone", (0.5, 0.5, 0.5)), ("grass", (0.2, 0.6, 0.2)), ("flower", (1.0, 0.0, 0.0))):
        cube = UsdGeom.Cube.Define(stage, lib.GetPath().AppendChild(name))
        cube.CreateDisplayColorAttr(Vt.Vec3fArray([Gf.Vec3f(*rgb)]))
    inst = UsdGeom.PointInstancer.Define(stage, "/World/Blocks")
    protos = []
    for name in ("stone", "grass", "flower"):
        p = stage.DefinePrim(f"/World/Blocks/Prototypes/{name}")
        if with_class:
            p.GetInherits().AddInherit(f"/Library/{name}")
        protos.append(p.GetPath())
    inst.CreatePrototypesRel().SetTargets(protos)
    if up == "Y":
        pts = CELLS
    else:  # the same cells authored Z-up: Y-up (x, y, z) is Z-up (x, -z - 1, y)
        pts = [(x, -z - 1, y) for x, y, z in CELLS]
    inst.CreatePositionsAttr(Vt.Vec3fArray([Gf.Vec3f(*p) for p in pts]))
    # (0,0,0) stone, (0,1,0) grass on top, (1,0,0) stone, (2,0,1) flower, (0,0,3) grass
    inst.CreateProtoIndicesAttr(Vt.IntArray([0, 1, 0, 2, 1]))
    return stage


def as_dict(surface, palette):
    out = {}
    x0, z0 = surface.origin
    for z in range(surface.depth):
        for x in range(surface.width):
            if surface.heights[z, x] != np.iinfo(np.int32).min:
                out[(x + x0, z + z0)] = (int(surface.heights[z, x]), palette.colors[surface.colors[z, x]], palette.colors[surface.subsurface[z, x]])
    return out


EXPECTED = {
    (0, 0): (2, "#339933", "#808080"),
    (1, 0): (1, "#808080", "#808080"),
    (2, 1): (1, "#ff0000", "#ff0000"),
    (0, 3): (1, "#339933", "#339933"),
}


@pytest.mark.parametrize("up", ["Y", "Z"])
def test_y_up_and_z_up_stages_give_the_same_surface(up):
    surface, palette = surface_from_stage(make_stage(up))
    assert as_dict(surface, palette) == EXPECTED


def test_world_transform_is_applied():
    surface, palette = surface_from_stage(make_stage("Y", translate=(10, 2, -5)))
    got = as_dict(surface, palette)
    assert got[(10, -5)] == (4, "#339933", "#808080")
    assert len(got) == 4


def test_prototype_filter_drops_instances():
    surface, palette = surface_from_stage(make_stage("Y"), include=exclude_names({"flower"}))
    assert (2, 1) not in as_dict(surface, palette)
    assert "#ff0000" not in palette.colors


def test_missing_colour_falls_back_to_grey():
    surface, palette = surface_from_stage(make_stage("Y", with_class=False))
    assert palette.colors == ["#808080"]


def test_existing_palette_keeps_indices():
    pal = Palette(["#123456"])
    surface, palette = surface_from_stage(make_stage("Y"), palette=pal)
    assert palette is pal and palette.colors[0] == "#123456"
    assert set(surface.colors[surface.heights != np.iinfo(np.int32).min].tolist()) <= set(range(1, len(pal)))


def test_stage_from_file_and_tiles(tmp_path):
    path = tmp_path / "s.usda"
    stage = make_stage("Z")  # keep the stage alive while its layer is exported
    stage.GetRootLayer().Export(str(path))
    surface, palette = surface_from_stage(path)
    tiles = build_tiles(surface, tile_size=16, chunk_size=16)
    assert sorted(tiles) == ["t.0.0"]


def test_empty_stage_is_an_error():
    with pytest.raises(ValueError):
        surface_from_stage(Usd.Stage.CreateInMemory())


def test_instancer_without_prototypes_is_skipped():
    stage = make_stage("Y")
    bare = UsdGeom.PointInstancer.Define(stage, "/World/Bare")
    bare.CreatePositionsAttr(Vt.Vec3fArray([Gf.Vec3f(50, 0, 50)]))
    bare.CreateProtoIndicesAttr(Vt.IntArray([0]))
    surface, palette = surface_from_stage(stage)
    assert as_dict(surface, palette) == EXPECTED


def blocks(stage) -> UsdGeom.PointInstancer:
    return UsdGeom.PointInstancer(stage.GetPrimAtPath("/World/Blocks"))


def test_out_of_range_proto_indices_are_dropped():
    stage = make_stage("Y")
    blocks(stage).GetPositionsAttr().Set(Vt.Vec3fArray([Gf.Vec3f(*p) for p in CELLS + [(50, 0, 50), (60, 0, 60)]]))
    blocks(stage).GetProtoIndicesAttr().Set(Vt.IntArray([0, 1, 0, 2, 1, 7, -1]))
    surface, palette = surface_from_stage(stage)
    assert as_dict(surface, palette) == EXPECTED


def test_positions_and_proto_indices_must_match_in_length():
    stage = make_stage("Y")
    blocks(stage).GetProtoIndicesAttr().Set(Vt.IntArray([0]))
    with pytest.raises(ValueError, match="/World/Blocks: positions and protoIndices differ"):
        surface_from_stage(stage)


def test_unopenable_path_is_a_value_error(tmp_path):
    with pytest.raises(ValueError, match="cannot open stage"):
        surface_from_stage(tmp_path / "missing.usda")


def test_filtering_out_every_prototype_is_an_error():
    with pytest.raises(ValueError, match="no PointInstancer instances"):
        surface_from_stage(make_stage("Y"), include=lambda prim: False)


def test_cell_size_scales_positions_and_the_z_up_offset():
    stage = make_stage("Z")
    # The same cells as CELLS, authored Z-up in units of 2: Y-up (x, y, z) is Z-up (2x, -2z - 2, 2y).
    blocks(stage).GetPositionsAttr().Set(Vt.Vec3fArray([Gf.Vec3f(2 * x, -2 * z - 2, 2 * y) for x, y, z in CELLS]))
    surface, palette = surface_from_stage(stage, cell_size=2.0)
    assert as_dict(surface, palette) == EXPECTED


def test_colour_is_found_on_a_prototype_descendant():
    stage = make_stage("Y", with_class=False)
    cube = UsdGeom.Cube.Define(stage, "/World/Blocks/Prototypes/flower/Body")
    cube.CreateDisplayColorAttr(Vt.Vec3fArray([Gf.Vec3f(1.0, 0.0, 0.0)]))
    surface, palette = surface_from_stage(stage)
    got = as_dict(surface, palette)
    assert got[(2, 1)] == (1, "#ff0000", "#ff0000")
    assert got[(1, 0)] == (1, "#808080", "#808080")


def test_time_code_selects_sampled_positions():
    stage = make_stage("Y")
    shifted = Vt.Vec3fArray([Gf.Vec3f(x + 5, y, z) for x, y, z in CELLS])
    blocks(stage).GetPositionsAttr().Set(shifted, Usd.TimeCode(10))
    default, palette = surface_from_stage(stage)
    assert as_dict(default, palette) == EXPECTED
    sampled, palette = surface_from_stage(stage, time=Usd.TimeCode(10))
    assert as_dict(sampled, palette) == {(x + 5, z): v for (x, z), v in EXPECTED.items()}


def test_several_instancers_share_one_surface_and_palette():
    stage = make_stage("Y")
    more = UsdGeom.PointInstancer.Define(stage, "/World/More")
    more.CreatePrototypesRel().SetTargets([Sdf.Path("/World/Blocks/Prototypes/stone")])
    more.CreatePositionsAttr(Vt.Vec3fArray([Gf.Vec3f(5, 0, 5)]))
    more.CreateProtoIndicesAttr(Vt.IntArray([0]))
    surface, palette = surface_from_stage(stage)
    assert as_dict(surface, palette) == {**EXPECTED, (5, 5): (1, "#808080", "#808080")}
    assert palette.colors.count("#808080") == 1
