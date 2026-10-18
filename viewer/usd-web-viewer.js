// usd-web viewer: a dependency-free ES module that draws a usd-web tile directory with three.js.
//
// The host page owns three.js and passes its namespace in, so the page pins the exact version:
//
//   import * as THREE from "https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js";
//   import { createViewer } from "./usd-web-viewer.js";
//   const viewer = createViewer({ THREE, canvas, baseUrl: "tiles/", panel, labelsHost });
//   await viewer.ready;
//
// Data from index.json (labels, legend entries, titles) is only ever written with textContent.
// See ARCHITECTURE.md § Viewer module for the full option list.

export const INDEX_FORMAT = "usd-web-index";
export const INDEX_VERSION = 1;
export const TILE_MAGIC = "UWT1";

const NORMALS = [[0, 1, 0], [1, 0, 0], [-1, 0, 0], [0, 0, 1], [0, 0, -1]];
const SHADE = [1.0, 0.86, 0.86, 0.78, 0.78];
const HOLE = -2147483648;

// ------------------------------------------------------------------------------- decoding --

/** Gunzip ``bytes`` when they start with the gzip magic; return them unchanged otherwise. */
export async function maybeGunzip(bytes) {
  const u8 = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  if (u8.length < 2 || u8[0] !== 0x1f || u8[1] !== 0x8b) return u8;
  const stream = new Blob([u8]).stream().pipeThrough(new DecompressionStream("gzip"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

/** Decode raw UWT1 bytes into {n, x, z, width, depth, dir, qx, qz, y0, du, dv, color}. */
export function decodeTile(u8) {
  if (u8.length < 20) throw new Error("truncated tile");
  const magic = String.fromCharCode(u8[0], u8[1], u8[2], u8[3]);
  if (magic !== TILE_MAGIC) throw new Error(`not a ${TILE_MAGIC} tile: ${JSON.stringify(magic)}`);
  const dv = new DataView(u8.buffer, u8.byteOffset, u8.byteLength);
  const n = dv.getUint32(4, true);
  const out = { n, x: dv.getInt32(8, true), z: dv.getInt32(12, true), width: dv.getUint16(16, true), depth: dv.getUint16(18, true) };
  const expected = 20 + n + (n % 2) + 12 * n;
  if (u8.length !== expected) throw new Error(`tile is ${u8.length} bytes, expected ${expected}`);
  let off = 20;
  out.dir = u8.slice(off, off + n); off += n + (n % 2);
  const column = Ctor => { const a = new Ctor(u8.buffer.slice(u8.byteOffset + off, u8.byteOffset + off + 2 * n)); off += 2 * n; return a; };
  out.qx = column(Uint16Array); out.qz = column(Uint16Array); out.y0 = column(Int16Array);
  out.du = column(Uint16Array); out.dv = column(Uint16Array); out.color = column(Uint16Array);
  return out;
}

const hexRgb = s => [parseInt(s.slice(1, 3), 16), parseInt(s.slice(3, 5), 16), parseInt(s.slice(5, 7), 16)];

/** Vertex positions/normals/indices for a decoded tile, plus per-quad owning column and the
 * tile's top-surface heights (``heights[z * width + x]``, HOLE where no top face). */
export function tileArrays(t) {
  const n = t.n, pos = new Float32Array(n * 12), nor = new Int8Array(n * 12), idx = new Uint32Array(n * 6);
  const ownerX = new Int32Array(n), ownerZ = new Int32Array(n);
  const heights = new Int32Array(t.width * t.depth).fill(HOLE);
  for (let q = 0; q < n; q++) {
    const d = t.dir[q], x = t.qx[q], z = t.qz[q], y = t.y0[q], du = t.du[q], dv = t.dv[q];
    let v, ox = x, oz = z;
    if (d === 0) {
      v = [x, y, z, x, y, z + dv, x + du, y, z + dv, x + du, y, z];
      for (let zz = z; zz < z + dv; zz++) heights.fill(y, zz * t.width + x, zz * t.width + x + du);
    } else if (d === 1 || d === 2) {
      v = d === 1 ? [x, y, z, x, y + dv, z, x, y + dv, z + du, x, y, z + du]
                  : [x, y, z, x, y, z + du, x, y + dv, z + du, x, y + dv, z];
      if (d === 1) ox = x - 1;
    } else {
      v = d === 3 ? [x, y, z, x + du, y, z, x + du, y + dv, z, x, y + dv, z]
                  : [x, y, z, x, y + dv, z, x + du, y + dv, z, x + du, y, z];
      if (d === 3) oz = z - 1;
    }
    pos.set(v, q * 12);
    const nn = NORMALS[d];
    for (let k = 0; k < 4; k++) { nor[q * 12 + k * 3] = nn[0] * 127; nor[q * 12 + k * 3 + 1] = nn[1] * 127; nor[q * 12 + k * 3 + 2] = nn[2] * 127; }
    ownerX[q] = Math.max(0, Math.min(t.width - 1, ox));
    ownerZ[q] = Math.max(0, Math.min(t.depth - 1, oz));
    const i0 = q * 4;
    idx[q * 6] = i0; idx[q * 6 + 1] = i0 + 1; idx[q * 6 + 2] = i0 + 2;
    idx[q * 6 + 3] = i0; idx[q * 6 + 4] = i0 + 2; idx[q * 6 + 5] = i0 + 3;
  }
  return { pos, nor, idx, ownerX, ownerZ, heights };
}

// --------------------------------------------------------------------------------- viewer --

/**
 * Create a viewer. Options:
 *   THREE (required)   the three.js namespace (the page pins its version)
 *   canvas (required)  a <canvas>
 *   baseUrl            directory holding index.json and the tiles (default "./")
 *   pollMs             index.json poll interval for tile hot-swap; 0 disables (default 5000)
 *   panel              element to fill with title, layer toggles and legend (optional)
 *   labelsHost         positioned element for projected HTML labels (optional)
 *   hook               name of the window test hook object (default "__usdWeb"; null disables)
 *   background         scene colour (default 0xb9d6ee)
 *   pixelatedSprites   nearest-neighbour sprite filtering (default true)
 * Returns { ready, poll, flyTo, heightAt, scene, camera, renderer, setLayerVisible, setLabelsVisible,
 *           setSpritesVisible, render, dispose, state }. Call render() after changing the scene yourself.
 */
export function createViewer(options) {
  const { THREE, canvas } = options;
  if (!THREE || !canvas) throw new Error("createViewer needs { THREE, canvas }");
  const baseUrl = new URL(options.baseUrl || "./", document.baseURI);
  const pollMs = options.pollMs === undefined ? 5000 : options.pollMs;
  const hookName = options.hook === undefined ? "__usdWeb" : options.hook;
  const hook = { ready: false, tiles: 0, revision: null, swaps: [], errors: [], frames: 0, versions: {} };
  if (hookName) window[hookName] = hook;

  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, preserveDrawingBuffer: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  const scene = new THREE.Scene();
  const sky = new THREE.Color(options.background === undefined ? 0xb9d6ee : options.background);
  scene.background = sky;
  scene.fog = new THREE.Fog(sky, 900, 2600);
  const camera = new THREE.PerspectiveCamera(50, 1, 0.5, 6000);
  scene.add(new THREE.HemisphereLight(0xeaf4ff, 0x5a5040, 1.8));
  const sun = new THREE.DirectionalLight(0xffffff, 1.6);
  sun.position.set(0.5, 1, 0.3);
  scene.add(sun);
  const material = new THREE.MeshLambertMaterial({ vertexColors: true });

  let index = null, palette = [], disposed = false, pollTimer = null, raf = null, needsRender = true;
  const tiles = new Map(); // name -> {mesh, arrays, base, colorAttr, meta, decoded}
  const layerVisible = new Map();
  const layerCells = new Map(); // layer id -> Map("cx,cz" -> entry index)
  let labelsVisible = true, spritesVisible = true;
  const spriteGroup = new THREE.Group();
  scene.add(spriteGroup);
  const texCache = new Map();
  const cardGeo = new THREE.PlaneGeometry(1, 1);
  cardGeo.translate(0, 0.5, 0);
  let labels = [];
  let sectionsKey = null;

  const fail = err => { hook.errors.push(String(err && err.message || err)); console.warn("usd-web:", err); };

  // ---- data ----
  async function fetchIndex() {
    const r = await fetch(new URL("index.json", baseUrl), { cache: "no-store" });
    if (!r.ok) throw new Error(`index.json: HTTP ${r.status}`);
    const doc = await r.json();
    if (doc.format !== INDEX_FORMAT) throw new Error("not a usd-web index");
    if (!(doc.version <= INDEX_VERSION)) throw new Error(`index version ${doc.version} is newer than this viewer (${INDEX_VERSION})`);
    return doc;
  }
  async function fetchTile(meta) {
    const r = await fetch(new URL(meta.file, baseUrl));
    if (!r.ok) throw new Error(`${meta.file}: HTTP ${r.status}`);
    return decodeTile(await maybeGunzip(await r.arrayBuffer()));
  }

  // ---- colouring ----
  function cellOwner(layer, wx, wz) {
    const cells = layerCells.get(layer.id);
    return cells ? cells.get(Math.floor(wx / layer.cellSize) + "," + Math.floor(wz / layer.cellSize)) : undefined;
  }
  function applyColors(tile) {
    const { arrays, base, decoded } = tile, c = tile.colorAttr.array, n = decoded.n;
    const active = (index.layers || []).filter(l => layerVisible.get(l.id));
    const tints = active.map(l => ({ l, a: l.tint === undefined ? 0.5 : l.tint, rgb: (l.entries || []).map(e => hexRgb(e.color)) }));
    for (let q = 0; q < n; q++) {
      let r = base[q * 3], g = base[q * 3 + 1], b = base[q * 3 + 2];
      const wx = decoded.x + arrays.ownerX[q], wz = decoded.z + arrays.ownerZ[q];
      for (const t of tints) {
        const o = cellOwner(t.l, wx, wz);
        if (o === undefined) continue;
        const f = t.rgb[o];
        r = r * (1 - t.a) + f[0] * t.a; g = g * (1 - t.a) + f[1] * t.a; b = b * (1 - t.a) + f[2] * t.a;
      }
      for (let k = 0; k < 4; k++) { c[q * 12 + k * 3] = r; c[q * 12 + k * 3 + 1] = g; c[q * 12 + k * 3 + 2] = b; }
    }
    tile.colorAttr.needsUpdate = true;
    needsRender = true;
  }

  function baseColors(decoded) {
    const n = decoded.n, base = new Uint8Array(n * 3);
    for (let q = 0; q < n; q++) {
      const rgb = palette[decoded.color[q]] || [255, 0, 255], s = SHADE[decoded.dir[q]];
      base[q * 3] = rgb[0] * s; base[q * 3 + 1] = rgb[1] * s; base[q * 3 + 2] = rgb[2] * s;
    }
    return base;
  }

  function buildTile(decoded, meta) {
    const arrays = tileArrays(decoded), n = decoded.n, base = baseColors(decoded);
    const g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(arrays.pos, 3));
    g.setAttribute("normal", new THREE.BufferAttribute(arrays.nor, 3, true));
    const colorAttr = new THREE.BufferAttribute(new Uint8Array(n * 12), 3, true);
    g.setAttribute("color", colorAttr);
    g.setIndex(new THREE.BufferAttribute(arrays.idx, 1));
    g.computeBoundingSphere();
    const mesh = new THREE.Mesh(g, material);
    mesh.position.set(decoded.x, 0, decoded.z);
    const tile = { mesh, arrays, base, colorAttr, meta, decoded };
    applyColors(tile);
    return tile;
  }

  function removeTile(name) {
    const old = tiles.get(name);
    if (!old) return;
    scene.remove(old.mesh);
    old.mesh.geometry.dispose();
    tiles.delete(name);
    delete hook.versions[name];
  }

  async function loadTile(name, meta) {
    const t0 = performance.now();
    const decoded = await fetchTile(meta);
    const built = buildTile(decoded, meta);
    const swapped = tiles.has(name);
    removeTile(name);
    scene.add(built.mesh);
    tiles.set(name, built);
    hook.versions[name] = meta.version;
    hook.tiles = tiles.size;
    if (swapped) hook.swaps.push({ tile: name, version: meta.version, ms: Math.round(performance.now() - t0) });
    needsRender = true;
  }

  /** Top-surface height at world cell (x, z), or null. */
  function heightAt(x, z) {
    x = Math.floor(x); z = Math.floor(z);
    for (const t of tiles.values()) {
      const d = t.decoded, lx = x - d.x, lz = z - d.z;
      if (lx < 0 || lz < 0 || lx >= d.width || lz >= d.depth) continue;
      const h = t.arrays.heights[lz * d.width + lx];
      return h === HOLE ? null : h;
    }
    return null;
  }

  // ---- overlay sections ----
  function entryCentre(layer, entry) {
    let sx = 0, sz = 0;
    for (const [cx, cz] of entry.cells) { sx += (cx + 0.5) * layer.cellSize; sz += (cz + 0.5) * layer.cellSize; }
    const n = Math.max(1, entry.cells.length), x = sx / n, z = sz / n, h = heightAt(x, z);
    return { x, y: h === null ? 0 : h, z, dist: Math.max(60, Math.sqrt(n) * layer.cellSize * 3.2) };
  }

  function rebuildSections() {
    layerCells.clear();
    for (const layer of index.layers || []) {
      if (!layerVisible.has(layer.id)) layerVisible.set(layer.id, layer.visible !== false);
      const m = new Map();
      (layer.entries || []).forEach((e, i) => { for (const [cx, cz] of e.cells) m.set(cx + "," + cz, i); });
      layerCells.set(layer.id, m);
    }
    for (const t of tiles.values()) applyColors(t);
    buildSprites();
    buildLabels();
    buildPanel();
  }

  function texture(url) {
    if (texCache.has(url)) return texCache.get(url);
    const t = new THREE.TextureLoader().load(url, () => { needsRender = true; }, undefined, () => {
      const c = document.createElement("canvas"); c.width = c.height = 2;
      const g = c.getContext("2d"); g.fillStyle = "#ff00ff"; g.fillRect(0, 0, 2, 2);
      t.image = c; t.needsUpdate = true; needsRender = true;
    });
    if (options.pixelatedSprites !== false) { t.magFilter = THREE.NearestFilter; }
    if ("colorSpace" in t && THREE.SRGBColorSpace) t.colorSpace = THREE.SRGBColorSpace;
    texCache.set(url, t);
    return t;
  }
  const spriteMats = new Map();
  function spriteMaterial(url) {
    if (!spriteMats.has(url)) spriteMats.set(url, new THREE.MeshBasicMaterial({ map: texture(url), transparent: true, alphaTest: 0.5, side: THREE.DoubleSide }));
    return spriteMats.get(url);
  }
  function buildSprites() {
    spriteGroup.clear();
    const sp = index.sprites;
    if (!sp) return;
    for (const item of sp.items || []) {
      const src = (sp.images || {})[item.image];
      if (!src) continue;
      const [x, y, z] = item.position;
      const m = new THREE.Mesh(cardGeo, spriteMaterial(new URL(src, baseUrl).href));
      const h = y === null || y === undefined ? heightAt(x, z) : y;
      m.position.set(x, h === null ? 0 : h, z);
      m.scale.setScalar(item.scale || 1);
      spriteGroup.add(m);
    }
    spriteGroup.visible = spritesVisible;
    hook.sprites = spriteGroup.children.length;
    needsRender = true;
  }

  function buildLabels() {
    const host = options.labelsHost;
    labels = [];
    if (!host) return;
    host.replaceChildren();
    const add = (text, color, p) => {
      const el = document.createElement("div");
      el.className = "uw-label";
      if (color) { const sw = document.createElement("span"); sw.className = "uw-swatch"; sw.style.background = color; el.append(sw); }
      const t = document.createElement("span"); t.textContent = text; el.append(t);
      host.append(el);
      labels.push({ el, p: new THREE.Vector3(p.x, p.y, p.z) });
    };
    for (const layer of index.layers || []) {
      if (!layer.labels || !layerVisible.get(layer.id)) continue;
      for (const e of layer.entries || []) { const c = entryCentre(layer, e); add(e.label, e.color, { x: c.x, y: c.y + 6, z: c.z }); }
    }
    for (const l of index.labels || []) {
      const [x, y, z] = l.position, h = y === null || y === undefined ? heightAt(x, z) : y;
      add(l.text, l.color || null, { x, y: (h === null ? 0 : h) + 4, z });
    }
    hook.labels = labels.length;
  }
  const _v = new THREE.Vector3();
  function placeLabels() {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    for (const L of labels) {
      _v.copy(L.p).project(camera);
      const vis = labelsVisible && _v.z < 1 && Math.abs(_v.x) < 1.1 && Math.abs(_v.y) < 1.1;
      L.el.style.display = vis ? "" : "none";
      if (vis) { L.el.style.left = ((_v.x + 1) / 2 * w) + "px"; L.el.style.top = ((1 - _v.y) / 2 * h) + "px"; }
    }
  }

  function buildPanel() {
    const panel = options.panel;
    if (!panel) return;
    panel.replaceChildren();
    const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };
    panel.append(el("h1", "uw-title", index.title || "usd-web"));
    const q = Object.values(index.tiles).reduce((a, m) => a + m.quads, 0);
    panel.append(el("div", "uw-sub", `${Object.keys(index.tiles).length} tiles · ${q.toLocaleString()} quads · revision ${index.revision}`));
    const toggle = (label, checked, onchange) => {
      const row = el("label", "uw-row"), box = el("input");
      box.type = "checkbox"; box.checked = checked; box.onchange = () => onchange(box.checked);
      row.append(box, el("span", null, label));
      return row;
    };
    const hasLabels = (index.labels || []).length || (index.layers || []).some(l => l.labels);
    if ((index.layers || []).length || hasLabels || index.sprites) panel.append(el("h2", "uw-h2", "Layers"));
    for (const layer of index.layers || []) panel.append(toggle(layer.label || layer.id, !!layerVisible.get(layer.id), on => setLayerVisible(layer.id, on)));
    if (hasLabels) panel.append(toggle("Labels", labelsVisible, setLabelsVisible));
    if (index.sprites) panel.append(toggle("Sprites", spritesVisible, setSpritesVisible));
    for (const layer of index.layers || []) {
      if (!(layer.entries || []).length) continue;
      panel.append(el("h2", "uw-h2", layer.label || layer.id));
      const list = el("div", "uw-legend");
      for (const e of layer.entries) {
        const b = el("button", "uw-entry"), sw = el("span", "uw-swatch");
        sw.style.background = e.color;
        b.append(sw, el("span", null, e.label), el("span", "uw-num", String(e.cells.length)));
        b.onclick = () => { const c = entryCentre(layer, e); flyTo(c.x, c.y, c.z, c.dist); };
        list.append(b);
      }
      panel.append(list);
    }
    panel.append(el("div", "uw-help", "Drag to orbit, right-drag or shift-drag to pan (two fingers on touch), wheel or pinch to zoom."));
  }

  function setLayerVisible(id, on) { layerVisible.set(id, !!on); for (const t of tiles.values()) applyColors(t); buildLabels(); }
  function setLabelsVisible(on) { labelsVisible = !!on; needsRender = true; }
  function setSpritesVisible(on) { spritesVisible = !!on; spriteGroup.visible = spritesVisible; needsRender = true; }

  // ---- camera ----
  const ctl = { target: new THREE.Vector3(), dist: 300, theta: 0.8, phi: 0.9 };
  let anim = null;
  function placeCamera() {
    const sp = Math.sin(ctl.phi);
    camera.position.set(ctl.target.x + ctl.dist * sp * Math.sin(ctl.theta), ctl.target.y + ctl.dist * Math.cos(ctl.phi),
      ctl.target.z + ctl.dist * sp * Math.cos(ctl.theta));
    camera.lookAt(ctl.target);
  }
  function flyTo(x, y, z, dist) {
    const from = { x: ctl.target.x, y: ctl.target.y, z: ctl.target.z, d: ctl.dist }, t0 = performance.now();
    dist = dist || ctl.dist;
    anim = now => {
      const k = Math.min(1, (now - t0) / 900), e = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
      ctl.target.set(from.x + (x - from.x) * e, from.y + (y - from.y) * e, from.z + (z - from.z) * e);
      ctl.dist = from.d + (dist - from.d) * e;
      if (k >= 1) anim = null;
    };
    needsRender = true;
  }
  function frameAll() {
    const v = index.view;
    if (v && v.target) {
      ctl.target.set(v.target[0], v.target[1], v.target[2]);
      if (v.distance) ctl.dist = v.distance;
      if (v.theta !== undefined) ctl.theta = v.theta;
      if (v.phi !== undefined) ctl.phi = v.phi;
      return;
    }
    const metas = Object.values(index.tiles);
    if (!metas.length) return;
    let x0 = Infinity, x1 = -Infinity, z0 = Infinity, z1 = -Infinity, y = 0;
    for (const m of metas) { x0 = Math.min(x0, m.x); z0 = Math.min(z0, m.z); x1 = Math.max(x1, m.x + m.width); z1 = Math.max(z1, m.z + m.depth); y += ((m.yMin || 0) + (m.yMax || 0)) / 2; }
    ctl.target.set((x0 + x1) / 2, y / metas.length, (z0 + z1) / 2);
    ctl.dist = Math.max(x1 - x0, z1 - z0) * 1.1;
  }

  const pointers = new Map();
  let pinch0 = null;
  const onDown = e => { canvas.setPointerCapture(e.pointerId); pointers.set(e.pointerId, { x: e.clientX, y: e.clientY, b: e.button, s: e.shiftKey }); anim = null; pinch0 = null; };
  const onUp = e => { pointers.delete(e.pointerId); pinch0 = null; };
  function pan(dx, dy) {
    const s = ctl.dist * 0.0015, f = new THREE.Vector3(Math.sin(ctl.theta), 0, Math.cos(ctl.theta)), r = new THREE.Vector3(f.z, 0, -f.x);
    ctl.target.addScaledVector(r, -dx * s).addScaledVector(f, -dy * s);
  }
  const onMove = e => {
    const p = pointers.get(e.pointerId);
    if (!p) return;
    const dx = e.clientX - p.x, dy = e.clientY - p.y;
    p.x = e.clientX; p.y = e.clientY;
    if (pointers.size >= 2) {
      const [a, b] = [...pointers.values()], d = Math.hypot(a.x - b.x, a.y - b.y);
      if (pinch0) ctl.dist = Math.max(10, Math.min(5000, ctl.dist * pinch0 / Math.max(d, 1)));
      pinch0 = d;
      pan(dx / 2, dy / 2);
    } else if (p.b === 2 || p.s) pan(dx, dy);
    else { ctl.theta -= dx * 0.005; ctl.phi = Math.max(0.12, Math.min(1.45, ctl.phi - dy * 0.005)); }
    needsRender = true;
  };
  const onWheel = e => { e.preventDefault(); anim = null; ctl.dist = Math.max(10, Math.min(5000, ctl.dist * Math.exp(e.deltaY * 0.001))); needsRender = true; };
  const onContext = e => e.preventDefault();
  canvas.style.touchAction = "none";
  canvas.addEventListener("pointerdown", onDown);
  canvas.addEventListener("pointerup", onUp);
  canvas.addEventListener("pointercancel", onUp);
  canvas.addEventListener("pointermove", onMove);
  canvas.addEventListener("wheel", onWheel, { passive: false });
  canvas.addEventListener("contextmenu", onContext);

  // ---- loop ----
  function resize() {
    const w = canvas.clientWidth || window.innerWidth, h = canvas.clientHeight || window.innerHeight;
    renderer.setSize(w, h, false);
    camera.aspect = w / Math.max(h, 1);
    camera.updateProjectionMatrix();
    needsRender = true;
  }
  window.addEventListener("resize", resize);
  const camDir = new THREE.Vector3();
  function render() {
    placeCamera();
    camera.getWorldDirection(camDir);
    const yaw = Math.atan2(-camDir.x, -camDir.z);
    for (const c of spriteGroup.children) c.rotation.y = yaw; // upright, camera-facing cards
    renderer.render(scene, camera);
    placeLabels();
    hook.frames++;
  }
  function loop(now) {
    if (disposed) return;
    if (anim) { anim(now); needsRender = true; }
    if (needsRender) { needsRender = false; render(); }
    raf = requestAnimationFrame(loop);
  }

  // ---- polling ----
  let polling = false;
  async function poll() {
    if (polling || disposed) return false;
    polling = true;
    try {
      const next = await fetchIndex();
      const changed = Object.keys(next.tiles).filter(n => !tiles.has(n) || tiles.get(n).meta.version !== next.tiles[n].version);
      const gone = [...tiles.keys()].filter(n => !(n in next.tiles));
      const nextKey = JSON.stringify([next.layers, next.labels, next.sprites, next.title, next.palette]);
      if (!changed.length && !gone.length && nextKey === sectionsKey) { hook.revision = next.revision; return false; }
      const paletteChanged = JSON.stringify(next.palette) !== JSON.stringify(index && index.palette);
      index = next;
      palette = index.palette.map(hexRgb);
      for (const n of gone) removeTile(n);
      for (const n of changed) await loadTile(n, index.tiles[n]);
      if (paletteChanged) for (const [n, t] of tiles) if (!changed.includes(n)) { t.base = baseColors(t.decoded); applyColors(t); }
      if (nextKey !== sectionsKey || changed.length) { sectionsKey = nextKey; rebuildSections(); }
      hook.tiles = tiles.size;
      hook.revision = index.revision;
      return true;
    } catch (err) {
      fail(err);
      return false;
    } finally {
      polling = false;
    }
  }

  const ready = (async () => {
    resize();
    index = await fetchIndex();
    palette = index.palette.map(hexRgb);
    for (const [name, meta] of Object.entries(index.tiles)) await loadTile(name, meta);
    sectionsKey = JSON.stringify([index.layers, index.labels, index.sprites, index.title, index.palette]);
    rebuildSections();
    frameAll();
    render();
    hook.revision = index.revision;
    hook.ready = true;
    raf = requestAnimationFrame(loop);
    if (pollMs > 0) pollTimer = setInterval(poll, pollMs);
  })().catch(err => { fail(err); throw err; });

  function dispose() {
    disposed = true;
    if (pollTimer) clearInterval(pollTimer);
    if (raf) cancelAnimationFrame(raf);
    for (const n of [...tiles.keys()]) removeTile(n);
    for (const m of spriteMats.values()) m.dispose();
    for (const t of texCache.values()) t.dispose();
    cardGeo.dispose(); material.dispose(); renderer.dispose();
    canvas.removeEventListener("pointerdown", onDown);
    canvas.removeEventListener("pointerup", onUp);
    canvas.removeEventListener("pointercancel", onUp);
    canvas.removeEventListener("pointermove", onMove);
    canvas.removeEventListener("wheel", onWheel);
    canvas.removeEventListener("contextmenu", onContext);
    window.removeEventListener("resize", resize);
  }

  hook.render = () => { render(); return hook.frames; };
  hook.poll = poll;
  return {
    ready, poll, flyTo, heightAt, scene, camera, renderer, setLayerVisible, setLabelsVisible, setSpritesVisible, render, dispose,
    get state() { return { index, tiles: tiles.size, target: ctl.target.toArray(), dist: ctl.dist }; },
  };
}
