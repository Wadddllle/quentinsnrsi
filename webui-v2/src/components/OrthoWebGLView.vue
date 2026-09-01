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
// Phase 4b Path 3 (mesh import placement): client-side parse of an uploaded
// STL/OBJ, purely to derive a 2D preview outline -- the backend does its own
// authoritative parse (via trimesh) once placement is confirmed, this is
// never uploaded, just rendered. Moved here from ThreeJsViewer.vue (the
// original 3D ghost-preview flow) per direct user feedback: there's no real
// use for a Z axis when placing a footprint, and orbiting/panning the 3D
// camera while a ghost is active fought the interaction badly. Placement is
// 2D-only now, matching how the boundary-drawing tool already works.
import { OBJLoader } from 'three/examples/jsm/loaders/OBJLoader.js';
import { STLLoader } from 'three/examples/jsm/loaders/STLLoader.js';
import { mergeGeometries } from 'three/examples/jsm/utils/BufferGeometryUtils.js';

const props = defineProps({
	footprints: { type: Array, default: () => [] },
	ring: { type: Array, default: () => [] },
	keptIds: { type: Array, default: () => [] },
	crossingIds: { type: Array, default: () => [] },
	// Phase 3 (remove-building): ids removed from the working dataset --
	// reuses the exact same delta-recolor mechanism already built for
	// keptIds/crossingIds below, just a third paintable state.
	removedIds: { type: Array, default: () => [] },
	// Phase 4b Path 3: set to { file, note } to start interactive placement
	// (mousemove-follow + scroll-to-rotate + click-to-confirm), null/
	// undefined otherwise -- a prop rather than a method call so the parent
	// can reactively clear it (e.g. Escape/cancel) without needing a ref.
	placeMeshRequest: { type: Object, default: null },
	// Wind/CFD-buffer overlays: the buffered domain envelope, the per-direction
	// wind rectangle, and the flow arrow. Every one of these rings is computed
	// SERVER-SIDE (sbg/onemap_native/ui/wind_plan.py) and handed here as plain
	// world-coordinate points -- this component does no trigonometry. Two
	// implementations of the rotation convention would diverge, and the failure
	// mode is a mis-georeferenced dose map that looks entirely plausible.
	// Each entry: { points: [[x,y],...], color, closed?, width?, z? }
	overlays: { type: Array, default: () => [] },
});

const emit = defineEmits(['click', 'view-changed', 'mesh-placement-confirmed', 'mesh-placement-cancelled']);

const containerRef = ref(null);
let renderer, scene, camera, controls, mesh, colorAttr;
let vertexRanges = {}; // buildingId -> [startVertex, endVertexExclusive]
let hasAutoFitted = false;
let fullExtentBounds = null; // {xmin,ymin,xmax,ymax} of all loaded footprints, for resetView()
let ringLine = null;
let overlayLines = [];   // wind/buffer overlays -- see the `overlays` prop
let boundaryOnly = false; // true while a drag gesture is in progress on controls -- avoids fighting MapControls' own render loop

// Phase 4b Path 3 (mesh import placement) -- see placeMeshRequest prop
// comment above for why this lives here (2D) rather than the 3D view.
let ghostGroup = null; // THREE.Group holding the fill + outline, moved/rotated as a unit
let ghostRawCentroidXY = null; // hull's own XY centroid, before recentering -- needed to compute dx/dy on confirm
let ghostRawMinZ = null; // raw mesh's min Z -- dz places the mesh's own base at world Z=0, matching Path 1's flat base_z=0.0 default
let ghostRotationDeg = 0;
let ghostSourceRequest = null;
let controlsWereEnabled = true;
let placementMoveHandler = null;
let placementWheelHandler = null;
let placementClickHandler = null;
let placementKeyHandler = null;

const DEFAULT_COLOR = [150 / 255, 160 / 255, 180 / 255];
const KEPT_COLOR = [74 / 255, 158 / 255, 255 / 255];
const CROSSING_COLOR = [255 / 255, 99 / 255, 71 / 255];
const REMOVED_COLOR = [60 / 255, 65 / 255, 75 / 255];

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
	const nextHighlighted = new Set([...props.keptIds, ...props.crossingIds, ...props.removedIds]);
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
	// Painted last so it wins over a stale kept/crossing color for the same
	// building -- e.g. a building removed mid-boundary-draw shouldn't keep
	// showing as "kept" or "crossing" once it's gone from the dataset.
	paint(props.removedIds, REMOVED_COLOR);
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

// Shared "fat line" builder -- Line2/LineGeometry/LineMaterial, because plain
// THREE.Line's `linewidth` is silently ignored on most WebGL drivers (a real bug
// this project already hit: overlays rendered as invisible 1px traces).
function makeLine(points, { color = 0xffd23f, width = 3, z = 1, closed = true } = {}) {
	const pts = points.map(([x, y]) => [x, y, z]);
	if (closed && pts.length > 2) pts.push(pts[0]);
	const flat = [];
	for (const p of pts) flat.push(p[0], p[1], p[2]);
	const geometry = new LineGeometry();
	geometry.setPositions(flat);
	const material = new LineMaterial({ color, linewidth: width, resolution: lineResolution() });
	const line = new Line2(geometry, material);
	line.computeLineDistances();
	return line;
}

function disposeLine(line) {
	if (!line) return;
	scene.remove(line);
	line.geometry.dispose();
	line.material.dispose();
}

function updateRingLine() {
	disposeLine(ringLine);
	ringLine = null;
	if (!props.ring || props.ring.length < 2) return;
	ringLine = makeLine(props.ring, { color: 0xffd23f, width: 3, z: 1 });
	scene.add(ringLine);
}

function updateOverlays() {
	for (const l of overlayLines) disposeLine(l);
	overlayLines = [];
	for (const o of props.overlays || []) {
		if (!o || !o.points || o.points.length < 2) continue;
		// z below the ROI ring (1) so the drawn area of interest always stays
		// readable on top of the larger buffered domain around it.
		const line = makeLine(o.points, {
			color: o.color ?? 0x6f8fbf, width: o.width ?? 2,
			z: o.z ?? 0.5, closed: o.closed !== false,
		});
		overlayLines.push(line);
		scene.add(line);
	}
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

function screenToWorld(evt) {
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
	return raycaster.ray.intersectPlane(groundPlane, hit) ? hit : null;
}

function onPointerUp(evt) {
	// Suppress boundary-point placement while a mesh-placement ghost is
	// active -- both this (via pointerup) and _onGhostClick (via a separate
	// 'click' listener) fire for the same physical click on the same DOM
	// element, and confirming a placement shouldn't also add a boundary
	// point (the exact cross-firing bug already found and fixed once for
	// the original 3D ghost flow -- same fix, same reason, new location).
	if (ghostGroup) return;
	// MapControls consumes drag gestures itself; a plain click (no drag) is
	// what should register as a boundary-point placement. MapControls
	// doesn't distinguish for us, so track movement ourselves.
	if (evt.__wasDrag) return;
	const hit = screenToWorld(evt);
	if (hit) emit('click', [hit.x, hit.y]);
}

let downX = 0, downY = 0;
function onPointerDown(evt) { downX = evt.clientX; downY = evt.clientY; }
function onPointerUpWithDragCheck(evt) {
	const moved = Math.hypot(evt.clientX - downX, evt.clientY - downY) > 3;
	evt.__wasDrag = moved;
	onPointerUp(evt);
}

// --- Phase 4b Path 3: mesh-import placement -------------------------------

function convexHull2D(points) {
	// Andrew's monotone chain -- a cheap, dependency-free way to turn a raw
	// mesh's vertices into a top-down outline for the ghost preview. Not an
	// exact silhouette for a concave building, but a real, honest outline
	// (not a bounding box) that's good enough for "does this look about
	// right" placement, matching how the added-buildings 3D overlay is also
	// only ever an approximation (see ThreeJsViewer.vue's own comment).
	const pts = [...new Set(points.map((p) => p.join(',')))].map((s) => s.split(',').map(Number));
	pts.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
	if (pts.length < 3) return pts;
	const cross = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
	const lower = [];
	for (const p of pts) {
		while (lower.length >= 2 && cross(lower[lower.length - 2], lower[lower.length - 1], p) <= 0) lower.pop();
		lower.push(p);
	}
	const upper = [];
	for (let i = pts.length - 1; i >= 0; i--) {
		const p = pts[i];
		while (upper.length >= 2 && cross(upper[upper.length - 2], upper[upper.length - 1], p) <= 0) upper.pop();
		upper.push(p);
	}
	lower.pop();
	upper.pop();
	return lower.concat(upper);
}

async function parseMeshFile(file) {
	const ext = file.name.split('.').pop().toLowerCase();
	if (ext === 'obj') {
		const text = await file.text();
		const group = new OBJLoader().parse(text);
		const geometries = [];
		group.traverse((child) => {
			if (child.isMesh && child.geometry) {
				geometries.push(child.geometry.index ? child.geometry.toNonIndexed() : child.geometry);
			}
		});
		return geometries.length > 1 ? mergeGeometries(geometries, false) : geometries[0];
	} else if (ext === 'stl') {
		const buffer = await file.arrayBuffer();
		return new STLLoader().parse(buffer);
	}
	throw new Error(`Unsupported file type: .${ext}`);
}

function removeGhostMeshes() {
	if (!ghostGroup) return;
	scene.remove(ghostGroup);
	for (const child of ghostGroup.children) {
		child.geometry.dispose();
		child.material.dispose();
	}
	ghostGroup = null;
}

function buildGhostMeshes(hullPoints, worldX, worldY) {
	removeGhostMeshes();
	const shape = new THREE.Shape(hullPoints.map(([x, y]) => new THREE.Vector2(x, y)));
	const fillGeom = new THREE.ShapeGeometry(shape);
	const fillMat = new THREE.MeshBasicMaterial({ color: 0xffa500, transparent: true, opacity: 0.4, side: THREE.DoubleSide, depthWrite: false });
	const fillMesh = new THREE.Mesh(fillGeom, fillMat);
	fillMesh.position.z = 5; // above the footprint mesh (z=0) and the ring line (z=1)

	const loop = [...hullPoints, hullPoints[0]];
	const flat = [];
	for (const [x, y] of loop) flat.push(x, y, 5);
	const lineGeom = new LineGeometry();
	lineGeom.setPositions(flat);
	const lineMat = new LineMaterial({ color: 0xffa500, linewidth: 3, resolution: lineResolution() });
	const line = new Line2(lineGeom, lineMat);
	line.computeLineDistances();

	ghostGroup = new THREE.Group();
	ghostGroup.add(fillMesh);
	ghostGroup.add(line);
	ghostGroup.position.set(worldX, worldY, 0);
	scene.add(ghostGroup);
}

async function startMeshPlacement(request) {
	teardownMeshPlacement(false);

	let geometry;
	try {
		geometry = await parseMeshFile(request.file);
	} catch (err) {
		emit('mesh-placement-cancelled', { error: err.message });
		return;
	}
	if (!geometry || !geometry.attributes.position || geometry.attributes.position.count === 0) {
		emit('mesh-placement-cancelled', { error: 'No usable geometry found in file' });
		return;
	}

	const pos = geometry.attributes.position;
	const xy = [];
	for (let i = 0; i < pos.count; i++) xy.push([pos.getX(i), pos.getY(i)]);
	const hull = convexHull2D(xy);
	if (hull.length < 3) {
		emit('mesh-placement-cancelled', { error: 'Could not compute a 2D outline from this file' });
		return;
	}

	geometry.computeBoundingBox();
	ghostRawMinZ = geometry.boundingBox.min.z;

	let cx = 0, cy = 0;
	for (const [x, y] of hull) { cx += x; cy += y; }
	cx /= hull.length;
	cy /= hull.length;
	ghostRawCentroidXY = { x: cx, y: cy };
	const localHull = hull.map(([x, y]) => [x - cx, y - cy]);

	ghostRotationDeg = 0;
	ghostSourceRequest = request;
	buildGhostMeshes(localHull, controls.target.x, controls.target.y);

	// Placement owns mouse/wheel/click entirely while active -- same
	// reasoning as the original 3D ghost flow: no camera pan/zoom fighting
	// with ghost-follow/scroll-to-rotate/click-to-confirm. Restored on
	// teardown.
	controlsWereEnabled = controls.enabled;
	controls.enabled = false;

	const dom = renderer.domElement;
	placementMoveHandler = onGhostPointerMove;
	placementWheelHandler = onGhostWheel;
	placementClickHandler = onGhostClick;
	placementKeyHandler = onGhostKeydown;
	dom.addEventListener('pointermove', placementMoveHandler);
	dom.addEventListener('wheel', placementWheelHandler, { passive: false });
	dom.addEventListener('click', placementClickHandler);
	window.addEventListener('keydown', placementKeyHandler);

	render();
}

function onGhostPointerMove(evt) {
	if (!ghostGroup) return;
	const hit = screenToWorld(evt);
	if (hit) {
		ghostGroup.position.x = hit.x;
		ghostGroup.position.y = hit.y;
		render();
	}
}

function onGhostWheel(evt) {
	if (!ghostGroup) return;
	evt.preventDefault();
	ghostRotationDeg += evt.deltaY > 0 ? 5 : -5;
	ghostGroup.rotation.z = THREE.MathUtils.degToRad(ghostRotationDeg);
	render();
}

function onGhostClick() {
	if (!ghostGroup || !ghostSourceRequest) return;
	const result = {
		file: ghostSourceRequest.file,
		note: ghostSourceRequest.note,
		dx: ghostGroup.position.x - ghostRawCentroidXY.x,
		dy: ghostGroup.position.y - ghostRawCentroidXY.y,
		dz: -ghostRawMinZ, // places the mesh's own base at world Z=0 -- no elevation lookup, matching Path 1's flat base_z=0.0 default
		rotationDeg: ghostRotationDeg,
	};
	teardownMeshPlacement(true);
	emit('mesh-placement-confirmed', result);
}

function onGhostKeydown(evt) {
	if (evt.key !== 'Escape') return;
	teardownMeshPlacement(false);
	emit('mesh-placement-cancelled', {});
}

function teardownMeshPlacement() {
	const dom = renderer && renderer.domElement;
	if (dom && placementMoveHandler) dom.removeEventListener('pointermove', placementMoveHandler);
	if (dom && placementWheelHandler) dom.removeEventListener('wheel', placementWheelHandler);
	if (dom && placementClickHandler) dom.removeEventListener('click', placementClickHandler);
	if (placementKeyHandler) window.removeEventListener('keydown', placementKeyHandler);
	placementMoveHandler = null;
	placementWheelHandler = null;
	placementClickHandler = null;
	placementKeyHandler = null;

	removeGhostMeshes();
	ghostSourceRequest = null;
	ghostRawCentroidXY = null;
	ghostRawMinZ = null;
	ghostRotationDeg = 0;

	if (controls) controls.enabled = controlsWereEnabled;
	render();
}

watch(() => props.placeMeshRequest, (newVal) => {
	if (newVal) startMeshPlacement(newVal);
	else teardownMeshPlacement(false);
});

watch(() => props.footprints, rebuildGeometry);
watch(() => [props.keptIds, props.crossingIds, props.removedIds], () => { applySelectionColors(); render(); }, { deep: true });
watch(() => props.ring, () => { updateRingLine(); render(); }, { deep: true });
watch(() => props.overlays, () => { updateOverlays(); render(); }, { deep: true });

function onResize() {
	const el = containerRef.value;
	const w = el.clientWidth, h = el.clientHeight;
	renderer.setSize(w, h);
	const frustumW = camera.right - camera.left;
	const frustumH = frustumW / (w / h);
	camera.top = frustumH / 2;
	camera.bottom = -frustumH / 2;
	camera.updateProjectionMatrix();
	// LineMaterial needs the pixel resolution to size a screen-space-width line;
	// miss one of these and that overlay silently renders at the wrong thickness.
	if (ringLine) ringLine.material.resolution.copy(lineResolution());
	for (const l of overlayLines) l.material.resolution.copy(lineResolution());
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
		updateOverlays();
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
	updateOverlays();
	render();

	window.addEventListener('resize', onResize);
});

onBeforeUnmount(() => {
	window.removeEventListener('resize', onResize);
	teardownMeshPlacement(false);
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
