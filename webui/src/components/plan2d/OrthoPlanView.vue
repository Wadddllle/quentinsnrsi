<script setup>
// Plain HTML5 Canvas, not a Three.js orthographic reuse and not SVG (see
// project plan, Phase 6): recoloring tens of thousands of footprints on
// every drag-frame is a different workload from the 3D view's static-ish
// selection highlighting, and one DOM node per footprint (SVG) doesn't
// scale at this count.
//
// Owns its own map-like camera (center + metersPerPixel) and pan/zoom
// gestures -- Phase 1's version had none of this (a fixed halfWidth prop,
// panning only by dragging the *3D* camera). Real UX redesign motivation:
// the user needs to navigate a map the way they would in Google Maps/QGIS,
// starting from a full-island view, not be dropped into an arbitrary small
// bbox with no way to orient themselves (see project plan, "UX redesign").
//
// Viewport culling: `footprints` can be up to 118,780 entries (the full
// island, loaded once by the parent -- see App.vue). Per-building bounding
// boxes are precomputed once whenever the `footprints` array reference
// changes (not deep-watched -- the parent passes a shallowRef's value, same
// non-reactive-large-blob pattern citymodel already uses in
// SbgViewer3D.vue), then every draw() only draws footprints whose bbox
// intersects the current view. Measured: a full unfiltered redraw of all
// 118,780 footprints already only takes ~360ms in Chromium (see project
// plan) -- this culling pass is cheaper still (bbox-only comparisons, no
// path construction) and mainly exists to keep zoomed-in redraws fast
// during continuous drag, not because the unfiltered case was slow.
import { ref, shallowRef, watch, onMounted, onBeforeUnmount } from 'vue';

const props = defineProps({
	footprints: { type: Array, default: () => [] }, // [{id, rings: [[[x,y],...]], height, height_source}, ...]
	ring: { type: Array, default: () => [] }, // in-progress or committed boundary, world [x, y] points
	keptIds: { type: Array, default: () => [] },
	crossingIds: { type: Array, default: () => [] },
});

const emit = defineEmits(['click', 'view-changed']);

const canvasRef = ref(null);

// Camera state -- internal, not a prop, so pan/zoom gestures don't need to
// round-trip through the parent on every frame. Parent reads the current
// view via getViewBounds() (e.g. when deriving the 3D load bbox on mode
// toggle) or drives it via flyTo()/fitBounds().
const center = ref({ x: 29500, y: 30500 }); // overwritten by the first-footprints auto-fit below
const metersPerPixel = ref(50);
let hasAutoFitted = false;

// Precomputed per-building bbox, indexed parallel to props.footprints --
// rebuilt only when the footprints array reference changes, not per frame.
let footprintBboxes = [];
function rebuildBboxCache() {
	footprintBboxes = props.footprints.map((b) => {
		let xmin = Infinity, xmax = -Infinity, ymin = Infinity, ymax = -Infinity;
		for (const ring of b.rings) {
			for (const [x, y] of ring) {
				if (x < xmin) xmin = x;
				if (x > xmax) xmax = x;
				if (y < ymin) ymin = y;
				if (y > ymax) ymax = y;
			}
		}
		return [xmin, ymin, xmax, ymax];
	});
}

function currentViewBounds(w, h) {
	const halfW = (w / 2) * metersPerPixel.value;
	const halfH = (h / 2) * metersPerPixel.value;
	return {
		xmin: center.value.x - halfW,
		ymin: center.value.y - halfH,
		xmax: center.value.x + halfW,
		ymax: center.value.y + halfH,
	};
}

function worldToScreen(x, y, w, h) {
	const mpp = metersPerPixel.value;
	return [w / 2 + (x - center.value.x) / mpp, h / 2 - (y - center.value.y) / mpp];
}

function screenToWorld(sx, sy, w, h) {
	const mpp = metersPerPixel.value;
	return [center.value.x + (sx - w / 2) * mpp, center.value.y - (sy - h / 2) * mpp];
}

// Buildings whose on-screen bbox is smaller than this (in pixels) get a
// single cheap fillRect from the precomputed bbox instead of a full
// moveTo/lineTo ring path + fill + stroke. At island-wide zoom, the large
// majority of the 118,780 buildings are a handful of pixels or less --
// tracing their real ring geometry is real work (beginPath/many
// lineTo/closePath/fill/stroke per ring) spent on detail that isn't even
// visible. This is the fix for "why is it rendering at max res when zoomed
// out" -- it wasn't a deliberate choice, just missing LOD.
const LOD_PIXEL_THRESHOLD = 3;

function draw() {
	const canvas = canvasRef.value;
	if (!canvas) return;
	const w = canvas.clientWidth, h = canvas.clientHeight;
	if (w === 0 || h === 0) return;
	if (canvas.width !== w) canvas.width = w;
	if (canvas.height !== h) canvas.height = h;
	const ctx = canvas.getContext('2d');

	ctx.fillStyle = '#0b1220';
	ctx.fillRect(0, 0, w, h);

	// Cull to the visible extent plus a small margin (avoids pop-in right at
	// the edge during a drag).
	const vb = currentViewBounds(w, h);
	const marginX = (vb.xmax - vb.xmin) * 0.1;
	const marginY = (vb.ymax - vb.ymin) * 0.1;
	const cxmin = vb.xmin - marginX, cxmax = vb.xmax + marginX;
	const cymin = vb.ymin - marginY, cymax = vb.ymax + marginY;

	const keptSet = new Set(props.keptIds);
	const crossingSet = new Set(props.crossingIds);
	const hasSelection = keptSet.size > 0 || crossingSet.size > 0;
	const mpp = metersPerPixel.value;

	const footprints = props.footprints;
	for (let i = 0; i < footprints.length; i++) {
		const bbox = footprintBboxes[i];
		if (!bbox || bbox[2] < cxmin || bbox[0] > cxmax || bbox[3] < cymin || bbox[1] > cymax) continue;

		const b = footprints[i];
		if (keptSet.has(b.id)) {
			ctx.fillStyle = 'rgba(74, 158, 255, 0.55)';
			ctx.strokeStyle = '#4a9eff';
		} else if (crossingSet.has(b.id)) {
			ctx.fillStyle = 'rgba(255, 99, 71, 0.65)';
			ctx.strokeStyle = '#ff6347';
		} else if (hasSelection) {
			ctx.fillStyle = 'rgba(100, 108, 125, 0.25)';
			ctx.strokeStyle = '#4a5266';
		} else {
			ctx.fillStyle = 'rgba(150, 160, 180, 0.35)';
			ctx.strokeStyle = '#7d8aa0';
		}

		const screenW = (bbox[2] - bbox[0]) / mpp, screenH = (bbox[3] - bbox[1]) / mpp;
		if (screenW < LOD_PIXEL_THRESHOLD && screenH < LOD_PIXEL_THRESHOLD) {
			const [sx0, sy0] = worldToScreen(bbox[0], bbox[3], w, h); // note: screen y is flipped, top-left uses ymax
			ctx.fillRect(sx0, sy0, Math.max(screenW, 1), Math.max(screenH, 1));
			continue;
		}

		for (const ringPts of b.rings) {
			ctx.beginPath();
			ringPts.forEach(([x, y], j) => {
				const [sx, sy] = worldToScreen(x, y, w, h);
				if (j === 0) ctx.moveTo(sx, sy); else ctx.lineTo(sx, sy);
			});
			ctx.closePath();
			ctx.lineWidth = 1;
			ctx.fill();
			ctx.stroke();
		}
	}

	if (props.ring.length > 0) {
		ctx.beginPath();
		props.ring.forEach(([x, y], i) => {
			const [sx, sy] = worldToScreen(x, y, w, h);
			if (i === 0) ctx.moveTo(sx, sy); else ctx.lineTo(sx, sy);
		});
		if (props.ring.length > 2) ctx.closePath();
		ctx.strokeStyle = '#ffd23f';
		ctx.lineWidth = 2;
		ctx.stroke();
		for (const [x, y] of props.ring) {
			const [sx, sy] = worldToScreen(x, y, w, h);
			ctx.beginPath();
			ctx.arc(sx, sy, 3, 0, Math.PI * 2);
			ctx.fillStyle = '#ffd23f';
			ctx.fill();
		}
	}
}

// --- rAF-throttled real redraws ---
// The old code called draw() synchronously on every single pointermove/wheel
// event -- at island-wide zoom that's a full-detail recompute (even with the
// LOD fix above, still real per-frame work) potentially dozens of times a
// second. Never queue more than one real draw per animation frame.
let drawScheduled = false;
function scheduleDraw() {
	if (drawScheduled) return;
	drawScheduled = true;
	requestAnimationFrame(() => {
		drawScheduled = false;
		draw();
	});
}

// --- pan (drag) + zoom (wheel), and click-vs-drag disambiguation ---
//
// Dragging specifically gets a cheaper fast path on top of rAF-throttling
// and the LOD fix: a snapshot of the last real render is taken once at drag
// start, and every pointermove during the drag just blits that snapshot at
// a translated offset (a single drawImage call, GPU-accelerated, cost
// independent of building count) instead of recomputing any geometry at
// all. The real, fully-recomputed draw() only runs once when the drag ends
// -- exactly how Google Maps/Figma-style pan feels smooth regardless of
// scene complexity: show the same pixels shifted during the gesture, refine
// after it settles.
let dragging = false;
let dragMoved = false;
let lastX = 0, lastY = 0;
let dragSnapshot = null;
let dragTotalDx = 0, dragTotalDy = 0;

function onPointerDown(evt) {
	dragging = true;
	dragMoved = false;
	lastX = evt.clientX;
	lastY = evt.clientY;
	dragTotalDx = 0;
	dragTotalDy = 0;
	dragSnapshot = null;
}

function blitDragSnapshot() {
	const canvas = canvasRef.value;
	if (!canvas || !dragSnapshot) return;
	const ctx = canvas.getContext('2d');
	const w = canvas.clientWidth, h = canvas.clientHeight;
	ctx.fillStyle = '#0b1220';
	ctx.fillRect(0, 0, w, h);
	ctx.drawImage(dragSnapshot, dragTotalDx, dragTotalDy);
}

function onPointerMove(evt) {
	if (!dragging) return;
	const dx = evt.clientX - lastX;
	const dy = evt.clientY - lastY;
	if (Math.abs(dx) > 3 || Math.abs(dy) > 3) dragMoved = true;
	if (!dragMoved) return;

	if (!dragSnapshot) {
		// First move of this drag -- snapshot whatever's currently rendered
		// (the last real draw()) before we start translating it.
		const canvas = canvasRef.value;
		dragSnapshot = document.createElement('canvas');
		dragSnapshot.width = canvas.width;
		dragSnapshot.height = canvas.height;
		dragSnapshot.getContext('2d').drawImage(canvas, 0, 0);
	}

	const mpp = metersPerPixel.value;
	center.value = { x: center.value.x - dx * mpp, y: center.value.y + dy * mpp };
	dragTotalDx += dx;
	dragTotalDy += dy;
	lastX = evt.clientX;
	lastY = evt.clientY;

	requestAnimationFrame(blitDragSnapshot); // cheap -- fine to queue every move, no throttle-drop needed
	emit('view-changed', getViewBounds());
}

function onPointerUp(evt) {
	if (dragging && !dragMoved) {
		const canvas = canvasRef.value;
		const rect = canvas.getBoundingClientRect();
		const [x, y] = screenToWorld(evt.clientX - rect.left, evt.clientY - rect.top, canvas.clientWidth, canvas.clientHeight);
		emit('click', [x, y]);
	}
	if (dragMoved) {
		dragSnapshot = null;
		draw(); // final, fully-recomputed frame now that the gesture has settled
	}
	dragging = false;
}

function onWheel(evt) {
	evt.preventDefault();
	const canvas = canvasRef.value;
	const rect = canvas.getBoundingClientRect();
	const w = canvas.clientWidth, h = canvas.clientHeight;
	const sx = evt.clientX - rect.left, sy = evt.clientY - rect.top;
	const [worldXBefore, worldYBefore] = screenToWorld(sx, sy, w, h);

	const zoomFactor = Math.exp(evt.deltaY * 0.001); // smooth, direction-natural (scroll down = zoom out)
	metersPerPixel.value = Math.min(Math.max(metersPerPixel.value * zoomFactor, 0.05), 500);

	// Keep the point under the cursor fixed (standard map zoom-to-cursor).
	const [worldXAfter, worldYAfter] = screenToWorld(sx, sy, w, h);
	center.value = {
		x: center.value.x + (worldXBefore - worldXAfter),
		y: center.value.y + (worldYBefore - worldYAfter),
	};
	scheduleDraw();
	emit('view-changed', getViewBounds());
}

function getViewBounds() {
	const canvas = canvasRef.value;
	const w = canvas ? canvas.clientWidth : 0;
	const h = canvas ? canvas.clientHeight : 0;
	return currentViewBounds(w, h);
}

function fitBounds(xmin, ymin, xmax, ymax, paddingFraction = 0.08) {
	const canvas = canvasRef.value;
	if (!canvas) return;
	const w = canvas.clientWidth, h = canvas.clientHeight;
	if (w === 0 || h === 0) return;
	const spanX = (xmax - xmin) * (1 + paddingFraction * 2) || 1;
	const spanY = (ymax - ymin) * (1 + paddingFraction * 2) || 1;
	metersPerPixel.value = Math.max(spanX / w, spanY / h);
	center.value = { x: (xmin + xmax) / 2, y: (ymin + ymax) / 2 };
	draw();
	emit('view-changed', getViewBounds());
}

function flyTo(x, y, zoomMetersPerPixel = 2) {
	center.value = { x, y };
	metersPerPixel.value = zoomMetersPerPixel;
	draw();
	emit('view-changed', getViewBounds());
}

watch(() => props.footprints, () => {
	rebuildBboxCache();
	if (!hasAutoFitted && props.footprints.length > 0) {
		hasAutoFitted = true;
		let xmin = Infinity, xmax = -Infinity, ymin = Infinity, ymax = -Infinity;
		for (const bbox of footprintBboxes) {
			if (bbox[0] < xmin) xmin = bbox[0];
			if (bbox[2] > xmax) xmax = bbox[2];
			if (bbox[1] < ymin) ymin = bbox[1];
			if (bbox[3] > ymax) ymax = bbox[3];
		}
		fitBounds(xmin, ymin, xmax, ymax, 0.02);
	} else {
		draw();
	}
});
watch(() => [props.ring, props.keptIds, props.crossingIds], draw, { deep: true });

onMounted(() => {
	draw();
	window.addEventListener('resize', draw);
});
onBeforeUnmount(() => {
	window.removeEventListener('resize', draw);
});

defineExpose({ redraw: draw, getViewBounds, fitBounds, flyTo });
</script>

<template>
	<canvas
		ref="canvasRef"
		class="ortho-canvas"
		@pointerdown="onPointerDown"
		@pointermove="onPointerMove"
		@pointerup="onPointerUp"
		@pointerleave="onPointerUp"
		@wheel="onWheel"
	></canvas>
</template>

<style scoped>
.ortho-canvas {
	width: 100%;
	height: 100%;
	display: block;
	cursor: crosshair;
	touch-action: none;
}
</style>
