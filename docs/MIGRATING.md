# Migrating an existing viewer onto usd-web

## Roam's 3D spectator

[`Preponderous-Software/roam`](https://github.com/Preponderous-Software/roam) ships
`spectator/index.html`: a single page served by Roam's read-only REST server that polls
`GET /api/v1/snapshot` every 500 ms and draws the rooms around the player as an HD-2D diorama
(three@0.147.0 from jsdelivr + the legacy `examples/js/controls/OrbitControls.js`).

| Spectator piece | usd-web equivalent | Notes |
|---|---|---|
| Biome height field (`surfaceHeight`, plateau blend, half-tile terraces) | `Surface.from_heightmap` on the server side | Heights are integer cells in usd-web; half-tile terraces need a 2× vertical scale (multiply heights by 2 and scale the mesh Y by 0.5 in the page). |
| `Batch` geometry builder, one draw call per texture per room | `greedy_mesh` + one mesh per tile | usd-web is vertex-coloured, **not textured**: the pixel-art tile textures do not carry over. This is the main visual trade-off. |
| Per-room rebuild when `chunkSignature(room)` changes | tile `version` (content hash) + hot-swap polling | A room maps naturally onto a tile: set both `tile_size` and `chunk_size` to the room size (17). |
| Rocks, trunks, canopy as raised columns | top surface columns | Canopies over walkable ground are overhangs and are dropped by a top-surface mesher. |
| Creature/item/player cards facing the camera | `sprites` section (upright cards from an image URL map) | Sprite items are positions + image names; the spectator's per-class `CARD_SCALE` becomes each item's `scale`. |
| `OrbitControls` + follow-player + recentre | built-in orbit/pan/zoom + `viewer.flyTo` | "Follow player" becomes a `flyTo` on each poll from the page. |
| Hover/tap tile inspector | not provided | Stays Roam-specific; `viewer.heightAt` and the decoded tiles give the page what it needs to pick. |
| Day/night lighting, point lights, shadows, cave levels | not provided | Stays Roam-specific; the page can add its own lights through `viewer.scene` and call `viewer.render()`. |

**Suggested path:** keep the spectator as is for play sessions (it is textured and lit) and
use usd-web where a static or slowly changing overview is wanted — e.g. a whole-world map
published from a save, with rooms as tiles and points of interest as a `cells` layer. A Python
exporter (Roam is Python) would write tiles with `build_tiles` + `publish`; the viewer needs no
Roam code. Migrating the live spectator itself would first need textured tiles, which are outside
usd-web v0.1.

## A voxel-world map from Minecraft-USD-Exporter output

The tiler's stage path was shaped by exactly this case: `usd-web tiles terrain.usdc
--exclude <decorative prototype names>` reads the exporter's per-region `PointInstancer`s
(prototypes inherit their colours from `/World/BlockLibrary`, which usd-web resolves through
composition). Claims or other chunk-keyed data become a `cells` layer with `cellSize: 16`.
