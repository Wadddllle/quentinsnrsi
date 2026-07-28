<script setup>
import { ref, shallowRef, computed, onMounted, watch } from 'vue';
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
	// Phase 4a (metadata editing): the last mutation App.vue's global Undo
	// button reversed, so an edited-attributes override for that building
	// (if any) can be cleared -- see attributeOverrides below.
	lastUndone: { type: Object, default: null },
	// Phase 4b (add-building): footprint+height records for buildings added
	// this session -- only the ids are actually used here (see
	// addedCitymodel below, which fetches each id's REAL geometry rather
	// than approximating it from the footprint+height record itself).
	// Placement UI moved to 2D (OrthoWebGLView) -- see its own comments for
	// why (no real use for a Z axis, camera-orbit fighting with ghost-
	// follow, and "press add before the real mesh has rendered" were all
	// real problems with the original 3D ghost-preview flow).
	addedFootprints: { type: Array, default: () => [] },
	// Phase 4c (OneMap review-gate): straight passthrough to ThreeJsViewer,
	// no fetch/state logic needed here -- unlike addedCitymodel above,
	// OneMapReviewPanel.vue already owns the proposal state (it knows
	// exactly when to fetch/refetch, e.g. after a nudge) and hands the
	// resulting mini-citymodel down through App.vue as a plain prop.
	reviewOldFootprintRings: { type: Array, default: () => [] },
	reviewCitymodel: { type: Object, default: null },
	// Phase 4c pooling: whether a OneMap replacement is currently open for
	// review -- when true, the info-panel offers "Add to current OneMap
	// replacement" for whatever's selected, alongside Remove/Replace.
	oneMapProposalActive: { type: Boolean, default: false },
	// Real per-object hide/dim shader mechanism (see ThreeJsViewer.vue's
	// own comment) -- straight passthrough, same as reviewOldFootprintRings
	// above, no fetch/state logic needed here.
	removedIds: { type: Array, default: () => [] },
	reviewOldBuildingIds: { type: Array, default: () => [] },
	showIslandTerrain: { type: Boolean, default: true },
	editedIds: { type: Array, default: () => [] },
});

const emit = defineEmits(['object_clicked', 'loaded', 'error', 'building_removed', 'attributes_edited', 'replace_with_onemap', 'add_to_onemap_replacement', 'height_edited']);

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
// Phase 4a (metadata editing): a small local override map, NOT stored on
// citymodel -- citymodel is a shallowRef, always reassigned wholesale, and
// ThreeJsViewer's citymodel watcher does a full expensive teardown+reload
// on ANY reassignment (see the file-top comment on localFrameOffset/the
// project plan's own writeup on this exact trap). Reassigning citymodel.value
// just to reflect one edited attribute would trigger that same full reload
// for a one-field edit -- attributeOverrides is deliberately a separate,
// small, plain ref instead (only ever holds entries for buildings actually
// edited this session, dozens at most, so normal deep reactivity here is
// cheap and safe, unlike the 118k-building CityJSON graph).
const attributeOverrides = ref({});

const selectedAttributes = computed(() => {
	if (!props.selectedObjid) return null;
	const base = citymodel.value.CityObjects?.[props.selectedObjid]?.attributes;
	if (!base) return null;
	return { ...base, ...(attributeOverrides.value[props.selectedObjid] ?? {}) };
});

// Real bug found via user report: clicking the OneMap review overlay's
// proposed mesh (a real, if unusual, thing to do -- "sometimes you need to
// click it to see how big it is") resolves selectedObjid to a synthetic id
// like "proposal/<gml_id>" (see build_preview_citymodel) that doesn't exist
// in the main citymodel -- selectedAttributes above correctly returns null
// for it, but the OLD template gated the entire .info-panel (including the
// "Add to current OneMap replacement" pool button) on selectedAttributes
// being truthy, so clicking the overlay made the whole panel vanish. That's
// exactly the flow a user pooling multiple buildings hits: click the
// proposed mesh to gauge its size, then look for the pool button and find
// no panel at all. Same applies to Phase 4b's added-buildings overlay.
// This computed identifies which overlay (if any) the id belongs to, purely
// so the panel can show a small, honest "not a real building yet" message
// instead of disappearing -- selectedAttributes itself (and therefore
// Remove/Replace/pool, all of which need a REAL building) is untouched.
const selectedOverlaySource = computed(() => {
	if (!props.selectedObjid || selectedAttributes.value) return null;
	if (props.reviewCitymodel?.CityObjects?.[props.selectedObjid]) return 'review';
	if (addedCitymodel.value?.CityObjects?.[props.selectedObjid]) return 'added';
	return null;
});

watch(
	() => props.lastUndone,
	(entry) => {
		if (!entry?.building_id) return;
		if (entry.op !== 'edit_attributes' && entry.op !== 'edit_height') return;
		if (!(entry.building_id in attributeOverrides.value)) return;
		const next = { ...attributeOverrides.value };
		delete next[entry.building_id];
		attributeOverrides.value = next;
	}
);

// Fields a real geometry-mutating operation (remove/add, later the OneMap
// review-gate) is responsible for keeping in sync with actual geometry --
// mirrors sbg/ui/routers/attributes.py's own _BLOCKLIST exactly. Enforced
// server-side too (400 if sent); kept here as well so the edit UI never
// even offers to change them, rather than relying on a round-trip failure.
const ATTRIBUTE_BLOCKLIST = new Set([
	'height', 'height_source', 'mesh_vertex_count', 'mesh_face_count',
	'mesh_watertight', 'onemap_gml_id', 'onemap_storeys', 'onemap_name',
]);

const editingAttrs = ref(false);
const editValues = ref({});
const savingAttrs = ref(false);
const attrsError = ref(null);

function startEditAttrs() {
	if (!selectedAttributes.value) return;
	editValues.value = { ...selectedAttributes.value };
	attrsError.value = null;
	editingAttrs.value = true;
}

function cancelEditAttrs() {
	editingAttrs.value = false;
	attrsError.value = null;
}

watch(
	() => props.selectedObjid,
	() => {
		editingAttrs.value = false;
		attrsError.value = null;
	}
);

async function saveEditAttrs() {
	if (!props.selectedObjid || savingAttrs.value) return;
	const base = selectedAttributes.value ?? {};
	const changed = {};
	for (const key of Object.keys(editValues.value)) {
		if (ATTRIBUTE_BLOCKLIST.has(key)) continue;
		if (editValues.value[key] !== base[key]) changed[key] = editValues.value[key];
	}
	if (Object.keys(changed).length === 0) {
		editingAttrs.value = false;
		return;
	}
	savingAttrs.value = true;
	attrsError.value = null;
	try {
		const res = await fetch(`/api/buildings/${props.selectedObjid}/attributes`, {
			method: 'PATCH',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ attributes: changed }),
		});
		const data = await res.json();
		if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
		attributeOverrides.value = {
			...attributeOverrides.value,
			[props.selectedObjid]: { ...(attributeOverrides.value[props.selectedObjid] ?? {}), ...changed },
		};
		editingAttrs.value = false;
		emit('attributes_edited', { id: props.selectedObjid, session: data.session });
	} catch (err) {
		attrsError.value = err.message;
	} finally {
		savingAttrs.value = false;
	}
}

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

// Direct user request: "only rare weird cases are absurdly wrong, like
// Marina Bay Sands" -- OneMap backfill occasionally assigns a tower's real
// neighbor's height (see sbg/onemap/backfill.py's own honest ~9.5%-
// ambiguous-match finding). LoD1/extruded only, matching the backend's own
// authoritative check -- height_source==='onemap_mesh' is this project's
// established signal for "real mesh geometry, no single roof height to
// move" (every other code path that produces MultiSurface geometry tags
// this exact value). This is a client-side pre-filter for a clean UI only;
// the backend rejects non-Solid geometry regardless.
const canEditHeight = computed(() => selectedAttributes.value && selectedAttributes.value.height_source !== 'onemap_mesh');
const editingHeight = ref(false);
const heightInputValue = ref(null);
const savingHeight = ref(false);
const heightError = ref(null);

function startEditHeight() {
	if (!selectedAttributes.value) return;
	heightInputValue.value = selectedAttributes.value.height;
	heightError.value = null;
	editingHeight.value = true;
}

function cancelEditHeight() {
	editingHeight.value = false;
	heightError.value = null;
}

watch(
	() => props.selectedObjid,
	() => {
		editingHeight.value = false;
		heightError.value = null;
	}
);

async function saveEditHeight() {
	if (!props.selectedObjid || savingHeight.value) return;
	const h = Number(heightInputValue.value);
	if (!Number.isFinite(h) || h <= 0) {
		heightError.value = 'Height must be a positive number';
		return;
	}
	savingHeight.value = true;
	heightError.value = null;
	try {
		const res = await fetch(`/api/buildings/${props.selectedObjid}/height`, {
			method: 'PATCH',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ height: h }),
		});
		const data = await res.json();
		if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
		// Same reason attribute edits use attributeOverrides instead of
		// touching citymodel directly (see its own comment) -- real bug
		// found via testing: without this, the backend edit genuinely
		// succeeded (session count went up, 3D geometry updated via the
		// overlay) but the info panel kept showing the OLD height, since
		// selectedAttributes reads from the never-reloaded shared citymodel.
		attributeOverrides.value = {
			...attributeOverrides.value,
			[props.selectedObjid]: { ...(attributeOverrides.value[props.selectedObjid] ?? {}), height: h, height_source: 'manual' },
		};
		editingHeight.value = false;
		emit('height_edited', { id: props.selectedObjid, footprint: data.footprint, session: data.session });
	} catch (err) {
		heightError.value = err.message;
	} finally {
		savingHeight.value = false;
	}
}

// Phase 4b (add-building): real geometry for the 3D overlay, fetched from
// /api/buildings/geometry rather than approximated client-side (see
// ThreeJsViewer.vue's own comment on why an earlier box-extrusion version
// was wrong for Path 2/3). Refetches the WHOLE current added-id set on any
// change (add or undo) instead of trying to incrementally merge vertex
// pools across separate fetches -- simple, and cheap at the "dozens, not
// thousands" scale this overlay is meant for (same tradeoff already
// accepted for SpatialIndex.pending_additions). Skipped if the id set is
// unchanged (e.g. an unrelated prop reactivity tick) via a plain sorted-
// join key comparison.
const addedCitymodel = shallowRef(null);
let lastAddedIdsKey = '';

watch(
	() => props.addedFootprints.map((r) => r.id),
	async (ids) => {
		const key = [...ids].sort().join(',');
		if (key === lastAddedIdsKey) return;
		lastAddedIdsKey = key;
		if (ids.length === 0) {
			addedCitymodel.value = null;
			return;
		}
		try {
			const qs = new URLSearchParams({ ids: ids.join(',') });
			const res = await fetch(`/api/buildings/geometry?${qs}`);
			if (!res.ok) return;
			addedCitymodel.value = await res.json();
		} catch {
			// leave the previous overlay state as-is -- the next add/undo will retry
		}
	},
	{ immediate: true }
);

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
			:added-citymodel="addedCitymodel"
			:review-old-footprint-rings="reviewOldFootprintRings"
			:review-citymodel="reviewCitymodel"
			:removed-ids="removedIds"
			:review-old-building-ids="reviewOldBuildingIds"
			:show-island-terrain="showIslandTerrain"
			:edited-ids="editedIds"
			@object_clicked="onObjectClicked"
			@rendering="onRendering"
			@chunkLoaded="onChunkLoaded"
		/>
		<div v-if="fetching || parsing" class="loading-badge">
			<span v-if="fetching">fetching…</span>
			<span v-else>rendering… ({{ chunkProgress }} / ~{{ chunkEstimateTotal }} chunks)</span>
		</div>
		<div v-if="selectedAttributes" class="info-panel">
			<div class="info-panel-header-row">
				<div class="info-panel-header">{{ selectedObjid }}</div>
				<button v-if="!editingAttrs" class="edit-toggle-btn" @click="startEditAttrs">Edit</button>
			</div>
			<table>
				<tbody>
					<tr v-for="(value, key) in selectedAttributes" :key="key">
						<td class="k">{{ key }}</td>
						<td class="v" v-if="!editingAttrs">{{ value === null || value === '' ? '—' : value }}</td>
						<td class="v" v-else>
							<input
								v-if="!ATTRIBUTE_BLOCKLIST.has(key)"
								v-model="editValues[key]"
								class="attr-input"
							/>
							<span v-else class="attr-locked" :title="'Geometry-linked, edit via Remove/Replace instead'">{{ value === null || value === '' ? '—' : value }}</span>
						</td>
					</tr>
				</tbody>
			</table>
			<div v-if="editingAttrs" class="edit-actions">
				<button class="save-attrs-btn" :disabled="savingAttrs" @click="saveEditAttrs">
					{{ savingAttrs ? 'Saving…' : 'Save changes' }}
				</button>
				<button class="cancel-attrs-btn" :disabled="savingAttrs" @click="cancelEditAttrs">Cancel</button>
			</div>
			<div v-if="attrsError" class="remove-error">{{ attrsError }}</div>
			<div v-if="!editingAttrs && canEditHeight" class="height-edit-row">
				<template v-if="!editingHeight">
					<button class="fix-height-btn" @click="startEditHeight">Fix height…</button>
				</template>
				<template v-else>
					<input type="number" v-model.number="heightInputValue" class="attr-input height-input" step="any" min="0" />
					<button class="save-attrs-btn" :disabled="savingHeight" @click="saveEditHeight">
						{{ savingHeight ? 'Saving…' : 'Save' }}
					</button>
					<button class="cancel-attrs-btn" :disabled="savingHeight" @click="cancelEditHeight">Cancel</button>
				</template>
				<div v-if="heightError" class="remove-error">{{ heightError }}</div>
			</div>
			<div v-if="!editingAttrs" class="info-panel-actions">
				<button class="remove-btn" :disabled="removing" @click="removeSelected">
					{{ removing ? 'Removing…' : 'Remove building' }}
				</button>
				<button class="onemap-btn" @click="emit('replace_with_onemap', { buildingId: selectedObjid })">
					Replace with OneMap…
				</button>
			</div>
			<button
				v-if="!editingAttrs && oneMapProposalActive"
				class="onemap-btn pool-btn"
				@click="emit('add_to_onemap_replacement', { buildingId: selectedObjid })"
			>
				Add to current OneMap replacement
			</button>
			<div v-if="removeError" class="remove-error">{{ removeError }}</div>
		</div>
		<!-- Real bug fix: clicking the OneMap review overlay's proposed mesh
		     (or a same-session added-building overlay) used to make the WHOLE
		     info panel vanish, including the pool button, since selectedAttributes
		     is only ever non-null for a REAL main-citymodel building -- see
		     selectedOverlaySource's own comment. This isn't a real building, so
		     no Remove/Replace/pool actions apply, but a click that lands here was
		     deliberate (checking the proposed mesh's extent), so it gets honest
		     feedback instead of nothing. -->
		<div v-else-if="selectedOverlaySource" class="info-panel">
			<div class="info-panel-header-row">
				<div class="info-panel-header">{{ selectedObjid }}</div>
			</div>
			<div class="hint">
				{{ selectedOverlaySource === 'review' ? 'Proposed OneMap mesh (not yet approved) -- click a real building nearby to pool it into this replacement.' : 'A building added this session.' }}
			</div>
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
	/* Real bug, user report: this used to dock top-right, same corner as
	   OneMapReviewPanel.vue/VersionHistoryPanel.vue (both also top:44px;
	   right:8px). Those sit at a higher z-index, so whenever either was
	   open, this panel rendered fully underneath it -- completely hidden,
	   not merely visually competing. That's exactly the situation pooling
	   requires (review panel open + a building selected), so the
	   "Add to current OneMap replacement" button was never actually visible.
	   Bottom-right instead -- doesn't collide with anything else docked in
	   this view (loading-badge is bottom-LEFT). */
	position: absolute;
	bottom: 8px;
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
.info-panel-header-row {
	display: flex;
	align-items: flex-start;
	justify-content: space-between;
	gap: 8px;
	margin-bottom: 6px;
}
.info-panel-header {
	font-weight: bold;
	color: #4a9eff;
	word-break: break-all;
}
.hint {
	color: #8a97ad;
	font-size: 12px;
}
.edit-toggle-btn {
	flex-shrink: 0;
	cursor: pointer;
	padding: 2px 8px;
	background: #262e42;
	color: #ddd;
	border: 1px solid #3a4358;
	border-radius: 4px;
	font-family: monospace;
	font-size: 11px;
}
.attr-input {
	width: 100%;
	box-sizing: border-box;
	padding: 1px 4px;
	background: #0b1220;
	color: #ddd;
	border: 1px solid #3a4358;
	border-radius: 3px;
	font-family: monospace;
	font-size: 12px;
}
.attr-locked {
	color: #5a6478;
}
.edit-actions {
	margin-top: 8px;
	display: flex;
	gap: 6px;
}
.save-attrs-btn,
.cancel-attrs-btn {
	flex: 1;
	cursor: pointer;
	padding: 4px 10px;
	background: #262e42;
	color: #ddd;
	border: 1px solid #3a4358;
	border-radius: 4px;
	font-family: monospace;
	font-size: 12px;
}
.save-attrs-btn:disabled,
.cancel-attrs-btn:disabled {
	opacity: 0.6;
	cursor: default;
}
.save-attrs-btn:hover:not(:disabled) {
	background: #1f3a2e;
}
.height-edit-row {
	margin-top: 6px;
	display: flex;
	align-items: center;
	gap: 6px;
	flex-wrap: wrap;
}
.fix-height-btn {
	cursor: pointer;
	padding: 3px 8px;
	background: #262e42;
	color: #ffb347;
	border: 1px solid #7a5a2e;
	border-radius: 4px;
	font-family: monospace;
	font-size: 11px;
}
.height-input {
	width: 80px;
	flex: none;
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
.info-panel-actions {
	margin-top: 8px;
	display: flex;
	gap: 6px;
}
.remove-btn {
	flex: 1;
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
.onemap-btn {
	flex: 1;
	cursor: pointer;
	padding: 4px 10px;
	background: #1f2e4a;
	color: #9fd4ff;
	border: 1px solid #2e4a7a;
	border-radius: 4px;
	font-family: monospace;
	font-size: 12px;
}
.onemap-btn:hover {
	background: #263a5c;
}
.pool-btn {
	margin-top: 6px;
	width: 100%;
}
.remove-error {
	margin-top: 4px;
	color: #ff8080;
	font-size: 11px;
}
</style>
