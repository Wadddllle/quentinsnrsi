<script setup>
// WebGL replacement for the old Canvas2D OrthoPlanView.vue (see project
// plan, "2D/3D rendering architecture redesign"). Root cause of the old
// approach's real-world lagginess: Canvas2D redraw is CPU-bound work that
// scales with visible feature count -- even after LOD/rAF/drag-blit
// patches, a real full redraw still meant per-vertex JS projection math and
// per-ring beginPath/moveTo/lineTo/fill/stroke Canvas2D API calls. This
// component instead triangulates every footprint ONCE at load time (earcut,
// already an installed dependency, pulled transitively via
// cityjson-threejs-loader) into a SINGLE merged BufferGeometry with a
// per-vertex color attribute, uploads it to the GPU once, and lets
// MapControls' camera-matrix updates handle pan/zoom -- work that does not
// scale with feature count at all. One mesh, one draw call, regardless of
// how much of the island is visible.
import { ref, shallowRef, watch, onMounted, onBeforeUnmount } from 'vue';
import * as THREE from 'three';
import earcut from 'earcut';
import { MapControls } from 'three/examples/jsm/controls/MapControls.js';
import { Line2 } from 'three/examples/jsm/lines/Line2.js';
import { LineGeometry } from 'three/examples/jsm/lines/LineGeometry.js';
import { LineMaterial } from 'three/examples/jsm/lines/LineMaterial.js';

const props = defineProps({
	footprints: { type: Array, default: () => [] },
	ring: { type: Array, default: () => [] },
	keptIds: { type: Array, default: () => [] },
	crossingIds: { type: Array, default: () => [] },
});

const emit = defineEmits(['click', 'view-changed']);

const containerRef = ref(null);
let renderer, scene, camera, controls, mesh, colorAttr;
let vertexRanges = {}; // buildingId -> [startVertex, endVertexExclusive]
let hasAutoFitted = false;
let fullExtentBounds = null; // {xmin,ymin,xmax,ymax} of all loaded footprints, for resetView()
let ringLine = null;
let boundaryOnly = false; // true while a drag gesture is in progress on controls -- avoids fighting MapControls' own render loop

const DEFAULT_COLOR = [150 / 255, 160 / 255, 180 / 255];
const KEPT_COLOR = [74 / 255, 158 / 255, 255 / 255];
const CROSSING_COLOR = [255 / 255, 99 / 255, 71 / 255];

function buildMergedGeometry(footprints) {
	// Pre-sized typed arrays instead of plain-array .push(): at 118k
	// buildings/millions of vertices, growing a JS array past its backing
	// store's capacity repeatedly forces V8 to reallocate-and-copy, real,
	// avoidable overhead at this scale. Upper bound computed from the input
	// (not a guessed constant): earcut triangulating a simple N-point ring
	// never produces more than N-2 triangles = 3*(N-2) output VERTEX slots
	// (non-indexed triangle soup -- 3 vertices per triangle, no dedup), so
	// summing 3*(N-2) across all rings is the real bound.
	//
	// BUG FOUND (real, shipped, caught by user report of "half of SG
	// missing"): this previously summed plain ringLength (N), not 3*(N-2) --
	// undersizing the buffer for any ring with more than a handful of
	// points. TypedArray writes past the allocated length are silently
	// DROPPED (no error, no auto-resize, unlike a plain Array), so once the
	// undersized buffer filled up, every building processed after that
	// point wrote its geometry nowhere -- collapsing to (0,0,0) instead of
	// its real coordinates, i.e. invisible, for the rest of the dataset.
	// Fixed the formula and added the runtime guard below so any future
	// miscalculation fails loudly instead of silently dropping geometry.
	let maxVerts = 0;
	for (const b of footprints) {
		for (const polyRing of b.rings) {
			if (polyRing.length >= 3) maxVerts += 3 * (polyRing.length - 2);
		}
	}

	const positions = new Float32Array(maxVerts * 3);
	const colors = new Float32Array(maxVerts * 3);
	const ranges = {};
	let ptr = 0; // float index into positions/colors (3 floats per vertex)
	let vertexCursor = 0;

	for (const b of footprints) {
		const startVertex = vertexCursor;
		for (const polyRing of b.rings) {
			if (polyRing.length < 3) continue;
			const flat = [];
			for (const [x, y] of polyRing) flat.push(x, y);
			const triIndices = earcut(flat);
			for (const idx of triIndices) {
				positions[ptr] = flat[idx * 2];
				positions[ptr + 1] = flat[idx * 2 + 1];
				positions[ptr + 2] = 0;
				colors[ptr] = DEFAULT_COLOR[0];
				colors[ptr + 1] = DEFAULT_COLOR[1];
				colors[ptr + 2] = DEFAULT_COLOR[2];
				ptr += 3;
				vertexCursor++;
			}
		}
		ranges[b.id] = [startVertex, vertexCursor];
	}

	// Fail loudly, not silently, if the upper-bound math above is ever wrong
	// again (e.g. a future earcut version, or malformed input producing more
	// triangles than the simple-polygon bound assumes) -- this is exactly
	// the check that would have caught the bug described above immediately
	// instead of shipping "half of Singapore is just missing" with zero
	// error output.
	if (ptr > positions.length) {
		console.error(`buildMergedGeometry: wrote ${ptr} floats past a ${positions.length}-float buffer -- geometry was silently truncated. Fix the maxVerts upper-bound calculation.`);
	}

	const geometry = new THREE.BufferGeometry();
	geometry.setAttribute('position', new THREE.BufferAttribute(positions.subarray(0, ptr), 3));
	geometry.setAttribute('color', new THREE.BufferAttribute(colors.subarray(0, ptr), 3));
	return { geometry, ranges };
}

let previouslyHighlighted = new Set(); // building ids painted non-default by the last applySelectionColors() call

function applySelectionColors() {
	if (!colorAttr) return;
	const arr = colorAttr.array;

	const paintRange = (range, color) => {
		for (let v = range[0]; v < range[1]; v++) {
			const i = v * 3;
			arr[i] = color[0]; arr[i + 1] = color[1]; arr[i + 2] = color[2];
		}
	};

	// Only touch what actually changed: revert ids that were highlighted
	// last time but aren't anymore, then paint the current selection --
	// instead of resetting all 118,780 buildings' vertices on every click,
	// which is real, avoidable work even though a plain typed-array fill is
	// individually cheap per element (a few million writes on every
	// keptIds/crossingIds change adds up during, e.g., rapid boundary-point
	// placement while drawing).
	const nextHighlighted = new Set([...props.keptIds, ...props.crossingIds]);
	for (const id of previouslyHighlighted) {
		if (nextHighlighted.has(id)) continue;
		const range = vertexRanges[id];
		if (range) paintRange(range, DEFAULT_COLOR);
	}
	previouslyHighlighted = nextHighlighted;

	const paint = (ids, color) => {
		for (const id of ids) {
			const range = vertexRanges[id];
			if (!range) continue;
			paintRange(range, color);
		}
	};
	paint(props.keptIds, KEPT_COLOR);
	paint(props.crossingIds, CROSSING_COLOR);
	colorAttr.needsUpdate = true;
}

function rebuildGeometry() {
	if (mesh) {
		scene.remove(mesh);
		mesh.geometry.dispose();
		mesh.material.dispose();
	}
	const { geometry, ranges } = buildMergedGeometry(props.footprints);
	vertexRanges = ranges;
	colorAttr = geometry.getAttribute('color');
	const material = new THREE.MeshBasicMaterial({ vertexColors: true, side: THREE.DoubleSide });
	mesh = new THREE.Mesh(geometry, material);
	scene.add(mesh);
	applySelectionColors();

	if (!hasAutoFitted && props.footprints.length > 0) {
		hasAutoFitted = true;
		let xmin = Infinity, xmax = -Infinity, ymin = Infinity, ymax = -Infinity;
		const pos = geometry.getAttribute('position');
		for (let i = 0; i < pos.count; i++) {
			const x = pos.getX(i), y = pos.getY(i);
			if (x < xmin) xmin = x; if (x > xmax) xmax = x;
			if (y < ymin) ymin = y; if (y > ymax) ymax = y;
		}
		fullExtentBounds = { xmin, ymin, xmax, ymax };
		fitBounds(xmin, ymin, xmax, ymax, 0.02);
	}
}

// "Zoom hell" fix, same underlying complaint as the 3D view's resetView():
// MapControls' zoom can leave the camera absurdly far in or out with no
// obvious way back. Re-fit to the full loaded extent computed once above,
// callable on demand instead of only at initial load.
function resetView() {
	if (!fullExtentBounds) return;
	const { xmin, ymin, xmax, ymax } = fullExtentBounds;
	fitBounds(xmin, ymin, xmax, ymax, 0.02);
}

function lineResolution() {
	const el = containerRef.value;
	return new THREE.Vector2(el.clientWidth, el.clientHeight);
}

function updateRingLine() {
	if (ringLine) {
		scene.remove(ringLine);
		ringLine.geometry.dispose();
		ringLine.material.dispose();
		ringLine = null;
	}
	if (!props.ring || props.ring.length < 2) return;
	const points = props.ring.map(([x, y]) => new THREE.Vector3(x, y, 1));
	points.push(points[0]);
	const flat = [];
	for (const p of points) flat.push(p.x, p.y, p.z);
	const geometry = new LineGeometry();
	geometry.setPositions(flat);
	const material = new LineMaterial({ color: 0xffd23f, linewidth: 3, resolution: lineResolution() });
	ringLine = new Line2(geometry, material);
	ringLine.computeLineDistances();
	scene.add(ringLine);
}

function render() {
	renderer.render(scene, camera);
}

function fitBounds(xmin, ymin, xmax, ymax, paddingFraction = 0.08) {
	const el = containerRef.value;
	if (!el) return;
	const w = el.clientWidth, h = el.clientHeight;
	if (w === 0 || h === 0) return;
	const spanX = (xmax - xmin) * (1 + paddingFraction * 2) || 1;
	const spanY = (ymax - ymin) * (1 + paddingFraction * 2) || 1;
	const cx = (xmin + xmax) / 2, cy = (ymin + ymax) / 2;

	const aspect = w / h;
	let frustumW = spanX, frustumH = spanX / aspect;
	if (frustumH < spanY) { frustumH = spanY; frustumW = spanY * aspect; }
	camera.left = -frustumW / 2;
	camera.right = frustumW / 2;
	camera.top = frustumH / 2;
	camera.bottom = -frustumH / 2;
	camera.position.set(cx, cy, 1000);
	camera.zoom = 1;
	camera.updateProjectionMatrix();
	controls.target.set(cx, cy, 0);
	controls.update();
	render();
	emit('view-changed', getViewBounds());
}

function flyTo(x, y, halfWidth = 300) {
	fitBounds(x - halfWidth, y - halfWidth, x + halfWidth, y + halfWidth, 0);
}

function getViewBounds() {
	const w = (camera.right - camera.left) / camera.zoom;
	const h = (camera.top - camera.bottom) / camera.zoom;
	const cx = controls.target.x, cy = controls.target.y;
	return { xmin: cx - w / 2, ymin: cy - h / 2, xmax: cx + w / 2, ymax: cy + h / 2 };
}

function onPointerUp(evt) {
	// MapControls consumes drag gestures itself; a plain click (no drag) is
	// what should register as a boundary-point placement. MapControls
	// doesn't distinguish for us, so track movement ourselves.
	if (evt.__wasDrag) return;
	const el = containerRef.value;
	const rect = el.getBoundingClientRect();
	const ndc = new THREE.Vector2(
		((evt.clientX - rect.left) / rect.width) * 2 - 1,
		-((evt.clientY - rect.top) / rect.height) * 2 + 1
	);
	const raycaster = new THREE.Raycaster();
	raycaster.setFromCamera(ndc, camera);
	const groundPlane = new THREE.Plane(new THREE.Vector3(0, 0, 1), 0);
	const hit = new THREE.Vector3();
	if (raycaster.ray.intersectPlane(groundPlane, hit)) {
		emit('click', [hit.x, hit.y]);
	}
}

let downX = 0, downY = 0;
function onPointerDown(evt) { downX = evt.clientX; downY = evt.clientY; }
function onPointerUpWithDragCheck(evt) {
	const moved = Math.hypot(evt.clientX - downX, evt.clientY - downY) > 3;
	evt.__wasDrag = moved;
	onPointerUp(evt);
}

watch(() => props.footprints, rebuildGeometry);
watch(() => [props.keptIds, props.crossingIds], () => { applySelectionColors(); render(); }, { deep: true });
watch(() => props.ring, () => { updateRingLine(); render(); }, { deep: true });

function onResize() {
	const el = containerRef.value;
	const w = el.clientWidth, h = el.clientHeight;
	renderer.setSize(w, h);
	const frustumW = camera.right - camera.left;
	const frustumH = frustumW / (w / h);
	camera.top = frustumH / 2;
	camera.bottom = -frustumH / 2;
	camera.updateProjectionMatrix();
	if (ringLine) ringLine.material.resolution.copy(lineResolution());
	render();
}

onMounted(() => {
	const el = containerRef.value;
	scene = new THREE.Scene();
	scene.background = new THREE.Color(0x0b1220);

	const w = el.clientWidth, h = el.clientHeight;
	camera = new THREE.OrthographicCamera(-w / 2, w / 2, h / 2, -h / 2, 0.1, 100000);
	camera.up.set(0, 1, 0);
	camera.position.set(29500, 30500, 1000);
	camera.lookAt(29500, 30500, 0);

	renderer = new THREE.WebGLRenderer({ antialias: true });
	renderer.setSize(w, h);
	renderer.outputColorSpace = THREE.SRGBColorSpace;
	el.appendChild(renderer.domElement);

	// Same fix as ThreeJsViewer.vue's 3D renderer, same reasoning: without
	// calling preventDefault() here, a lost WebGL context never gets
	// automatically restored by the browser -- it just stays black. This
	// view runs its own separate WebGLRenderer/context from the 3D one, so
	// needs its own listener.
	renderer.domElement.addEventListener('webglcontextlost', (event) => {
		event.preventDefault();
		console.warn('[OrthoWebGLView] WebGL context lost -- attempting recovery on restore.');
	}, false);
	renderer.domElement.addEventListener('webglcontextrestored', () => {
		console.warn('[OrthoWebGLView] WebGL context restored -- rebuilding geometry and re-rendering.');
		rebuildGeometry();
		updateRingLine();
		render();
	}, false);

	controls = new MapControls(camera, renderer.domElement);
	controls.target.set(29500, 30500, 0);
	controls.enableRotate = false; // top-down 2D view -- no orbit, pan + zoom only
	controls.screenSpacePanning = true;
	controls.addEventListener('change', render);
	controls.update();

	renderer.domElement.addEventListener('pointerdown', onPointerDown);
	renderer.domElement.addEventListener('pointerup', onPointerUpWithDragCheck);

	rebuildGeometry();
	render();

	window.addEventListener('resize', onResize);
});

onBeforeUnmount(() => {
	window.removeEventListener('resize', onResize);
	controls?.dispose();
	renderer?.dispose();
});

defineExpose({ getViewBounds, fitBounds, flyTo, resetView });
</script>

<template>
	<div ref="containerRef" class="ortho-webgl ortho-canvas"></div>
</template>

<style scoped>
.ortho-webgl {
	width: 100%;
	height: 100%;
}
.ortho-webgl :deep(canvas) {
	display: block;
}
</style>
