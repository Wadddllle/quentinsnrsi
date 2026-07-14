<script setup>
import { ref, shallowRef, onMounted } from 'vue';
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

const emit = defineEmits(['object_clicked', 'loaded', 'error']);

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
function goTo(worldX, worldY, radius = 300) {
	const viewer = viewerRef.value;
	if (!viewer || !viewer.camera) return;
	const localX = worldX - localFrameOffset.value.x;
	const localY = worldY - localFrameOffset.value.y;
	const box = new THREE.Box3(
		new THREE.Vector3(localX - radius, localY - radius, 0),
		new THREE.Vector3(localX + radius, localY + radius, 100)
	);
	viewer.fitCameraToSelection(viewer.camera, viewer.controls, box);
}

onMounted(loadFullIsland);

function onObjectClicked(info) {
	emit('object_clicked', info);
}

defineExpose({ goTo });
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
</style>
