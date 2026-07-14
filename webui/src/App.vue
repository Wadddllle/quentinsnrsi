<script setup>
import { ref, shallowRef, computed } from 'vue';
import SbgViewer3D from './components/three/SbgViewer3D.vue';
import BoundaryDrawTool from './components/plan2d/BoundaryDrawTool.vue';
import LocationSearchBox from './components/LocationSearchBox.vue';
import { FULL_ISLAND_BBOX, getFullIslandCitymodel } from './composables/fullIslandData.js';

// UX redesign (see project plan, Phase 6): single full-viewport 2D/3D toggle
// replacing Phase 1's side-by-side split pane, per direct user feedback --
// "not much point showing both at once... my screen not even that big."
//
// 2D lands showing the WHOLE island (fast: ~0.76s/42MB backend, ~1.5s
// browser load, now GPU-rendered via OrthoWebGLView -- see project plan,
// "2D/3D rendering architecture redesign" -- so pan/zoom stays smooth
// regardless of zoom level, which the earlier Canvas2D version was not).
//
// 3D is lazy-mounted (v-if, not just v-show) on first activation --
// confirmed via direct DOM inspection that mounting it while hidden gives
// its WebGL renderer a 0x0 container size that nothing later corrects (see
// SbgViewer3D.vue's own comment) -- and always loads the whole island, every
// time. An earlier design had a separate bbox-scoped/full-island dual mode
// (deriving an initial bbox from wherever the 2D camera happened to be
// framing); dropped per direct user instruction after real testing showed
// it was unneeded complexity -- "just render the whole island every time,
// we will handle loading screens and other optimisations later."
//
// The fetch itself, though, starts right now (see getFullIslandCitymodel()
// below), well before the user ever clicks "3D" -- per direct follow-up
// request to let it "peacefully load in back" while the user is still
// getting oriented in 2D, rather than only starting on click. The WebGL
// scene itself still only gets built on first activation (that part can't
// move earlier -- see the 0x0-container note above).
//
// Tried making the whole scene-build eager too (always-mounted, hidden via
// CSS visibility instead of v-if), paired with moving fetch+JSON.parse into
// a dedicated Worker -- reverted both after real-browser testing showed it
// made things worse, not better: the Worker's own hand-off back to the main
// thread (a structuredClone of the whole parsed object) turned out to cost
// about as much as the JSON.parse it was meant to avoid, so it added a new
// ~5s freeze on top of the existing pipeline instead of removing one, and
// made the (previously click-triggered, expected) chunk-building freeze
// happen automatically/unprompted while the user was still in 2D. Back to
// the confirmed-good state: eager plain fetch, lazy scene mount on click.

const mode = ref('2d'); // '2d' | '3d'
const has3DBeenActivated = ref(false);

const boundaryToolRef = ref(null);
const viewer3dRef = ref(null);

const footprints = shallowRef([]);
const footprintsById = shallowRef({});
const status2d = ref('loading…');
const status3d = ref('not loaded yet');
const selectedObjid = ref(null);

// Lifted from BoundaryDrawTool (2D-only editing) so the 3D read-only overlay
// can render the same ring/crossing-ids -- see BoundaryDrawTool.vue's own
// comment on why only these three (not drawing/committing/error) are lifted.
const boundaryRing = ref([]);
const crossingIds = ref([]);

async function fetchAllFootprints() {
	status2d.value = 'loading island overview…';
	const { xmin, ymin, xmax, ymax } = FULL_ISLAND_BBOX;
	const res = await fetch(`/api/dataset/footprints?bbox=${xmin},${ymin},${xmax},${ymax}`);
	const data = await res.json();
	footprints.value = data.buildings;
	const byId = {};
	for (const b of data.buildings) byId[b.id] = b;
	footprintsById.value = byId;
	status2d.value = `${data.buildings.length} buildings (2D)`;
}
fetchAllFootprints();
getFullIslandCitymodel().catch(() => {}); // kick off the 3D fetch in the background now; errors surface for real when SbgViewer3D awaits the same promise

const highlightFootprintRings = computed(() => {
	const byId = footprintsById.value;
	const rings = [];
	for (const id of crossingIds.value) {
		const rec = byId[id];
		if (rec) for (const r of rec.rings) rings.push(r);
	}
	return rings;
});

function onRingChanged(newRing) {
	boundaryRing.value = newRing;
}

function onSelectionChanged({ crossingIds: c }) {
	crossingIds.value = c;
}

function activateMode(newMode) {
	if (newMode === '3d' && !has3DBeenActivated.value) {
		has3DBeenActivated.value = true;
		status3d.value = 'loading…';
	}
	mode.value = newMode;
}

function onGoto({ x, y }) {
	boundaryToolRef.value?.flyTo(x, y, 300); // 300m half-width, matches OrthoWebGLView's flyTo default
	if (has3DBeenActivated.value) {
		viewer3dRef.value?.goTo(x, y); // camera-only reposition, the whole island is always loaded
	}
}

function on3dLoaded({ count }) {
	status3d.value = `${count} buildings (3D)`;
}

function onObjectClicked(info) {
	selectedObjid.value = info ? info[0] : null;
}
</script>

<template>
	<div id="app-root">
		<div class="mode-bar">
			<button :class="{ active: mode === '2d' }" @click="activateMode('2d')">2D</button>
			<button :class="{ active: mode === '3d' }" @click="activateMode('3d')">3D</button>
			<LocationSearchBox @goto="onGoto" />
			<span class="status">
				{{ status2d }} / {{ status3d }}<span v-if="selectedObjid"> — selected: {{ selectedObjid }}</span>
			</span>
		</div>
		<div class="stage">
			<div class="pane" v-show="mode === '2d'">
				<BoundaryDrawTool
					ref="boundaryToolRef"
					:footprints="footprints"
					@ring-changed="onRingChanged"
					@selection-changed="onSelectionChanged"
				/>
			</div>
			<div class="pane" v-show="mode === '3d'" v-if="has3DBeenActivated">
				<SbgViewer3D
					ref="viewer3dRef"
					:selected-objid="selectedObjid"
					:boundary-ring="boundaryRing"
					:highlight-footprint-rings="highlightFootprintRings"
					@loaded="on3dLoaded"
					@object_clicked="onObjectClicked"
				/>
			</div>
			<div v-else-if="mode === '3d'" class="pane placeholder">switching to 3D…</div>
		</div>
	</div>
</template>

<style>
html,
body,
#app {
	margin: 0;
	padding: 0;
	height: 100%;
	width: 100%;
	overflow: hidden;
}
#app-root {
	position: relative;
	display: flex;
	flex-direction: column;
	width: 100%;
	height: 100%;
}
.mode-bar {
	display: flex;
	align-items: center;
	gap: 8px;
	padding: 6px 10px;
	background: #1a2030;
	color: #ddd;
	font-family: monospace;
	font-size: 13px;
	z-index: 20;
}
.mode-bar button {
	cursor: pointer;
	padding: 3px 14px;
	background: #262e42;
	color: #ddd;
	border: 1px solid #3a4358;
	border-radius: 4px;
}
.mode-bar button.active {
	background: #4a9eff;
	color: #0b1220;
	border-color: #4a9eff;
	font-weight: bold;
}
.mode-bar .status {
	margin-left: auto;
}
.stage {
	position: relative;
	flex: 1;
	min-height: 0;
}
.pane {
	position: absolute;
	inset: 0;
}
.pane.placeholder {
	display: flex;
	align-items: center;
	justify-content: center;
	background: #0b1220;
	color: #7d8aa0;
	font-family: monospace;
}
</style>
