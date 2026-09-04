<script setup>
// Viewer for the generated GeoTIFF. A browser cannot decode a GeoTIFF, so the backend
// ships the raw height grid instead (16-byte header + row-major float32) and the surface
// is built here. Same conventions as StlViewer: recentre to the origin, Z-up -> Y-up so
// OrbitControls behaves, neutral grey background.
import { ref, watch, onMounted, onBeforeUnmount } from 'vue';
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';

const props = defineProps({ url: { type: String, default: null } });

const containerRef = ref(null);
const loading = ref(false);
const error = ref(null);
const stats = ref(null);
const shade = ref('height');   // 'height' | 'grey'
const mode = ref('stepped');   // 'stepped' = what the file says; 'smooth' = interpolated
let renderer, scene, camera, controls, meshObj, animHandle;

// blue -> green -> tan -> brown -> white, the usual elevation read
const RAMP = [
  [0.00, 0x2c6fa8],
  [0.35, 0x3f8f4f],
  [0.62, 0xc8bd6a],
  [0.82, 0x9c6b46],
  [1.00, 0xf2f2f2],
];
function ramp(t) {
  for (let i = 1; i < RAMP.length; i++) {
    if (t <= RAMP[i][0]) {
      const [t0, c0] = RAMP[i - 1];
      const [t1, c1] = RAMP[i];
      const k = (t - t0) / (t1 - t0 || 1);
      return new THREE.Color(c0).lerp(new THREE.Color(c1), k);
    }
  }
  return new THREE.Color(RAMP[RAMP.length - 1][1]);
}

function geomStepped(z, ncols, nrows, px, zmin, zmax) {
  // What the file ACTUALLY says: one constant height per pixel. So each pixel is a flat
  // square at its own height, joined to its neighbours by VERTICAL walls. Joining pixel
  // centres with shared vertices instead (the obvious heightfield mesh) turns every
  // ground-to-roof jump into a sloped ramp, which makes the data look finer than it is.
  const span = (zmax - zmin) || 1;
  const w = (ncols - 1) * px, h = (nrows - 1) * px;
  const base = zmin - span * 0.03;
  // Upper bound, then subarray -- a pre-sized buffer that overruns is SILENTLY truncated,
  // which has shipped two real bugs in this project, so it also gets a loud guard below.
  const maxQuads = ncols * nrows + (ncols - 1) * nrows + (nrows - 1) * ncols
                 + 2 * (ncols + nrows);
  const pos = new Float32Array(maxQuads * 12);
  const col = new Float32Array(maxQuads * 12);
  const idx = new Uint32Array(maxQuads * 6);
  let vp = 0, ip = 0, nv = 0;

  const push = (x0, y0, z0, x1, y1, z1, x2, y2, z2, x3, y3, z3, c) => {
    const xs = [x0, x1, x2, x3], ys = [y0, y1, y2, y3], zs = [z0, z1, z2, z3];
    for (let i = 0; i < 4; i++) {
      pos[vp] = xs[i]; pos[vp + 1] = ys[i]; pos[vp + 2] = zs[i];
      col[vp] = c.r; col[vp + 1] = c.g; col[vp + 2] = c.b;
      vp += 3;
    }
    idx[ip++] = nv; idx[ip++] = nv + 1; idx[ip++] = nv + 2;
    idx[ip++] = nv; idx[ip++] = nv + 2; idx[ip++] = nv + 3;
    nv += 4;
  };
  const X = (c) => c * px - w / 2, Y = (r) => -(r * px - h / 2);
  const half = px / 2;
  for (let r = 0; r < nrows; r++) {
    for (let c = 0; c < ncols; c++) {
      const zz = z[r * ncols + c];
      const x = X(c), y = Y(r);
      const cc = shade.value === 'height' ? ramp((zz - zmin) / span)
        : new THREE.Color().setScalar(0.35 + 0.5 * ((zz - zmin) / span));
      // flat top, wound CCW seen from above
      push(x - half, y - half, zz, x + half, y - half, zz,
           x + half, y + half, zz, x - half, y + half, zz, cc);
    }
  }
  // vertical walls between neighbours, darker so the sides read as sides
  const wallCol = (zh) => (shade.value === 'height' ? ramp((zh - zmin) / span)
    : new THREE.Color().setScalar(0.35 + 0.5 * ((zh - zmin) / span))).multiplyScalar(0.72);
  for (let r = 0; r < nrows; r++) {
    for (let c = 0; c < ncols - 1; c++) {
      const za = z[r * ncols + c], zb = z[r * ncols + c + 1];
      if (Math.abs(za - zb) < 1e-4) continue;
      const x = X(c) + half, y = Y(r);
      push(x, y - half, za, x, y + half, za, x, y + half, zb, x, y - half, zb,
           wallCol(Math.max(za, zb)));
    }
  }
  for (let r = 0; r < nrows - 1; r++) {
    for (let c = 0; c < ncols; c++) {
      const za = z[r * ncols + c], zb = z[(r + 1) * ncols + c];
      if (Math.abs(za - zb) < 1e-4) continue;
      const x = X(c), y = Y(r) - half;
      push(x - half, y, za, x + half, y, za, x + half, y, zb, x - half, y, zb,
           wallCol(Math.max(za, zb)));
    }
  }
  // skirt down to a common base, so it reads as a solid block rather than a floating sheet
  const skirt = new THREE.Color(0x4a4f55);
  for (let c = 0; c < ncols; c++) {
    const zt = z[c], x = X(c);
    push(x - half, Y(0) + half, zt, x + half, Y(0) + half, zt,
         x + half, Y(0) + half, base, x - half, Y(0) + half, base, skirt);
    const zb2 = z[(nrows - 1) * ncols + c];
    push(x - half, Y(nrows - 1) - half, zb2, x + half, Y(nrows - 1) - half, zb2,
         x + half, Y(nrows - 1) - half, base, x - half, Y(nrows - 1) - half, base, skirt);
  }
  for (let r = 0; r < nrows; r++) {
    const zl = z[r * ncols], y = Y(r);
    push(X(0) - half, y - half, zl, X(0) - half, y + half, zl,
         X(0) - half, y + half, base, X(0) - half, y - half, base, skirt);
    const zr = z[r * ncols + ncols - 1];
    push(X(ncols - 1) + half, y - half, zr, X(ncols - 1) + half, y + half, zr,
         X(ncols - 1) + half, y + half, base, X(ncols - 1) + half, y - half, base, skirt);
  }
  if (vp > pos.length || ip > idx.length) console.error('DEM viewer buffer overrun', vp, ip);

  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(pos.subarray(0, vp), 3));
  g.setAttribute('color', new THREE.BufferAttribute(col.subarray(0, vp), 3));
  g.setIndex(new THREE.BufferAttribute(idx.subarray(0, ip), 1));
  return { g, tris: ip / 3 };
}

function geomSmooth(z, ncols, nrows, px, zmin, zmax) {
  const span = (zmax - zmin) || 1;
  const w = (ncols - 1) * px, h = (nrows - 1) * px;
  const pos = new Float32Array(ncols * nrows * 3);
  const col = new Float32Array(ncols * nrows * 3);
  for (let r = 0, v = 0; r < nrows; r++) {
    for (let c = 0; c < ncols; c++, v++) {
      const zz = z[r * ncols + c];
      pos[v * 3] = c * px - w / 2;
      pos[v * 3 + 1] = -(r * px - h / 2);
      pos[v * 3 + 2] = zz;
      const t = (zz - zmin) / span;
      const cc = shade.value === 'height' ? ramp(t) : new THREE.Color().setScalar(0.35 + 0.5 * t);
      col[v * 3] = cc.r; col[v * 3 + 1] = cc.g; col[v * 3 + 2] = cc.b;
    }
  }
  const idx = new Uint32Array((ncols - 1) * (nrows - 1) * 6);
  let k = 0;
  for (let r = 0; r < nrows - 1; r++) {
    for (let c = 0; c < ncols - 1; c++) {
      const a = r * ncols + c, b = a + 1, d = a + ncols, e = d + 1;
      idx[k++] = a; idx[k++] = d; idx[k++] = b;
      idx[k++] = b; idx[k++] = d; idx[k++] = e;
    }
  }
  if (k !== idx.length) console.error('DEM index buffer miscount', k, idx.length);
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  g.setAttribute('color', new THREE.BufferAttribute(col, 3));
  g.setIndex(new THREE.BufferAttribute(idx, 1));
  return { g, tris: k / 3 };
}

function build(buffer) {
  const dv = new DataView(buffer);
  const ncols = dv.getInt32(0, true);
  const nrows = dv.getInt32(4, true);
  const px = dv.getFloat64(8, true);
  const z = new Float32Array(buffer, 16, ncols * nrows);

  let zmin = Infinity, zmax = -Infinity;
  for (let i = 0; i < z.length; i++) {
    if (z[i] < zmin) zmin = z[i];
    if (z[i] > zmax) zmax = z[i];
  }
  // Stepped costs ~6x the triangles of the smooth surface, so a very large raster falls
  // back rather than stalling the tab. Reported in the HUD so it is never a silent swap.
  const stepped = mode.value === 'stepped' && ncols * nrows <= 400000;
  const { g, tris } = stepped
    ? geomStepped(z, ncols, nrows, px, zmin, zmax)
    : geomSmooth(z, ncols, nrows, px, zmin, zmax);
  stats.value = { ncols, nrows, px, zmin, zmax, tris,
                  downgraded: mode.value === 'stepped' && !stepped };

  g.computeVertexNormals();
  g.rotateX(-Math.PI / 2);
  g.translate(0, -(zmin + zmax) / 2, 0);

  if (meshObj) {
    scene.remove(meshObj);
    meshObj.geometry.dispose();
    meshObj.material.dispose();
  }
  meshObj = new THREE.Mesh(g, new THREE.MeshStandardMaterial({
    vertexColors: true, metalness: 0.0, roughness: 0.95, side: THREE.DoubleSide,
    flatShading: true,
  }));
  scene.add(meshObj);

  const w = (ncols - 1) * px, h = (nrows - 1) * px;
  const radius = Math.max(w, h) || 1;
  camera.near = radius / 1000;
  camera.far = radius * 100;
  camera.up.set(0, 1, 0);
  camera.position.set(radius * 0.55, radius * 0.55, radius * 0.75);
  camera.lookAt(0, 0, 0);
  camera.updateProjectionMatrix();
  controls.target.set(0, 0, 0);
  controls.minDistance = radius * 0.02;
  controls.maxDistance = radius * 30;
  controls.update();
}

async function load(url) {
  if (!url) return;
  loading.value = true; error.value = null;
  try {
    build(await (await fetch(url)).arrayBuffer());
  } catch (e) {
    error.value = e.message || String(e);
  } finally {
    loading.value = false;
  }
}

function onResize() {
  const el = containerRef.value;
  if (!el || !renderer) return;
  const w = el.clientWidth, h = el.clientHeight;
  renderer.setSize(w, h);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}

onMounted(() => {
  const el = containerRef.value;
  scene = new THREE.Scene();
  scene.background = new THREE.Color(0x3c3f43);
  const w = el.clientWidth, h = el.clientHeight;
  camera = new THREE.PerspectiveCamera(50, w / h, 1, 1e6);
  renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setSize(w, h);
  el.appendChild(renderer.domElement);
  scene.add(new THREE.HemisphereLight(0xffffff, 0x707070, 1.0));
  const dir = new THREE.DirectionalLight(0xffffff, 1.5);
  dir.position.set(1, 2, 1);
  scene.add(dir);
  controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  const loop = () => {
    animHandle = requestAnimationFrame(loop);
    controls.update();
    renderer.render(scene, camera);
  };
  loop();
  window.addEventListener('resize', onResize);
  if (props.url) load(props.url);
});

onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize);
  cancelAnimationFrame(animHandle);
  controls?.dispose();
  renderer?.dispose();
});

watch(() => props.url, (u) => { if (u) load(u); });
watch([shade, mode], () => { if (props.url) load(props.url); });
</script>

<template>
  <div class="demview">
    <div class="demcanvas" ref="containerRef"></div>
    <div class="demhud" v-if="stats">
      {{ stats.ncols }} × {{ stats.nrows }} px · {{ stats.px }} m ·
      {{ stats.zmin.toFixed(0) }}–{{ stats.zmax.toFixed(0) }} m
      <button class="link" @click="mode = mode === 'stepped' ? 'smooth' : 'stepped'">
        {{ mode === 'stepped' ? 'smooth' : 'stepped' }}
      </button>
      <button class="link" @click="shade = shade === 'height' ? 'grey' : 'height'">
        {{ shade === 'height' ? 'grey' : 'colour' }}
      </button>
      <span v-if="stats.downgraded" class="warnhud">
        too large to step — showing interpolated
      </span>
    </div>
    <div class="demhud" v-if="loading">loading…</div>
    <div class="demhud err" v-if="error">{{ error }}</div>
  </div>
</template>

<style scoped>
.demview { position: relative; width: 100%; height: 100%; }
.demcanvas { width: 100%; height: 100%; }
.demhud {
  position: absolute; left: 10px; bottom: 10px;
  background: rgba(0, 0, 0, 0.55); color: #eee;
  padding: 4px 9px; border-radius: 5px; font-size: 11px;
}
.demhud .link { margin-left: 8px; }
.demhud.err { color: #ffb4b4; }
.warnhud { color: #ffd28a; margin-left: 8px; }
</style>
