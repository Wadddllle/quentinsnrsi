<script setup>
// Owns all boundary-drawing interaction state (click-to-add-point,
// finish/undo/clear) and talks to /api/cutout/preview + /api/cutout/commit.
// OrthoWebGLView is the pure-rendering counterpart this drives (a WebGL/
// Three.js renderer, replacing the earlier Canvas2D OrthoPlanView.vue after
// it proved too CPU-bound to stay smooth zoomed out in real use -- see
// project plan, "2D/3D rendering architecture redesign"), and owns its own
// pan/zoom camera -- this component just forwards its own view methods.
//
// `ring`/`crossingIds`/`stats` (but deliberately not the rest of this
// component's edit-session-only state like `drawing`/`committing`/`error`)
// are emitted upward on every change so App.vue can mirror a read-only copy
// for the 3D mode's boundary-crossing highlight overlay -- boundary editing
// stays 2D-only per the UX redesign, but 3D still needs to see the same
// ring/crossing-ids to render the warning highlight.
import { onUnmounted, ref } from 'vue';
import OrthoWebGLView from './OrthoWebGLView.vue';
import CrossingBuildingsWarning from './CrossingBuildingsWarning.vue';
import JobProgressPanel from '../jobs/JobProgressPanel.vue';

const props = defineProps({
	footprints: { type: Array, default: () => [] },
});

const emit = defineEmits(['committed', 'ring-changed', 'selection-changed', 'view-changed']);

const viewRef = ref(null);

const ring = ref([]);
const drawing = ref(false);
const keptIds = ref([]);
const crossingIds = ref([]);
const stats = ref(null);
const committing = ref(false);
const commitResult = ref(null);
const error = ref(null);

let previewTimer = null;

function startDrawing() {
	ring.value = [];
	drawing.value = true;
	keptIds.value = [];
	crossingIds.value = [];
	stats.value = null;
	commitResult.value = null;
	error.value = null;
	emit('ring-changed', ring.value);
	emit('selection-changed', { keptIds: keptIds.value, crossingIds: crossingIds.value, stats: stats.value });
}

function undoPoint() {
	ring.value = ring.value.slice(0, -1);
	emit('ring-changed', ring.value);
	schedulePreview();
}

function clearAll() {
	ring.value = [];
	drawing.value = false;
	keptIds.value = [];
	crossingIds.value = [];
	stats.value = null;
	commitResult.value = null;
	error.value = null;
	emit('ring-changed', ring.value);
	emit('selection-changed', { keptIds: keptIds.value, crossingIds: crossingIds.value, stats: stats.value });
}

function onCanvasClick([x, y]) {
	if (!drawing.value) return;
	ring.value = [...ring.value, [x, y]];
	emit('ring-changed', ring.value);
	schedulePreview();
}

// Manual coordinate entry -- clicking on the map is imprecise, and a CFD
// domain boundary is exactly the kind of thing that shouldn't be "eyeballed"
// (same reasoning as LocationSearchBox's coordinate-entry fallback: a
// scientist needs to be able to state precisely, not approximately, what
// domain they used). Reuses the exact same ring-update path as
// onCanvasClick, just fed typed numbers instead of a raycast hit.
const manualX = ref(null);
const manualY = ref(null);

function addManualPoint() {
	if (manualX.value === null || manualY.value === null || Number.isNaN(manualX.value) || Number.isNaN(manualY.value)) return;
	// NOT startDrawing() -- that resets ring to []. Resuming an already-
	// finished ring (e.g. clicked Finish, then wants to add one more point
	// by coordinate) must not silently wipe it.
	drawing.value = true;
	commitResult.value = null;
	ring.value = [...ring.value, [manualX.value, manualY.value]];
	emit('ring-changed', ring.value);
	schedulePreview();
	manualX.value = null;
	manualY.value = null;
}

// Quick rectangle-by-bbox entry -- CFD domains are very often plain
// rectangles, so typing xmin/ymin/xmax/ymax is a lot faster (and more
// precise) than clicking 4 corners individually.
const bboxXmin = ref(null);
const bboxYmin = ref(null);
const bboxXmax = ref(null);
const bboxYmax = ref(null);

function setRectangle() {
	const vals = [bboxXmin.value, bboxYmin.value, bboxXmax.value, bboxYmax.value];
	if (vals.some((v) => v === null || Number.isNaN(v))) return;
	const [xmin, ymin, xmax, ymax] = vals;
	if (xmin >= xmax || ymin >= ymax) {
		error.value = 'bbox min must be less than max';
		return;
	}
	ring.value = [[xmin, ymin], [xmax, ymin], [xmax, ymax], [xmin, ymax]];
	drawing.value = false;
	commitResult.value = null;
	error.value = null;
	emit('ring-changed', ring.value);
	schedulePreview();
}

function finishDrawing() {
	drawing.value = false;
	schedulePreview();
}

function schedulePreview() {
	if (ring.value.length < 3) {
		keptIds.value = [];
		crossingIds.value = [];
		stats.value = null;
		emit('selection-changed', { keptIds: keptIds.value, crossingIds: crossingIds.value, stats: stats.value });
		return;
	}
	if (previewTimer) clearTimeout(previewTimer);
	previewTimer = setTimeout(runPreview, 200);
}

async function runPreview() {
	try {
		const res = await fetch('/api/cutout/preview', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ ring: ring.value }),
		});
		if (!res.ok) {
			const e = await res.json();
			throw new Error(e.detail || res.statusText);
		}
		const data = await res.json();
		keptIds.value = data.kept_ids;
		crossingIds.value = data.crossing_ids;
		stats.value = data.stats;
		error.value = null;
		emit('selection-changed', { keptIds: keptIds.value, crossingIds: crossingIds.value, stats: stats.value });
	} catch (err) {
		error.value = err.message;
	}
}

async function commit() {
	committing.value = true;
	error.value = null;
	try {
		const res = await fetch('/api/cutout/commit', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ ring: ring.value }),
		});
		if (!res.ok) {
			const e = await res.json();
			throw new Error(e.detail || res.statusText);
		}
		const data = await res.json();
		commitResult.value = data;
		emit('committed', data);
	} catch (err) {
		error.value = err.message;
	} finally {
		committing.value = false;
	}
}

function onViewChanged(bounds) {
	emit('view-changed', bounds);
}

// STL generation is a deliberately separate, explicit action from
// commit() above -- commit stays fast/JSON-only (real cutout use is mostly
// as a record of exactly what was cut, not something scientists directly
// consume), while a domain this size can take minutes end to end (mostly
// the Blender stage) per the Phase 5 benchmark, so it shouldn't fire
// automatically on every commit while someone's still iterating on a shape.
const stlJob = ref(null);
let stlPollTimer = null;

async function generateStl() {
	stlJob.value = null;
	error.value = null;
	try {
		const res = await fetch('/api/pipeline/run', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ ring: ring.value }),
		});
		if (!res.ok) {
			const e = await res.json();
			throw new Error(e.detail || res.statusText);
		}
		const { job_id } = await res.json();
		stlJob.value = { id: job_id, status: 'pending' };
		pollStlJob(job_id);
	} catch (err) {
		error.value = err.message;
	}
}

function pollStlJob(jobId) {
	if (stlPollTimer) clearInterval(stlPollTimer);
	stlPollTimer = setInterval(async () => {
		try {
			const res = await fetch(`/api/pipeline/jobs/${jobId}`);
			if (!res.ok) return;
			const data = await res.json();
			stlJob.value = data;
			if (data.status === 'done' || data.status === 'error') {
				clearInterval(stlPollTimer);
				stlPollTimer = null;
			}
		} catch {
			// transient fetch failure -- next tick will retry, no need to surface
		}
	}, 2000);
}

onUnmounted(() => {
	if (stlPollTimer) clearInterval(stlPollTimer);
});

defineExpose({
	getViewBounds: () => viewRef.value?.getViewBounds(),
	fitBounds: (...args) => viewRef.value?.fitBounds(...args),
	flyTo: (...args) => viewRef.value?.flyTo(...args),
	resetView: () => viewRef.value?.resetView(),
});
</script>

<template>
	<div class="boundary-draw-tool">
		<div class="toolbar">
			<button v-if="!drawing" @click="startDrawing">Draw boundary</button>
			<template v-else>
				<button @click="finishDrawing">Finish</button>
				<button @click="undoPoint" :disabled="ring.length === 0">Undo point</button>
			</template>
			<button v-if="ring.length" @click="clearAll">Clear</button>
			<button v-if="ring.length >= 3 && !drawing" :disabled="committing" @click="commit">
				{{ committing ? 'Committing…' : 'Commit cutout' }}
			</button>
			<button
				v-if="ring.length >= 3 && !drawing"
				:disabled="stlJob && stlJob.status !== 'done' && stlJob.status !== 'error'"
				@click="generateStl"
			>
				Generate STL
			</button>
			<span v-if="stats" class="stats">
				kept {{ stats.kept }} / crossing {{ stats.crossing }} / area {{ (stats.area_m2 / 1e6).toFixed(3) }} km²
			</span>
			<span v-if="error" class="error">{{ error }}</span>
			<span v-if="commitResult" class="success">saved {{ commitResult.path }} (kept {{ commitResult.stats.kept }})</span>
		</div>
		<div class="toolbar coords-toolbar">
			<span class="label">Add point (EPSG:3414):</span>
			<input type="number" v-model.number="manualX" placeholder="x" step="any" @keyup.enter="addManualPoint" />
			<input type="number" v-model.number="manualY" placeholder="y" step="any" @keyup.enter="addManualPoint" />
			<button @click="addManualPoint">Add</button>
			<span class="label">Rectangle:</span>
			<input type="number" v-model.number="bboxXmin" placeholder="xmin" step="any" @keyup.enter="setRectangle" />
			<input type="number" v-model.number="bboxYmin" placeholder="ymin" step="any" @keyup.enter="setRectangle" />
			<input type="number" v-model.number="bboxXmax" placeholder="xmax" step="any" @keyup.enter="setRectangle" />
			<input type="number" v-model.number="bboxYmax" placeholder="ymax" step="any" @keyup.enter="setRectangle" />
			<button @click="setRectangle">Set rectangle</button>
		</div>
		<div class="canvas-wrap">
			<OrthoWebGLView
				ref="viewRef"
				:footprints="footprints"
				:ring="ring"
				:kept-ids="keptIds"
				:crossing-ids="crossingIds"
				@click="onCanvasClick"
				@view-changed="onViewChanged"
			/>
			<div class="warning-overlay">
				<CrossingBuildingsWarning :crossing-ids="crossingIds" />
				<JobProgressPanel v-if="stlJob" :job="stlJob" />
			</div>
		</div>
	</div>
</template>

<style scoped>
.boundary-draw-tool {
	display: flex;
	flex-direction: column;
	width: 100%;
	height: 100%;
}
.toolbar {
	display: flex;
	gap: 8px;
	align-items: center;
	padding: 6px 8px;
	background: #1a2030;
	color: #ddd;
	font-family: monospace;
	font-size: 12px;
	flex-wrap: wrap;
}
.toolbar button {
	cursor: pointer;
}
.coords-toolbar {
	background: #141926;
	border-top: 1px solid #262e42;
}
.coords-toolbar .label {
	color: #8a97b0;
}
.coords-toolbar input {
	width: 90px;
	background: #0b1220;
	color: #ddd;
	border: 1px solid #3a4358;
	border-radius: 3px;
	padding: 2px 5px;
	font-family: monospace;
	font-size: 12px;
}
.stats {
	color: #9fd4ff;
}
.error {
	color: #ff6b6b;
}
.success {
	color: #7cfc9a;
}
.canvas-wrap {
	position: relative;
	flex: 1;
	min-height: 0;
}
.warning-overlay {
	position: absolute;
	top: 8px;
	right: 8px;
	z-index: 10;
	display: flex;
	flex-direction: column;
	gap: 8px;
	align-items: flex-end;
}
</style>
