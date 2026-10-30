# Architecture

```text
src/usd_web/
  surface.py   Surface (top-surface columns) and Palette (append-only colours)
  stage.py     USD stage -> Surface: PointInstancers, world transforms, up axis, prototype colours
  mesh.py      greedy mesher (+ a naive reference used by the tests)
  tile.py      UWT1 encode/decode, build_tiles (world-aligned tiling)
  publish.py   atomic publish, index.json schema + validation, pruning
  replay.py    usd-replay/1 stage -> replay.json + UWR1 chunks (and a Python chunk decoder)
  cli.py       usd-web tiles | heightmap | replay | inspect
viewer/
  usd-web-viewer.js   dependency-free ES module (three.js passed in)
  index.html          demo / drop-in page (pins three@0.160.0)
  usd-web-replay.js   replay player module (three.js passed in)
  replay.html         replay page: scrubber, play/pause, speed, stats, state hash
examples/make_demo.py synthetic demo tile set
docs/capture_readme.py, docs/capture_replay.py   README screenshots/GIFs (Playwright + Pillow)
```

## Coordinates

Everything after ingestion is **Y-up**, in integer **cells**. A `Surface` is indexed `[z, x]`
from `origin = (x0, z0)`; `heights[z, x]` is the Y of the column's top face (one past the top
cell) or `HOLE` (`int32` minimum). Each column also has a top colour and a sub-surface colour
(the cell under the top one), both palette indices.

## Stage ingestion (`stage.py`)

- Every prim that `IsA(UsdGeom.PointInstancer)` contributes `positions[i]` for each valid
  `protoIndices[i]`, transformed by the instancer's `ComputeLocalToWorldTransform` (row vectors:
  `p' = p · M`). Orientations and scales are ignored: each instance is one cell whose minimum
  corner is its position (the convention of voxel exporters). Positions are floored after a
  `1e-6` nudge.
- A **Z-up** stage is converted with `(x, y, z) → (x, z, −y − cell)`: rotating the cell
  `[y, y+1)` gives `(−y−1, −y]`, whose minimum corner is `−y − 1`.
- Prototype colour: the first `displayColor` on the prototype or its descendants, **as
  composed** (prototypes that inherit a class prim get its colour). Missing → grey.
- `include(prim) -> bool` filters prototypes (`exclude_names({...})` for the common case,
  e.g. small decorative blocks that would speckle the surface and break greedy merging).
- Top surface (`Surface.from_points`): sort by `(column, y)`; the last cell per column is the
  top; the cell before it, if directly below, gives the sub-surface colour.

## Greedy meshing (`mesh.py`)

Quads are rows of `dir, x, z, y0, du, dv, color` in tile-local cells:

| dir | facing | plane | spans |
|---|---|---|---|
| 0 | +Y top | `y = y0` | `x..x+du`, `z..z+dv` |
| 1 / 2 | +X / −X | `x` | `z..z+du`, `y0..y0+dv` |
| 3 / 4 | +Z / −Z | `z` | `x..x+du`, `y0..y0+dv` |

- **Top faces**: run-length encode each row along X by `(height, colour)`, then merge
  identical runs on consecutive rows.
- **Side faces**: for every plane between two columns, the higher one shows a wall down to the
  lower one, split into a one-cell **top band** (top colour) and a **body** (sub-surface
  colour); walls merge along the plane.
- **Chunk breaks**: runs restart at every multiple of `chunk_size` along X and Z and rows never
  merge across one, so every quad lies in exactly one `chunk_size × chunk_size` cell. This is
  what lets the viewer tint quads by a cell-keyed overlay without splitting geometry.
- **Skirts**: columns outside the tile and holes count as a floor `skirt` cells below the
  lowest column, so each tile is closed at its edges. Adjacent tiles therefore show walls at
  their seams when looked at from below; that is the price of independent tiles.
- Output is sorted, so meshing is deterministic. `reference_faces` meshes the same input one
  unit face at a time; the tests require the greedy quads to cover exactly that set with no
  overlap.

## Tiling

`build_tiles(surface, tile_size=256, chunk_size=16, skirt=3)` cuts world-aligned tiles:
`t.<tx>.<tz>` covers `[tx·tile_size, (tx+1)·tile_size)` on X and likewise on Z. `tile_size`
must be a multiple of `chunk_size` (so chunk breaks are world-aligned) and at most 65535.

## Tile format (`UWT1`)

Little-endian, stored gzip-compressed (`mtime = 0`) under a `.uwt` name:

| offset | size | field |
|---|---|---|
| 0 | 4 | magic `"UWT1"` (the `1` is the format version) |
| 4 | 4 | `n` — uint32 quad count |
| 8 | 4 | `x` — int32 tile origin X |
| 12 | 4 | `z` — int32 tile origin Z |
| 16 | 2 | `width` — uint16 |
| 18 | 2 | `depth` — uint16 |
| 20 | n | `dir` — uint8 × n |
| 20+n | n mod 2 | zero padding (2-byte alignment) |
| … | 2n each | `x` u16, `z` u16, `y0` i16, `du` u16, `dv` u16, `color` u16 |

Total raw size `20 + n + (n mod 2) + 12n`. Columns are stored structure-of-arrays because it
compresses better than interleaved records. Readers must check the magic and the exact size.
The viewer gunzips only when the bytes start with `1f 8b`, so a server that already decoded a
`Content-Encoding: gzip` response still works. Limits that follow from the types: heights in
`[−32768, 32767]`, at most 65535 palette colours, tile side ≤ 65535.

A future incompatible layout gets a new magic (`UWT2`); a reader that does not know a magic must
refuse the tile.

## `index.json`

```json
{
  "format": "usd-web-index",
  "version": 1,
  "revision": 7,
  "generated": 1760745600.0,
  "title": "optional",
  "view": {"target": [x, y, z], "distance": 300, "theta": 0.8, "phi": 0.9},
  "palette": ["#rrggbb", "..."],
  "tiles": {
    "t.0.0": {"file": "t.0.0.1a2b3c4d5e6f.uwt", "version": "1a2b3c4d5e6f",
              "x": 0, "z": 0, "width": 256, "depth": 256,
              "quads": 1719, "bytes": 4128, "yMin": 1, "yMax": 15}
  },
  "layers": [{
    "id": "zones", "label": "Zones", "kind": "cells", "cellSize": 16,
    "visible": true, "tint": 0.5, "labels": true,
    "entries": [{"label": "Camp", "color": "#e0553d", "cells": [[2, 2], [2, 3]]}]
  }],
  "labels": [{"text": "Origin", "position": [0, null, 0], "color": "#ffffff"}],
  "sprites": {
    "images": {"tree": "sprites/tree.png"},
    "items": [{"image": "tree", "position": [12.5, null, 40.5], "scale": 3}]
  }
}
```

- `version` is the **schema** version. Rules: adding optional fields does not change it;
  readers ignore unknown fields; removing or changing the meaning of a field bumps it, and a
  reader refuses an index whose `version` is newer than it supports.
- `revision` increases by one on every publish into the directory.
- `version` of a tile is the first 12 hex digits of the SHA-256 of its file bytes, and is part
  of the file name, so tile files are immutable and cacheable forever.
- `palette` is append-only across incremental publishes.
- A `null` Y in a label or sprite position means "on the surface" (the viewer looks it up).
- Layer `cellSize` should be a multiple of the tiles' `chunk_size`; otherwise a quad can span
  two layer cells and is tinted by the cell of its owning column's minimum corner.
- Sprite image URLs and tile files are resolved relative to the index; tile `file` names are
  validated to contain no path separators.

## Atomic publish (`publish.py`)

`publish(out_dir, tiles, palette, …)`:

1. writes each changed tile as `<name>.<version>.uwt` via a temporary file in the same
   directory, `fsync`, `os.replace` (skipped when that file already exists);
2. writes `index.json` the same way, **last**;
3. prunes `*.uwt` files named by neither the new nor the **previous** index — a client that
   fetched the previous index a moment ago still finds its tiles.

A crash at any step leaves the previous `index.json` and every file it names intact (both
cases are tested by failing `os.replace` mid-publish). Publishing is incremental by default:
tiles not passed are kept (`remove=` drops some); `replace=True` starts over. Sections
(`layers`, `labels`, `sprites`, `title`, `view`) passed as `None` are carried over.

Only one publisher per directory is supported; two concurrent publishers can lose each other's
index update (never a tile, since names are content-addressed).

## Viewer module (`viewer/usd-web-viewer.js`)

```js
import { createViewer, decodeTile, maybeGunzip, tileArrays,
         INDEX_FORMAT, INDEX_VERSION, TILE_MAGIC } from "./usd-web-viewer.js";
const viewer = createViewer({
  THREE,                 // required: the page's three.js namespace
  canvas,                // required
  baseUrl: "tiles/",     // where index.json lives (default "./")
  pollMs: 5000,          // 0 disables live polling
  panel,                 // optional element: title, layer toggles, legend with fly-to
  labelsHost,            // optional positioned element for projected labels
  hook: "__usdWeb",      // window test hook name (null disables)
  background: 0xb9d6ee,
  pixelatedSprites: true,
});
await viewer.ready;
viewer.flyTo(x, y, z, distance); viewer.setLayerVisible("zones", false);
viewer.setLabelsVisible(true); viewer.setSpritesVisible(true);
viewer.heightAt(x, z); await viewer.poll(); viewer.render(); viewer.dispose();
viewer.scene; viewer.camera; viewer.renderer;   // for page-specific additions (then render())
viewer.state;   // {index, tiles, target, dist}: the loaded index, tile count and camera target/distance
```

- **Geometry**: one `BufferGeometry` per tile (Float32 positions, Int8 normals, Uint8 colours,
  Uint32 indices), `MeshLambertMaterial` with vertex colours; side faces are shaded darker.
- **Overlays**: a `cells` layer tints the quads whose owning column lies in one of its entries'
  cells (`tint` is the blend factor). Legend buttons fly to an entry's centroid.
- **Labels** are HTML elements projected each frame; all data goes through `textContent`.
- **Sprites** are upright camera-facing cards (rotated about Y to face the camera), bottom
  anchored, from the `sprites.images` URL map; a failed image shows magenta.
- **Controls**: pointer events — drag orbits, right-/shift-drag pans, wheel zooms; two
  pointers pinch-zoom and pan. `touch-action: none` on the canvas.
- **Hot swap**: every `pollMs` the index is refetched (`cache: "no-store"`); only tiles whose
  `version` changed are fetched, the new mesh replaces the old one, and the old geometry is
  disposed. Removed tiles are dropped; changed layers/labels/sprites are rebuilt.
- **Rendering** is on demand (only after input, a swap, or an animation).
- **Test hook** `window.__usdWeb`: `ready`, `tiles`, `revision`, `versions`, `swaps`,
  `errors`, `frames`, `labels`, `sprites`, `render()`, `poll()`.

### three.js pinning

The module never imports three.js; the page imports an **exact** version and passes it in. The
demo uses `https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js`. The module uses
only long-stable API (`BufferGeometry`, `MeshLambertMaterial`, `Fog`, `TextureLoader`,
`PlaneGeometry`); it sets `texture.colorSpace` when the three.js version has it. Bumping
three.js is a one-line page change, verified by the browser smoke test.

## Replays (`replay.py`, `viewer/usd-web-replay.js`)

Input is a stage in usd-recorder's `usd-replay/1` contract (`docs/REPLAY.md` there): one Z-up
layer, `/Replay.replay:stateHash` sampled at every sample tick, `Points` entity sets under
`/Replay/Entities`, colour-grid `Mesh` fields under `/Replay/Fields`, static `Points` under
`/Replay/Scene`, `replay:stat:*` doubles. `read_stage` reads exactly that and refuses anything
whose `customLayerData.usdReplay.schema` is not `usd-replay/1` (run `usd-recorder replay
validate` for the full rule check). usd-web does not depend on usd-recorder at run time; the
tests use its writer to make fixtures.

**Output.** `replay.json` (written last, atomically) and content-addressed chunks
`r.<first sample>.<sha>.uwr`, gzip'd. A conversion removes chunks the new index does not name.

`replay.json` (`format: "usd-web-replay"`, `version: 1`): `title`, `meta` (the replay's
`usdReplay` dictionary), `playbackRate`, `samples`, `ticks[]`, `hashes[]`, `bounds`, `quant`
(`min[3]`, `step[3]`, `idBytes`), `sets[]` (`id`, `label`, `shape`, `color` or null,
`width` or null, `vertexColor`, `vertexWidth`, `widthMax`, `maxCount`), `fields[]` (`id`,
`label`, `cols`, `rows`, `origin`, `cellSize`, `z`, `ticks` where the chunks carry a sample),
`stats` (one value per kept sample, held), `scene[]` (static points inline), `chunks[]`
(`file`, `first`, `count`, `bytes`, `raw`), optional `view`.

**UWR1 chunk** (little-endian): `"UWR1"`, `u32 count`, `u32 first`, then per sample, per set:
`u32 n`, `n` ids (`u16` or `u32` by `idBytes`; dense per set in first-seen order), `n × 3 u16`
quantized positions (`min + q * step`), then `n × 3 u8` sRGB colours if `vertexColor` and `n u8`
widths (`/ 255 * widthMax`) if `vertexWidth`; then per field `u8 present` and, if 1,
`cols × rows × 3 u8` sRGB. Each chunk restates every field in its first sample, so it decodes
on its own. `--every N` / `--max-samples N` thin the samples (always keeping the last); fields
and stats are held across dropped samples.

**Player.** The module exports `createReplayPlayer`, `decodeChunk(u8, doc)`, `floorIndex`, and
the constants `REPLAY_FORMAT`, `REPLAY_VERSION`, `CHUNK_MAGIC` and `SPEEDS`.
`createReplayPlayer({THREE, canvas, baseUrl, controls?, autoplay?, speed?, loop?,
interpolate?, start?, onTick?, keyboardTarget?, background?, fitPadding?, reducedMotion?,
preserveDrawingBuffer?, hook?})` returns `{ready, play, pause, seek(tick), seekSample(k),
setSpeed, render, dispose, scene, camera, renderer, state}`; `state` is
`{doc, playing, speed, sample, tick, hash}`. It maps the
Z-up replay to three.js Y-up as `(x, z, -y)`, draws each set as one `InstancedMesh` (sphere or
cube), each field as a plane with a nearest-filtered `DataTexture`, and scene points as static
instances. Playback advances `playbackRate × speed` ticks per second; positions of ids present
in both neighbouring samples are interpolated, fields and stats hold. Chunk 0 is loaded before
`ready`; the rest stream in order, and playback waits at an unloaded chunk. `prefers-reduced-
motion` turns autoplay off (`reducedMotion: true|false` overrides the media query). `controls` gets a play/pause button, a range scrubber, a speed button
and a tick readout (44 px targets; the page CSS puts the scrubber on its own row under 640 px).
Space toggles play, arrow keys step a sample. The hook `window.__usdWebReplay` exposes
`ready, samples, chunks, loaded, tick, sample, hash, playing, frames, errors` (plus `speed` once
it has been changed) and `play(), pause(), seek(tick), render()` for tests.

## Tests

- `test_mesh.py`: greedy output equals the naive reference on random maps (with holes, several
  chunk sizes) with no overlaps; chunk boundaries; a step's walls; determinism.
- `test_tile.py`: encode/decode round-trip, header layout, deterministic gzip, range checks,
  world-aligned tiling with negative origins, top-surface reduction, palette.
- `test_stage.py`: Y-up and Z-up stages give the same surface; world transforms; class-inherited
  colours; prototype filter; palette reuse.
- `test_publish.py`: content addressing, determinism, incremental publish, index-last order,
  crash mid-publish (tile or index), `atomic_write_bytes` (replace, crash leaves no temp file),
  append-only palette, schema validation (malformed shapes raise `ValueError`).
- `test_cli_and_js.py`: the CLI (`inspect` reads exactly the named file and reports a bad path
  or a malformed index as an error; every argument has help text); the JS decoder (run under node) reads Python-written tiles;
  the viewer's and replay player's format constants equal the Python ones.
- `tests/browser/test_viewer_smoke.py` (`-m browser`, CI): headless Chromium with SwiftShader
  loads the demo, checks tiles/labels/sprites/legend and that pixels were drawn, republishes one
  tile and waits for exactly that tile to be hot-swapped, with no console errors.
- `test_replay.py`: replay round trip against usd-recorder's reader (ids, quantized positions,
  colours, widths, field samples and holds, stats, scene), stand-alone chunks, thinning, stale
  chunk removal, a non-replay refused, the CLI, and the JS chunk decoder under node.
- `tests/browser/test_replay_smoke.py` (`-m browser`): the replay page plays, pauses, scrubs
  (tick and state hash follow), steps with the arrow keys, cycles speed, draws pixels, and on a
  phone viewport puts the scrubber on its own row with buttons at least 44 px tall.

## Open questions

- Level of detail (2×/4× downsampled tiles) and lazy loading by view distance for big worlds.
- Optional seam culling between neighbouring tiles (needs the neighbours' edge columns).
- A `points` layer kind (markers without cells) if a consumer needs it.
