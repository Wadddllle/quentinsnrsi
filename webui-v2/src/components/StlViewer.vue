<script setup>
// Minimal 3D viewer for the FINAL generated STL (the v2 plan's deliberate
// choice: view the pipeline output directly, no CityJSON loader). Loads the STL
// from its download URL, frames it, and lets the user orbit. Coordinates are
// real EPSG:3414 meters (tens of thousands), so we recenter the geometry to its
// own origin for stable camera/precision, exactly like the pipeline's own STL
// checks recenter for numerical sanity.
import { ref, watch, onMounted, onBeforeUnmount } from 'vue';
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { STLLoader } from 'three/examples/jsm/loaders/STLLoader.js';

const props = defineProps({
  url: { type: String, default: null },
});

const containerRef = ref(null);
const loading = ref(false);
const error = ref(null);
let renderer, scene, camera, controls, meshObj, animHandle;

function frameObject(geometry) {
  geometry.computeBoundingBox();
  const center = new THREE.Vector3();
  geometry.boundingBox.getCenter(center);
  // Recenter to origin (camera stability at ~30km SVY21 coords) AND rotate
  // Z-up (SVY21 elevation) -> Y-up so OrbitControls' default Y-up azimuth/polar
  // math behaves naturally. Setting camera.up=(0,0,1) instead (the previous
  // approach) is exactly what made left-drag rotate feel wrong / lock up: with a
  // non-default up vector OrbitControls' pole clamping and pivot go haywire.
  geometry.translate(-center.x, -center.y, -center.z);
  geometry.rotateX(-Math.PI / 2);
  geometry.computeBoundingBox();
  const size = new THREE.Vector3();
  geometry.boundingBox.getSize(size);
  const radius = Math.max(size.x, size.y, size.z) || 1;
  camera.near = radius / 1000;
  camera.far = radius * 100;
  camera.up.set(0, 1, 0); // default Y-up -- natural OrbitControls
  camera.position.set(radius * 0.8, radius * 0.7, radius * 0.8);
  camera.lookAt(0, 0, 0);
  camera.updateProjectionMatrix();
  controls.target.set(0, 0, 0);
  controls.minDistance = radius * 0.02;
  controls.maxDistance = radius * 30;
  controls.update();
}

async function loadStl(url) {
  if (!url) return;
  loading.value = true;
  error.value = null;
  try {
    const buffer = await (await fetch(url)).arrayBuffer();
    const geometry = new STLLoader().parse(buffer);
    if (meshObj) {
      scene.remove(meshObj);
      meshObj.geometry.dispose();
      meshObj.material.dispose();
    }
    geometry.computeVertexNormals();
    const material = new THREE.MeshStandardMaterial({
      color: 0x9fb3d1, metalness: 0.1, roughness: 0.8, side: THREE.DoubleSide,
      flatShading: false,
    });
    meshObj = new THREE.Mesh(geometry, material);
    scene.add(meshObj);
    frameObject(geometry);
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
  scene.background = new THREE.Color(0x0b1220);
  const w = el.clientWidth, h = el.clientHeight;
  camera = new THREE.PerspectiveCamera(50, w / h, 1, 1e6);
  renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setSize(w, h);
  el.appendChild(renderer.domElement);

  const hemi = new THREE.HemisphereLight(0xbcd0ff, 0x33384a, 1.1);
  scene.add(hemi);
  const dir = new THREE.DirectionalLight(0xffffff, 1.6);
  dir.position.set(1, -1, 2);
  scene.add(dir);

  controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;

  const loop = () => { animHandle = requestAnimationFrame(loop); controls.update(); renderer.render(scene, camera); };
  loop();

  window.addEventListener('resize', onResize);
  if (props.url) loadStl(props.url);
});

onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize);
  cancelAnimationFrame(animHandle);
  controls?.dispose();
  renderer?.dispose();
});

watch(() => props.url, (u) => { if (u) loadStl(u); });
</script>

<template>
  <div class="stl-viewer">
    <div ref="containerRef" class="stl-canvas"></div>
    <div v-if="loading" class="stl-overlay">loading STL…</div>
    <div v-if="error" class="stl-overlay err">STL load error: {{ error }}</div>
  </div>
</template>

<style scoped>
.stl-viewer { position: relative; width: 100%; height: 100%; }
.stl-canvas { width: 100%; height: 100%; }
.stl-canvas :deep(canvas) { display: block; }
.stl-overlay {
  position: absolute; top: 12px; left: 12px;
  background: rgba(11, 18, 32, 0.8); padding: 6px 12px; border-radius: 6px;
  border: 1px solid var(--border); color: var(--muted); font-size: 13px;
}
.stl-overlay.err { color: var(--danger); }
</style>
