// usd-web replay player: plays a `usd-web replay` directory (replay.json + UWR1 chunks) with three.js.
//
// The host page owns three.js and passes its namespace in, so the page pins the exact version:
//
//   import * as THREE from "https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js";
//   import { createReplayPlayer } from "./usd-web-replay.js";
//   const player = createReplayPlayer({ THREE, canvas, baseUrl: "replay/", controls: barElement });
//   await player.ready;
//
// `controls` (optional) receives a play/pause button, a timeline scrubber, a speed button and a
// tick readout, built with touch-sized targets. Strings from replay.json are only ever written
// with textContent. See ARCHITECTURE.md § Replays for the format and the full option list.

export const REPLAY_FORMAT = "usd-web-replay";
export const REPLAY_VERSION = 1;
export const CHUNK_MAGIC = "UWR1";
export const SPEEDS = [0.25, 0.5, 1, 2, 4, 8];

async function gunzip(bytes) {
  const u8 = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  if (u8.length < 2 || u8[0] !== 0x1f || u8[1] !== 0x8b) return u8;
  const stream = new Blob([u8]).stream().pipeThrough(new DecompressionStream("gzip"));
  return new Uint8Array(await new Response(stream).arrayBuffer());
}

/** Decode raw (gunzipped) UWR1 bytes against their replay.json into per-sample arrays:
 * [{sets: [{n, ids, q (Uint16 xyz), colors?, widths?}], fields: [Uint8Array rgb | null]}]. */
export function decodeChunk(u8, doc) {
  if (u8.length < 12) throw new Error("truncated replay chunk");
  const magic = String.fromCharCode(u8[0], u8[1], u8[2], u8[3]);
  if (magic !== CHUNK_MAGIC) throw new Error(`not a ${CHUNK_MAGIC} chunk`);
  const dv = new DataView(u8.buffer, u8.byteOffset, u8.byteLength);
  const count = dv.getUint32(4, true), first = dv.getUint32(8, true);
  const idBytes = doc.quant.idBytes === 4 ? 4 : 2;
  const copy = (Ctor, bytes, n) => {
    const out = new Ctor(n);
    new Uint8Array(out.buffer).set(u8.subarray(off, off + bytes));
    off += bytes;
    return out;
  };
  let off = 12;
  const samples = [];
  for (let s = 0; s < count; s++) {
    const sample = { sets: [], fields: [] };
    for (const set of doc.sets) {
      if (off + 4 > u8.length) throw new Error("truncated replay chunk");
      const n = dv.getUint32(off, true); off += 4;
      const e = { n };
      e.ids = idBytes === 4 ? copy(Uint32Array, 4 * n, n) : copy(Uint16Array, 2 * n, n);
      e.q = copy(Uint16Array, 6 * n, 3 * n);
      if (set.vertexColor) { e.colors = u8.slice(off, off + 3 * n); off += 3 * n; }
      if (set.vertexWidth) { e.widths = u8.slice(off, off + n); off += n; }
      sample.sets.push(e);
    }
    for (const f of doc.fields) {
      const present = u8[off]; off += 1;
      if (present) { const k = f.cols * f.rows * 3; sample.fields.push(u8.slice(off, off + k)); off += k; }
      else sample.fields.push(null);
    }
    samples.push(sample);
  }
  if (off !== u8.length) throw new Error(`replay chunk has ${u8.length - off} trailing bytes`);
  return { first, count, samples };
}

/** Index of the last element of sorted `a` that is <= x (-1 if none). */
export function floorIndex(a, x) {
  let lo = 0, hi = a.length - 1, r = -1;
  while (lo <= hi) { const m = (lo + hi) >> 1; if (a[m] <= x) { r = m; lo = m + 1; } else hi = m - 1; }
  return r;
}

export function createReplayPlayer(options) {
  const { THREE, canvas } = options;
  if (!THREE || !canvas) throw new Error("createReplayPlayer needs { THREE, canvas }");
  const baseUrl = new URL(options.baseUrl || "./", document.baseURI);
  const hookName = options.hook === undefined ? "__usdWebReplay" : options.hook;
  const reduced = options.reducedMotion !== undefined ? !!options.reducedMotion
    : !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  const hook = { ready: false, samples: 0, chunks: 0, loaded: 0, tick: null, hash: null, playing: false, frames: 0, errors: [] };
  if (hookName) window[hookName] = hook;

  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, preserveDrawingBuffer: !!options.preserveDrawingBuffer });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(options.background === undefined ? 0x1b2026 : options.background);
  const camera = new THREE.PerspectiveCamera(45, 1, 0.05, 100000);
  scene.add(new THREE.HemisphereLight(0xf2f6ff, 0x404040, 2.2));
  const sun = new THREE.DirectionalLight(0xffffff, 1.4);
  sun.position.set(0.4, 1, 0.6);
  scene.add(sun);

  let doc = null, disposed = false, raf = null, needsRender = true;
  const chunks = []; // index -> {first, count, samples} | Promise | undefined
  const sets = [];   // {mesh, cap, def}
  const fields = []; // {mesh, tex, data, def, shown}
  const geos = [], mats = [];
  let playing = false, speed = options.speed || 1, playhead = 0, lastNow = null, waiting = false;
  const fail = err => { hook.errors.push(String(err && err.message || err)); console.warn("usd-web replay:", err); };

  // ---- coordinates: replay is Z-up (x, y, z); three.js is Y-up -> (x, z, -y) ----
  function shapeGeo(shape) {
    const g = shape === "cube" ? new THREE.BoxGeometry(1, 1, 1) : new THREE.SphereGeometry(0.5, 14, 10);
    geos.push(g);
    return g;
  }
  function material(color) {
    // A "#rrggbb" string is read as sRGB, so the colour matches the legend swatch.
    const m = new THREE.MeshLambertMaterial({ color: color ? new THREE.Color(color) : 0xffffff });
    mats.push(m);
    return m;
  }

  function buildScene() {
    for (const sc of doc.scene || []) {
      const n = sc.points.length, vc = sc.colors.length > 1, vw = sc.widths.length > 1;
      const mesh = new THREE.InstancedMesh(shapeGeo(sc.shape), material(vc ? null : sc.colors[0]), Math.max(n, 1));
      const m = new THREE.Matrix4(), c = new THREE.Color();
      for (let i = 0; i < n; i++) {
        const [x, y, z] = sc.points[i], w = vw ? sc.widths[i] : sc.widths[0];
        m.makeScale(w, w, w).setPosition(x, z, -y);
        mesh.setMatrixAt(i, m);
        if (vc) mesh.setColorAt(i, c.set(sc.colors[i]));
      }
      mesh.count = n;
      scene.add(mesh);
    }
    for (const f of doc.fields) {
      const data = new Uint8Array(f.cols * f.rows * 4).fill(255);
      const tex = new THREE.DataTexture(data, f.cols, f.rows, THREE.RGBAFormat);
      tex.magFilter = THREE.NearestFilter; tex.minFilter = THREE.NearestFilter;
      if ("colorSpace" in tex && THREE.SRGBColorSpace) tex.colorSpace = THREE.SRGBColorSpace;
      const geo = new THREE.PlaneGeometry(f.cols * f.cellSize, f.rows * f.cellSize);
      geo.rotateX(-Math.PI / 2);
      geos.push(geo);
      const mat = new THREE.MeshBasicMaterial({ map: tex, side: THREE.DoubleSide });
      mats.push(mat);
      const mesh = new THREE.Mesh(geo, mat);
      mesh.position.set(f.origin[0] + f.cols * f.cellSize / 2, f.z, -(f.origin[1] + f.rows * f.cellSize / 2));
      scene.add(mesh);
      fields.push({ mesh, tex, data, def: f, shown: -1 });
    }
    for (const s of doc.sets) {
      const cap = Math.max(1, s.maxCount);
      const mesh = new THREE.InstancedMesh(shapeGeo(s.shape), material(s.vertexColor ? null : s.color), cap);
      mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
      if (s.vertexColor) mesh.setColorAt(0, new THREE.Color(1, 1, 1));
      mesh.count = 0;
      mesh.frustumCulled = false;
      scene.add(mesh);
      sets.push({ mesh, cap, def: s });
    }
  }

  // ---- data ----
  async function fetchDoc() {
    const r = await fetch(new URL("replay.json", baseUrl));
    if (!r.ok) throw new Error(`replay.json: HTTP ${r.status}`);
    const d = await r.json();
    if (d.format !== REPLAY_FORMAT) throw new Error("not a usd-web replay");
    if (!(d.version <= REPLAY_VERSION)) throw new Error(`replay version ${d.version} is newer than this player (${REPLAY_VERSION})`);
    return d;
  }
  function loadChunk(i) {
    if (chunks[i]) return chunks[i] instanceof Promise ? chunks[i] : Promise.resolve(chunks[i]);
    const meta = doc.chunks[i];
    const p = (async () => {
      const r = await fetch(new URL(meta.file, baseUrl));
      if (!r.ok) throw new Error(`${meta.file}: HTTP ${r.status}`);
      const c = decodeChunk(await gunzip(await r.arrayBuffer()), doc);
      chunks[i] = c;
      hook.loaded = chunks.filter(x => x && !(x instanceof Promise)).length;
      needsRender = true;
      return c;
    })();
    chunks[i] = p;
    p.catch(err => { chunks[i] = undefined; fail(err); });
    return p;
  }
  function chunkOf(sample) {
    let lo = 0, hi = doc.chunks.length - 1;
    while (lo < hi) { const m = (lo + hi + 1) >> 1; if (doc.chunks[m].first <= sample) lo = m; else hi = m - 1; }
    return lo;
  }
  function sampleData(k) {
    const ci = chunkOf(k), c = chunks[ci];
    if (!c || c instanceof Promise) return null;
    return c.samples[k - c.first];
  }

  // ---- drawing a moment ----
  const mtx = new THREE.Matrix4(), col = new THREE.Color();
  const idIndex = new Map(); // sample k -> per-set Map(id -> i), cached for interpolation
  function indexOf(k, si) {
    let per = idIndex.get(k);
    if (!per) { per = []; idIndex.set(k, per); if (idIndex.size > 64) idIndex.delete(idIndex.keys().next().value); }
    if (!per[si]) {
      const e = sampleData(k).sets[si], m = new Map();
      for (let i = 0; i < e.n; i++) m.set(e.ids[i], i);
      per[si] = m;
    }
    return per[si];
  }

  function drawAt(head) {
    // head: fractional sample index
    const k = Math.max(0, Math.min(doc.samples - 1, Math.floor(head)));
    const a = sampleData(k);
    if (!a) { waiting = true; loadChunk(chunkOf(k)); return false; }
    waiting = false;
    const frac = head - k, k2 = Math.min(doc.samples - 1, k + 1);
    const b = frac > 0 && k2 !== k && options.interpolate !== false ? sampleData(k2) : null;
    if (k2 !== k && !b && frac > 0) loadChunk(chunkOf(k2));
    const qm = doc.quant.min, qs = doc.quant.step;
    sets.forEach((S, si) => {
      const e = a.sets[si], def = S.def, n = Math.min(e.n, S.cap);
      const nb = b ? indexOf(k2, si) : null, eb = b ? b.sets[si] : null;
      for (let i = 0; i < n; i++) {
        let x = qm[0] + e.q[3 * i] * qs[0], y = qm[1] + e.q[3 * i + 1] * qs[1], z = qm[2] + e.q[3 * i + 2] * qs[2];
        if (nb) {
          const j = nb.get(e.ids[i]);
          if (j !== undefined) {
            x += (qm[0] + eb.q[3 * j] * qs[0] - x) * frac;
            y += (qm[1] + eb.q[3 * j + 1] * qs[1] - y) * frac;
            z += (qm[2] + eb.q[3 * j + 2] * qs[2] - z) * frac;
          }
        }
        const w = def.vertexWidth ? (e.widths[i] / 255) * def.widthMax : (def.width || 1);
        mtx.makeScale(w, w, w).setPosition(x, z, -y);
        S.mesh.setMatrixAt(i, mtx);
        if (def.vertexColor) S.mesh.setColorAt(i, col.setRGB(e.colors[3 * i] / 255, e.colors[3 * i + 1] / 255, e.colors[3 * i + 2] / 255, THREE.SRGBColorSpace));
      }
      S.mesh.count = n;
      S.mesh.instanceMatrix.needsUpdate = true;
      if (S.mesh.instanceColor) S.mesh.instanceColor.needsUpdate = true;
    });
    // Fields hold their latest sample at or before k (searching back within loaded data).
    fields.forEach((F, fi) => {
      let j = k;
      while (j >= 0) {
        const d = sampleData(j);
        if (!d) break;
        if (d.fields[fi]) break;
        j--;
      }
      if (j < 0 || j === F.shown) return;
      const d = sampleData(j);
      if (!d || !d.fields[fi]) return;
      const rgb = d.fields[fi], px = F.data;
      for (let i = 0, n = F.def.cols * F.def.rows; i < n; i++) { px[4 * i] = rgb[3 * i]; px[4 * i + 1] = rgb[3 * i + 1]; px[4 * i + 2] = rgb[3 * i + 2]; }
      F.tex.needsUpdate = true;
      F.shown = j;
    });
    const tick = doc.ticks[k];
    hook.tick = tick;
    hook.sample = k;
    hook.hash = doc.hashes[k];
    updateControls(k);
    if (options.onTick) options.onTick({ tick, sample: k, hash: doc.hashes[k], stats: statsAt(k) });
    return true;
  }
  function statsAt(k) {
    const out = {};
    for (const [name, series] of Object.entries(doc.stats || {})) out[name] = series[k];
    return out;
  }

  // ---- controls ----
  let ui = null;
  function buildControls() {
    const host = options.controls;
    if (!host) return;
    host.replaceChildren();
    host.classList.add("uwr-controls");
    const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; };
    const play = el("button", "uwr-btn uwr-play");
    play.type = "button";
    const range = el("input", "uwr-range");
    range.type = "range"; range.min = "0"; range.max = String(Math.max(0, doc.samples - 1)); range.step = "1"; range.value = "0";
    range.setAttribute("aria-label", "Timeline");
    const spd = el("button", "uwr-btn uwr-speed");
    spd.type = "button";
    spd.setAttribute("aria-label", "Playback speed");
    const label = el("span", "uwr-tick");
    label.setAttribute("aria-live", "off");
    play.onclick = () => (playing ? pause() : play_());
    spd.onclick = () => { const i = SPEEDS.indexOf(speed); setSpeed(SPEEDS[(i + 1) % SPEEDS.length]); };
    range.oninput = () => { seekSample(Number(range.value)); };
    host.append(play, range, spd, label);
    ui = { play, range, spd, label };
    updateControls(0);
  }
  function updateControls(k) {
    if (!ui) return;
    ui.play.textContent = playing ? "Pause" : "Play";
    ui.play.setAttribute("aria-label", playing ? "Pause" : "Play");
    ui.spd.textContent = `${speed}×`;
    if (document.activeElement !== ui.range || playing) ui.range.value = String(k);
    ui.label.textContent = `tick ${doc.ticks[k].toLocaleString()} / ${doc.ticks[doc.samples - 1].toLocaleString()}`;
  }

  // ---- playback ----
  // Playback runs in ticks: playbackRate ticks per second times the speed, mapped onto samples.
  function headToTick(h) {
    const k = Math.floor(h), f = h - k, t = doc.ticks;
    return k + 1 < t.length ? t[k] + (t[k + 1] - t[k]) * f : t[t.length - 1];
  }
  function tickToHead(tick) {
    const t = doc.ticks, k = floorIndex(t, tick);
    if (k < 0) return 0;
    if (k + 1 >= t.length) return t.length - 1;
    return k + (tick - t[k]) / (t[k + 1] - t[k]);
  }
  function play_() {
    if (!doc) return;
    if (playhead >= doc.samples - 1) playhead = 0;
    playing = true; hook.playing = true; lastNow = null; needsRender = true; updateControls(Math.floor(playhead));
  }
  function pause() { playing = false; hook.playing = false; needsRender = true; if (doc) updateControls(Math.floor(playhead)); }
  function setSpeed(s) { speed = s; hook.speed = s; if (doc) updateControls(Math.floor(playhead)); }
  function seekSample(k) { playhead = Math.max(0, Math.min(doc.samples - 1, k)); needsRender = true; }
  function seek(tick) { seekSample(tickToHead(tick)); }
  function advance(now) {
    if (!playing) { lastNow = null; return; }
    if (lastNow === null) { lastNow = now; return; }
    const dt = Math.min(0.25, (now - lastNow) / 1000);
    lastNow = now;
    if (waiting) return;
    const tick = headToTick(playhead) + dt * doc.playbackRate * speed;
    const end = doc.ticks[doc.samples - 1];
    if (tick >= end) {
      if (options.loop !== false) { playhead = 0; } else { playhead = doc.samples - 1; pause(); }
    } else playhead = tickToHead(tick);
    needsRender = true;
  }

  // ---- camera ----
  const ctl = { target: new THREE.Vector3(), dist: 50, theta: 0.6, phi: 0.75 };
  function placeCamera() {
    const sp = Math.sin(ctl.phi);
    camera.position.set(ctl.target.x + ctl.dist * sp * Math.sin(ctl.theta), ctl.target.y + ctl.dist * Math.cos(ctl.phi),
      ctl.target.z + ctl.dist * sp * Math.cos(ctl.theta));
    camera.lookAt(ctl.target);
  }
  function frameAll() {
    const b = doc.bounds, v = doc.view || {};
    const cx = (b.min[0] + b.max[0]) / 2, cy = (b.min[1] + b.max[1]) / 2, cz = (b.min[2] + b.max[2]) / 2;
    ctl.target.set(cx, cz, -cy);
    const size = Math.max(b.max[0] - b.min[0], b.max[1] - b.min[1], b.max[2] - b.min[2], 1e-3);
    // Fit the bounds' diagonal in the narrower of the two fields of view (portrait phones).
    const half = Math.tan((camera.fov * Math.PI) / 360), fit = Math.min(half, half * camera.aspect);
    const radius = 0.5 * Math.hypot(b.max[0] - b.min[0], b.max[1] - b.min[1], b.max[2] - b.min[2]);
    ctl.dist = Math.max(radius, 1e-3) / fit * (options.fitPadding || (camera.aspect < 1 ? 1.0 : 0.85));
    if (v.target) ctl.target.set(v.target[0], v.target[2], -v.target[1]);
    if (v.distance) ctl.dist = v.distance / Math.min(1, camera.aspect);
    if (v.theta !== undefined) ctl.theta = v.theta;
    if (v.phi !== undefined) ctl.phi = v.phi;
    camera.near = Math.max(size / 5000, 1e-4);
    camera.far = size * 50;
    camera.updateProjectionMatrix();
    ctl.min = size * 0.05; ctl.max = size * 8;
  }
  const pointers = new Map();
  let pinch0 = null;
  let userMoved = false;
  const onDown = e => { userMoved = true; canvas.setPointerCapture(e.pointerId); pointers.set(e.pointerId, { x: e.clientX, y: e.clientY, b: e.button, s: e.shiftKey }); pinch0 = null; };
  const onUp = e => { pointers.delete(e.pointerId); pinch0 = null; };
  function pan(dx, dy) {
    const s = ctl.dist * 0.0015, f = new THREE.Vector3(Math.sin(ctl.theta), 0, Math.cos(ctl.theta)), r = new THREE.Vector3(f.z, 0, -f.x);
    ctl.target.addScaledVector(r, -dx * s).addScaledVector(f, -dy * s);
  }
  const clampDist = d => Math.max(ctl.min || 0.1, Math.min(ctl.max || 1e5, d));
  const onMove = e => {
    const p = pointers.get(e.pointerId);
    if (!p) return;
    const dx = e.clientX - p.x, dy = e.clientY - p.y;
    p.x = e.clientX; p.y = e.clientY;
    if (pointers.size >= 2) {
      const [a, b] = [...pointers.values()], d = Math.hypot(a.x - b.x, a.y - b.y);
      if (pinch0) ctl.dist = clampDist(ctl.dist * pinch0 / Math.max(d, 1));
      pinch0 = d;
      pan(dx / 2, dy / 2);
    } else if (p.b === 2 || p.s) pan(dx, dy);
    else { ctl.theta -= dx * 0.005; ctl.phi = Math.max(0.05, Math.min(1.5, ctl.phi - dy * 0.005)); }
    needsRender = true;
  };
  const onWheel = e => { e.preventDefault(); userMoved = true; ctl.dist = clampDist(ctl.dist * Math.exp(e.deltaY * 0.001)); needsRender = true; };
  const onContext = e => e.preventDefault();
  const onKey = e => {
    // Buttons and text inputs handle their own keys; the scrubber keeps its arrow keys.
    if (!doc || e.defaultPrevented || e.target && /^(BUTTON|TEXTAREA|SELECT)$/.test(e.target.tagName)) return;
    if (e.target && e.target.tagName === "INPUT" && e.target !== (ui && ui.range)) return;
    if (e.key === " " || e.key === "k") { e.preventDefault(); playing ? pause() : play_(); }
    else if (e.key === "ArrowRight" && e.target !== (ui && ui.range)) { pause(); seekSample(Math.floor(playhead) + 1); }
    else if (e.key === "ArrowLeft" && e.target !== (ui && ui.range)) { pause(); seekSample(Math.ceil(playhead) - 1); }
  };
  canvas.style.touchAction = "none";
  canvas.tabIndex = canvas.tabIndex >= 0 ? canvas.tabIndex : 0;
  canvas.addEventListener("pointerdown", onDown);
  canvas.addEventListener("pointerup", onUp);
  canvas.addEventListener("pointercancel", onUp);
  canvas.addEventListener("pointermove", onMove);
  canvas.addEventListener("wheel", onWheel, { passive: false });
  canvas.addEventListener("contextmenu", onContext);
  const keyHost = options.keyboardTarget || canvas;
  keyHost.addEventListener("keydown", onKey);
  const keysOnControls = options.controls && !keyHost.contains(options.controls);
  if (keysOnControls) options.controls.addEventListener("keydown", onKey);

  // ---- loop ----
  function resize() {
    const w = canvas.clientWidth || window.innerWidth, h = canvas.clientHeight || window.innerHeight;
    renderer.setSize(w, h, false);
    camera.aspect = w / Math.max(h, 1);
    camera.updateProjectionMatrix();
    if (doc && !userMoved) frameAll();
    needsRender = true;
  }
  const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(resize) : null;
  if (ro) ro.observe(canvas); else window.addEventListener("resize", resize);
  function render() {
    if (doc) drawAt(playhead);
    placeCamera();
    renderer.render(scene, camera);
    hook.frames++;
  }
  function loop(now) {
    if (disposed) return;
    advance(now);
    if (needsRender || waiting) { needsRender = false; render(); }
    raf = requestAnimationFrame(loop);
  }

  const ready = (async () => {
    resize();
    doc = await fetchDoc();
    hook.samples = doc.samples;
    hook.chunks = doc.chunks.length;
    buildScene();
    frameAll();
    await loadChunk(0);
    buildControls();
    render();
    hook.ready = true;
    if (options.start !== undefined) seek(options.start);
    if (options.autoplay !== false && !reduced) play_();
    raf = requestAnimationFrame(loop);
    // Fetch the rest in order, one at a time, so playback can start on the first chunk.
    for (let i = 1; i < doc.chunks.length && !disposed; i++) await loadChunk(i).catch(() => {});
    return doc;
  })().catch(err => { fail(err); throw err; });

  function dispose() {
    disposed = true;
    if (raf) cancelAnimationFrame(raf);
    if (ro) ro.disconnect(); else window.removeEventListener("resize", resize);
    for (const F of fields) F.tex.dispose();
    for (const S of sets) S.mesh.dispose();
    for (const g of geos) g.dispose();
    for (const m of mats) m.dispose();
    renderer.dispose();
    canvas.removeEventListener("pointerdown", onDown);
    canvas.removeEventListener("pointerup", onUp);
    canvas.removeEventListener("pointercancel", onUp);
    canvas.removeEventListener("pointermove", onMove);
    canvas.removeEventListener("wheel", onWheel);
    canvas.removeEventListener("contextmenu", onContext);
    keyHost.removeEventListener("keydown", onKey);
    if (keysOnControls) options.controls.removeEventListener("keydown", onKey);
    if (options.controls) options.controls.replaceChildren();
  }

  hook.play = () => play_();
  hook.pause = () => pause();
  hook.seek = t => seek(t);
  hook.render = () => { render(); return hook.frames; };
  return {
    ready, play: play_, pause, seek, seekSample, setSpeed, render, dispose, scene, camera, renderer,
    get state() { return { doc, playing, speed, sample: doc ? Math.floor(playhead) : 0, tick: hook.tick, hash: hook.hash }; },
  };
}
