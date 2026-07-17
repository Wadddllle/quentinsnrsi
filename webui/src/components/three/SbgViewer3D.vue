<script setup>
import { ref, shallowRef, computed, onMounted } from 'vue';
import * as THREE from 'three';
import ThreeJsViewer from './ThreeJsViewer.vue';
import { getFullIslandCitymodel } from '../../composables/fullIslandData.js';

// Always loads the whole island, every time -- no scoped/bbox mode, no
// separate "load everything" button. Simplified deliberately per direct
// user instruction after the scoped/full dual-mode design proved to be
// unnecessary complexity in practice: "just render the whole island every
// time. we will handle loading screens and other optimisations later."
//
// IMPORTANT: this component is meant to be mounted lazily (v-if gated on
// first 3D activation, not always-on with v-show alone) -- confirmed via
// direct DOM inspection that mounting it while hidden (display:none) gives
// ThreeJsViewer's initScene() a 0x0 container size at the moment it sizes
// the renderer/camera, which WebGL reports as "Framebuffer is incomplete:
// Attachment has zero size" and nothing later corrects, since nothing ties
// a resize/reinit to a visibility change. See App.vue for the v-if gating
// that avoids this. (Tried always-mounted-but-CSS-hidden instead, paired
// with a Worker-based fetch, to start the scene build eagerly -- reverted,
// see App.vue's comment on why.)
//
// IMPORTANT #2: controls.target.x/y is NOT true EPSG:3414 world
// coordinates, despite camera.up=(0,0,1) suggesting no coordinate games are
// being played. cityjson-threejs-loader's CityJSONLoader.load() computes a
// matrix from the CityJSON's real transform (scale+translate), then calls
// `this.matrix.setPosition(0,0,0)` -- explicitly zeroing the translation --
// before handing that same matrix to both the mesh geometry and the
// camera-fit bounding box. Mesh and camera are shifted by the SAME
// -translate offset, so rendering still looks correct, but anything
// constructing a fit box in true world coordinates must first convert by
// subtracting transform.translate -- see localFrameOffset below.
const props = defineProps({
	boundaryRing: { type: Array, default: () => [] },
	highlightFootprintRings: { type: Array, default: () => [] },
	// Forwarded straight through to ThreeJsViewer's own selectedObjid prop,
	// which already drives its orange (0xFFC107) whole-object highlight
	// shader path -- that mechanism already existed and already matched
	// ninja, it just was never fed a value from here. App.vue tracks the
	// clicked id (from the @object_clicked event) but was only using it for
	// the status-bar text, not passing it back down.
	selectedObjid: { type: String, default: null },
});

const emit = defineEmits(['object_clicked', 'loaded', 'error', 'building_removed']);

const viewerRef = ref(null);
const citymodel = shallowRef({});
const fetching = ref(false);
const parsing = ref(false);
const localFrameOffset = ref({ x: 0, y: 0 }); // add to a local-frame coord to get true world coords; = transform.translate
const chunkProgress = ref(0); // count of 'chunkLoaded' events, for the loading-badge readout
const chunkEstimateTotal = ref(0);

async function loadFullIsland() {
	fetching.value = true;
	chunkProgress.value = 0;
	try {
		// getFullIslandCitymodel() is memoized -- if App.vue already kicked
		// this off in the background on app load (it does), this just awaits
		// that same in-flight/already-resolved fetch instead of starting a
		// second one, so the "click 3D and wait" cost has usually already
		// been paid (partly or fully) by the time the user gets here.
		const data = await getFullIslandCitymodel();
		if (data.transform) {
			localFrameOffset.value = { x: data.transform.translate[0], y: data.transform.translate[1] };
		}
		const objectCount = Object.keys(data.CityObjects || {}).length;
		chunkEstimateTotal.value = Math.ceil(objectCount / 2000); // ThreeJsViewer's fixed chunkSize
		citymodel.value = data;
		emit('loaded', { count: objectCount });
	} catch (err) {
		emit('error', err);
	} finally {
		fetching.value = false;
	}
}

function onRendering(isRendering) {
	parsing.value = isRendering;
}

function onChunkLoaded() {
	chunkProgress.value++;
}

// Camera-only reposition -- the whole island is always loaded, so "go to X"
// just means "point the camera at X", never a fetch. Reuses ThreeJsViewer's
// own fitCameraToSelection (already proven, see Phase 1's lighting-bug
// writeup) with a small local box instead of writing new camera-placement
// math.
//
// halfWidth/halfHeight separate (not one shared radius) so a caller can pass
// an exact, possibly non-square rectangle -- needed for snapTo2dView in
// App.vue, which wants the 3D frame to match 2D's actual current viewport
// aspect, not force it square.
//
// The box's Z range used to be a fixed [0, 100] regardless of the requested
// footprint size -- harmless for the original 300m-radius search-navigation
// case, but a real bug for a tight zoomed-in snap: fitCameraToSelection's
// distance formula takes max(size.x, size.y, size.z), so for e.g. a 60m-wide
// request that arbitrary 100 would dominate and zoom out further than
// asked, silently distorting the fit. Using a near-zero Z extent instead
// means the fit is driven purely by the requested footprint, matching what
// the caller actually asked for.
//
// Passes the viewer's own loadedBoundingBox as fitCameraToSelection's
// maxDistanceBox -- otherwise the zoom-OUT cap would shrink to ~10x
// whatever small area goTo() just framed, permanently losing the ability
// to zoom back out to island scale until a manual Reset View. Real user
// report: "snap to 2D view... cant zoom out th see the whole island
// anymore" -- goTo() is exactly the path both search navigation and
// snapTo2dView go through.
function goTo(worldX, worldY, halfWidth = 300, halfHeight = halfWidth, options = {}) {
	const viewer = viewerRef.value;
	if (!viewer || !viewer.camera) return;
	const localX = worldX - localFrameOffset.value.x;
	const localY = worldY - localFrameOffset.value.y;
	const box = new THREE.Box3(
		new THREE.Vector3(localX - halfWidth, localY - halfHeight, 0),
		new THREE.Vector3(localX + halfWidth, localY + halfHeight, 0)
	);
	viewer.fitCameraToSelection(
		viewer.camera, viewer.controls, box,
		options.fitOffset ?? 1.2, options.topDown ?? false,
		viewer.loadedBoundingBox
	);
}

onMounted(loadFullIsland);

function onObjectClicked(info) {
	emit('object_clicked', info);
}

function resetView() {
	viewerRef.value?.resetView();
}

// The click handler already resolves a real objid via
// resolveIntersectionInfo() (see ThreeJsViewer's handleClick) -- the
// building's full attributes are just citymodel.CityObjects[id].attributes,
// already sitting in memory (this is the same full-island fetch that
// already backs the 3D geometry itself), so no new endpoint/request is
// needed to show them. Previously that objid only ever reached the status
// bar as a bare id string, which is useless without the source JSON open
// next to it -- direct user complaint this addresses.
const selectedAttributes = computed(() => {
	if (!props.selectedObjid) return null;
	return citymodel.value.CityObjects?.[props.selectedObjid]?.attributes ?? null;
});

// Phase 3 (remove-building): the info panel already resolves a real,
// existing CityObject id (see selectedAttributes above) -- Remove reuses
// that directly, no new click/raycast machinery needed. The route uses
// FastAPI's {building_id:path} converter (see sbg/ui/routers/buildings.py)
// specifically because ids like "relation/6730642" contain a literal "/" --
// building the URL with the raw id (not encodeURIComponent, which would
// turn "/" into "%2F") is what actually reaches that route; this was
// confirmed directly against the real backend during development.
const removing = ref(false);
const removeError = ref(null);

async function removeSelected() {
	if (!props.selectedObjid || removing.value) return;
	removing.value = true;
	removeError.value = null;
	try {
		const res = await fetch(`/api/buildings/${props.selectedObjid}/remove`, { method: 'POST' });
		const data = await res.json();
		if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
		emit('building_removed', { id: props.selectedObjid, session: data.session });
	} catch (err) {
		removeError.value = err.message;
	} finally {
		removing.value = false;
	}
}

defineExpose({ goTo, resetView });
</script>

<template>
	<div class="sbg-viewer-3d">
		<ThreeJsViewer
			ref="viewerRef"
			:citymodel="citymodel"
			:selected-objid="selectedObjid"
			:boundary-ring="boundaryRing"
			:highlight-footprint-rings="highlightFootprintRings"
			@object_clicked="onObjectClicked"
			@rendering="onRendering"
			@chunkLoaded="onChunkLoaded"
		/>
		<div v-if="fetching || parsing" class="loading-badge">
			<span v-if="fetching">fetching…</span>
			<span v-else>rendering… ({{ chunkProgress }} / ~{{ chunkEstimateTotal }} chunks)</span>
		</div>
		<div v-if="selectedAttributes" class="info-panel">
			<div class="info-panel-header">{{ selectedObjid }}</div>
			<table>
				<tbody>
					<tr v-for="(value, key) in selectedAttributes" :key="key">
						<td class="k">{{ key }}</td>
						<td class="v">{{ value === null || value === '' ? '—' : value }}</td>
					</tr>
				</tbody>
			</table>
			<button class="remove-btn" :disabled="removing" @click="removeSelected">
				{{ removing ? 'Removing…' : 'Remove building' }}
			</button>
			<div v-if="removeError" class="remove-error">{{ removeError }}</div>
		</div>
	</div>
</template>

<style scoped>
.sbg-viewer-3d {
	position: relative;
	width: 100%;
	height: 100%;
}
.loading-badge {
	position: absolute;
	bottom: 8px;
	left: 8px;
	z-index: 10;
	background: rgba(0, 0, 0, 0.6);
	color: white;
	padding: 4px 10px;
	border-radius: 4px;
	font-family: monospace;
	font-size: 12px;
}
.info-panel {
	position: absolute;
	top: 8px;
	right: 8px;
	z-index: 10;
	background: rgba(11, 18, 32, 0.92);
	color: #ddd;
	border: 1px solid #3a4358;
	border-radius: 6px;
	padding: 8px 10px;
	font-family: monospace;
	font-size: 12px;
	max-width: 320px;
	max-height: 70vh;
	overflow-y: auto;
}
.info-panel-header {
	font-weight: bold;
	color: #4a9eff;
	margin-bottom: 6px;
	word-break: break-all;
}
.info-panel table {
	border-collapse: collapse;
	width: 100%;
}
.info-panel td {
	padding: 1px 4px;
	vertical-align: top;
}
.info-panel td.k {
	color: #8a97ad;
	white-space: nowrap;
	padding-right: 8px;
}
.info-panel td.v {
	word-break: break-word;
}
.remove-btn {
	margin-top: 8px;
	width: 100%;
	cursor: pointer;
	padding: 4px 10px;
	background: #4a1f24;
	color: #ff8080;
	border: 1px solid #7a2e35;
	border-radius: 4px;
	font-family: monospace;
	font-size: 12px;
}
.remove-btn:disabled {
	opacity: 0.6;
	cursor: default;
}
.remove-btn:hover:not(:disabled) {
	background: #5c262d;
}
.remove-error {
	margin-top: 4px;
	color: #ff8080;
	font-size: 11px;
}
</style>
