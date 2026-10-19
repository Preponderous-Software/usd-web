# Concept

## The problem

Several projects in this family produce worlds as OpenUSD stages or grids — voxel terrain
exported to `PointInstancer`s, procedurally generated rooms, simulation grids — and each one
that wanted a browser view wrote its own three.js page: its own binary format, its own
greedy mesher, its own orbit controls, its own touch handling, its own polling. The pages
worked, but every one re-learned the same lessons:

- a USD stage of millions of instanced cubes is far too heavy for a phone; the **top surface**,
  greedy-meshed, is two orders of magnitude smaller and still reads as the world;
- quads must **break at chunk boundaries**, or an overlay keyed by chunk (a claim, a zone, a
  room) cannot tint them;
- a live map needs **atomic publishing**: a poller must never read an index that points at a
  half-written tile;
- touch is first-class: pinch and two-finger pan, not just a mouse;
- anything shown from data (names, labels) must go through `textContent`.

`usd-web` is those lessons, once, as a library.

## What it is

1. **A Python tiler.** Input: a USD stage (every `PointInstancer`, colours from prototype
   `displayColor`, Y-up or Z-up) or plain numpy heightmap + colour arrays. Output: a directory
   of gzip binary tiles (`UWT1`) with content-addressed names and a versioned `index.json`.
2. **A viewer module.** One dependency-free ES module. The host page passes in its own pinned
   three.js; the module draws the tiles, overlay layers (tint + legend with fly-to + labels),
   billboard sprites, and polls `index.json` to hot-swap changed tiles.
3. **A CLI** (`usd-web tiles | heightmap | replay | inspect`).
4. **A replay player** for `usd-replay/1` simulation replays (`usd-web replay`, `viewer/usd-web-replay.js`).

## What it is not

- **Not a general USD renderer.** It shows a heightfield-like top surface: overhangs, caves and
  interiors are dropped by design. Arbitrary meshes, materials and animation are out of scope
  (use usdview, Blender, or a USD-to-glTF path for those). The one kind of animation it plays is
  a simulation replay in the narrow `usd-replay/1` layout (points, colour grids, static points).
- **Not a tile server.** Output is static files; any static host (or `python -m http.server`)
  serves them.
- **Not game-specific.** Overlays are "layers of cells with label + colour"; a consumer maps its
  own concepts (claims, rooms, zones) onto them.
- **No level of detail yet.** Large worlds should be published as several tile sets or wait for
  LOD tiles (see ARCHITECTURE.md § Open questions).

## Design principles

- Deterministic output: the same input gives byte-identical tiles (gzip `mtime = 0`, sorted
  quads), so content hashes are meaningful versions.
- Versioned formats: the tile magic carries the format version; the index carries `format`,
  `version` and a monotonically increasing `revision`.
- Append-only palette: incremental publishes never re-index colours of tiles already out there.
- The page, not the library, pins three.js.
