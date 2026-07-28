<script setup>
import { ref, shallowRef, computed, onMounted, onUnmounted } from 'vue';
import SbgViewer3D from './components/three/SbgViewer3D.vue';
import BoundaryDrawTool from './components/plan2d/BoundaryDrawTool.vue';
import LocationSearchBox from './components/LocationSearchBox.vue';
import VersionHistoryPanel from './components/versioning/VersionHistoryPanel.vue';
import ImportBuildingPanel from './components/plan2d/ImportBuildingPanel.vue';
import OneMapReviewPanel from './components/onemap/OneMapReviewPanel.vue';
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

// Phase 3 (remove-building): ids currently removed from the working
// dataset -- NOT session-scoped (unlike sessionStatus below, this persists
// across a Save Version, since the building really is gone from the file
// either way; it only resets on a full Restore, which reloads the page --
// see onRestored). Fed to both views the same way crossingIds already is:
// OrthoWebGLView recolors matching triangles (cheap, existing per-vertex
// color-attribute mechanism), ThreeJsViewer draws an outline overlay
// (cheap, existing Line2 fat-line mechanism) -- neither actually hides the
// removed building's real geometry, see SbgViewer3D.vue's own comment on
// why that's a deliberate scope decision, not an oversight.
const removedIds = ref([]);
// Direct user request: fix "absurdly wrong" LoD1 heights from the app
// (Marina Bay Sands and similar OneMap-backfill mismatches). Separate from
// removedIds -- see ThreeJsViewer.vue's own comment on why sharing that
// array would incorrectly trigger the 2D view's "removed" gray recolor for
// a building that's still there, just height-corrected. 3D-only, no 2D
// counterpart needed (2D doesn't render height at all).
const editedIds = ref([]);
const sessionStatus = ref({ dirty: false, count: 0, summary: 'No changes', log: [] });
const saveNote = ref('');
const savingVersion = ref(false);
const saveError = ref(null);
const showHistory = ref(false);

// Toolbar redesign: the mode bar had grown into 11+ always-visible controls
// in one row ("we have just been adding more and more buttons... it was
// originally so clean one row" -- direct user feedback). View/Edit/Add now
// each collapse behind their own "X ▾" dropdown, same pattern Add Building
// already used -- closeMenus() ensures opening one always closes the
// others, so at most one dropdown is ever open at a time.
const showViewMenu = ref(false);
const showEditMenu = ref(false);
const showAddMenu = ref(false);
const showOneMapMenu = ref(false);

function closeMenus() {
	showViewMenu.value = false;
	showEditMenu.value = false;
	showAddMenu.value = false;
	showOneMapMenu.value = false;
}

const MENU_REFS = { view: showViewMenu, edit: showEditMenu, add: showAddMenu, onemap: showOneMapMenu };

function toggleMenu(which) {
	const wasOpen = MENU_REFS[which].value;
	closeMenus();
	if (!wasOpen) MENU_REFS[which].value = true;
}

// Phase 4b (add-building): the three paths behind "Add ▾". Path 1
// (hypothetical/parametric) reuses BoundaryDrawTool's own point-collection
// machinery via its `mode` prop, so activating it just switches to 2D +
// flips this flag -- no new drawing code needed. Paths 2/3 (import CityJSON
// / import mesh) open ImportBuildingPanel, a small self-contained upload UI.
const addHypotheticalActive = ref(false);
const showImportPanel = ref(false);
const importPanelPath = ref('cityjson'); // 'cityjson' | 'mesh'

function openAddHypothetical() {
	activateMode('2d');
	addHypotheticalActive.value = true;
	showAddMenu.value = false;
}

function openImportPanel(path) {
	importPanelPath.value = path;
	showImportPanel.value = true;
	showAddMenu.value = false;
}

// Path 3's "Place manually" hands off from ImportBuildingPanel (2D-level UI)
// to the interactive ghost-preview flow -- 2D now (not 3D, see
// OrthoWebGLView.vue's own comment on why: no real use for a Z axis when
// placing a footprint, camera-orbit fighting with ghost-follow, and "press
// add before the real mesh has rendered" were all real problems with the
// original 3D flow, per direct user feedback). BoundaryDrawTool reads
// pendingMeshPlacement as a prop and forwards it to OrthoWebGLView, which
// owns the actual raycasting/rotation/confirm interaction.
const pendingMeshPlacement = ref(null);

function onStartMeshPlacement({ file, note }) {
	showImportPanel.value = false;
	activateMode('2d');
	pendingMeshPlacement.value = { file, note };
}

function onMeshPlacementDone() {
	pendingMeshPlacement.value = null;
}

// Buildings added THIS session -- fed to SbgViewer3D as an overlay
// (approximate extruded boxes, see ThreeJsViewer.vue's own comment) so a
// new building shows up in 3D immediately instead of only after the next
// full whole-island reload, which this app deliberately never triggers on
// its own (see project plan: reloading is a multi-minute cost, not
// something to pay per click).
const addedFootprints = ref([]);

// Every add-endpoint response now carries the new building's own
// {id, rings, height, height_source} record(s) directly (see
// sbg/ui/routers/buildings.py) -- pushed straight into local state instead
// of re-fetching the full ~118k-building footprint list just to reflect one
// new building. 2D and 3D both update immediately from the same data.
function pushAddedFootprint(rec) {
	if (!rec) return;
	footprints.value = [...footprints.value, rec];
	footprintsById.value = { ...footprintsById.value, [rec.id]: rec };
	addedFootprints.value = [...addedFootprints.value, rec];
}

function onBuildingAdded(data) {
	sessionStatus.value = data.session;
	addHypotheticalActive.value = false;
	showImportPanel.value = false;
	if (data.footprint) pushAddedFootprint(data.footprint);
	if (data.footprints) for (const rec of data.footprints) pushAddedFootprint(rec);
}

// "Draw hypothetical" mode has no other way back to plain boundary-drawing
// without either submitting a building or refreshing the page -- direct
// user feedback. BoundaryDrawTool's own Cancel button clears its local
// drawing state and emits this so App.vue can drop the flag it owns.
function onAddHypotheticalCancelled() {
	addHypotheticalActive.value = false;
}

async function fetchSessionStatus() {
	try {
		const res = await fetch('/api/session/status');
		if (res.ok) sessionStatus.value = await res.json();
	} catch {
		// non-fatal -- the badge just stays stale until the next successful poll
	}
}
fetchSessionStatus();

function onBuildingRemoved({ id, session }) {
	if (!removedIds.value.includes(id)) removedIds.value = [...removedIds.value, id];
	sessionStatus.value = session;
	if (selectedObjid.value === id) selectedObjid.value = null;
}

// Phase 4c (OneMap review-gate): two entry points converge on the same
// OneMapReviewPanel -- (a) "Replace with OneMap..." in the 3D info-panel
// (context = { buildingId }, point-in-polygon auto-discovery -- tested
// reliable, unchanged) and (b) the mode-bar's "Add from OneMap..." (context
// = {}, starts at a name search instead of automatic discovery -- see
// OneMapReviewPanel.vue's own comment for why the original "click/stand
// near a point" version was dropped entirely after real use showed it
// didn't work and wasn't what was actually wanted). Opening either forces
// 3D mode (the whole point is a contextual, in-place preview).
const showOneMapPanel = ref(false);
const oneMapContext = ref(null);
const reviewOldFootprintRings = ref([]);
const reviewCitymodel = shallowRef(null);

// Pooling (real user request: "I realize the fetched mesh also covers a
// couple of adjacent buildings, can I pool them into the replacement"):
// once a proposal exists, the 3D info-panel offers "Add to current OneMap
// replacement" for whatever's currently selected. activeOneMapProposal
// tracks whether that offer should show at all; pendingAddToOneMapOld is
// the same prop-driven handoff pattern already used for placeMeshRequest
// in Phase 4b (parent sets it, child watches + acts + acks back to null).
const activeOneMapProposal = ref(null); // { proposalId, oldBuildingIds } | null
const pendingAddToOneMapOld = ref(null);

function onReplaceWithOneMap({ buildingId }) {
	oneMapContext.value = { buildingId };
	activateMode('3d');
	showOneMapPanel.value = true;
}

function onAddFromOneMap() {
	oneMapContext.value = {};
	activateMode('3d');
	showOneMapPanel.value = true;
}

function onOneMapPanelClosed() {
	showOneMapPanel.value = false;
	oneMapContext.value = null;
	reviewOldFootprintRings.value = [];
	reviewCitymodel.value = null;
	activeOneMapProposal.value = null;
}

function onOneMapPreviewChanged({ oldFootprintRings, citymodel }) {
	reviewOldFootprintRings.value = oldFootprintRings || [];
	reviewCitymodel.value = citymodel;
}

function onOneMapProposalReady({ proposalId, oldBuildingIds }) {
	activeOneMapProposal.value = { proposalId, oldBuildingIds };
}

// 3D info-panel's "Add to current OneMap replacement" button -> hand off to
// the open panel via the pending-prop, mirroring placeMeshRequest.
function onAddToOneMapReplacement({ buildingId }) {
	pendingAddToOneMapOld.value = buildingId;
}

function onAckAddToOldSet() {
	pendingAddToOneMapOld.value = null;
}

// Approve is exactly "remove the old, add the new" -- reuses the SAME
// removedIds/addedFootprints display mechanisms a plain Remove+Add already
// gets for free (gray outline for the old, real-geometry overlay for the
// new), rather than inventing a third rendering path for the committed
// result.
function onOneMapApproved({ oldBuildingIds, newFootprints, session }) {
	sessionStatus.value = session;
	for (const id of oldBuildingIds) {
		if (!removedIds.value.includes(id)) removedIds.value = [...removedIds.value, id];
	}
	for (const rec of newFootprints || []) pushAddedFootprint(rec);
	reviewOldFootprintRings.value = [];
	reviewCitymodel.value = null;
	activeOneMapProposal.value = null;
}

// Phase 4a (metadata editing): SbgViewer3D owns the attribute-override map
// (see its own comment on why that state can't live on the shared
// citymodel ref) -- this just mirrors sessionStatus, same as every other
// mutation.
function onAttributesEdited({ session }) {
	sessionStatus.value = session;
}

// Height-edit: hides the stale (wrong-height) real geometry in the main
// whole-island mesh via editedIds (see its own comment) and shows the
// corrected shape immediately via the SAME added-buildings overlay
// mechanism a genuine add already uses -- reused, not duplicated, since
// "here's this one building's real current geometry, render it" is exactly
// the same need either way.
function onHeightEdited({ id, footprint, session }) {
	sessionStatus.value = session;
	if (!editedIds.value.includes(id)) editedIds.value = [...editedIds.value, id];
	if (footprint) pushAddedFootprint(footprint);
}

// Phase 4a: passed down to SbgViewer3D so it can clear an edited-attributes
// override when Undo reverses that specific edit -- a plain ref reassigned
// on every undo() call (not just when the undone op happens to be
// edit_attributes) so SbgViewer3D's watcher always sees a fresh object
// reference and fires correctly even if two edit_attributes undos happen
// back to back.
const lastUndone = ref(null);

async function undo() {
	try {
		const res = await fetch('/api/buildings/undo', { method: 'POST' });
		const data = await res.json();
		if (!res.ok) return;
		sessionStatus.value = data.session;
		// data.undone is a list now (Phase 4b: a multi-object import undoes
		// as one atomic action, see sbg/ui/session.py's record_group) --
		// the overwhelming majority of undos are still a single entry, so
		// SbgViewer3D's own lastUndone watcher (Phase 4a) only needs the
		// first one to react to an edit_attributes revert.
		lastUndone.value = data.undone[0] ?? null;
		const removedNowById = new Set(data.undone.filter((e) => e.op === 'remove').map((e) => e.building_id));
		if (removedNowById.size > 0) {
			removedIds.value = removedIds.value.filter((id) => !removedNowById.has(id));
		}
		// An undone "add" needs to disappear from both the pushed-directly
		// footprint state (see pushAddedFootprint) and the 3D overlay -- it
		// was never in the real dataset an undo would otherwise resync from.
		const addedNowUndoneById = new Set(data.undone.filter((e) => e.op === 'add').map((e) => e.building_id));
		if (addedNowUndoneById.size > 0) {
			footprints.value = footprints.value.filter((rec) => !addedNowUndoneById.has(rec.id));
			const byId = { ...footprintsById.value };
			for (const id of addedNowUndoneById) delete byId[id];
			footprintsById.value = byId;
			addedFootprints.value = addedFootprints.value.filter((rec) => !addedNowUndoneById.has(rec.id));
		}
		// An undone height-edit reverts the REAL geometry in the main mesh
		// back to its original (correct-again) shape -- editedIds's hide
		// and the corrected-height overlay both need to go, or the now-
		// reverted-but-still-hidden building would show nothing at all.
		const heightEditedUndoneById = new Set(data.undone.filter((e) => e.op === 'edit_height').map((e) => e.building_id));
		if (heightEditedUndoneById.size > 0) {
			editedIds.value = editedIds.value.filter((id) => !heightEditedUndoneById.has(id));
			addedFootprints.value = addedFootprints.value.filter((rec) => !heightEditedUndoneById.has(rec.id));
		}
	} catch {
		// leave state as-is -- user can retry
	}
}

async function saveVersion() {
	if (savingVersion.value) return;
	savingVersion.value = true;
	saveError.value = null;
	try {
		const res = await fetch('/api/versions/save', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ note: saveNote.value || null }),
		});
		const data = await res.json();
		if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
		saveNote.value = '';
		await fetchSessionStatus();
	} catch (err) {
		saveError.value = err.message;
	} finally {
		savingVersion.value = false;
	}
}

// Restoring an old version is a big, rare, explicit change to the working
// file -- rather than hand-reconcile removedIds/footprints/3D citymodel
// state against whatever the restored commit actually contained (which has
// no "removed ids" concept of its own, it's just a plain building set), a
// full page reload brings 2D+3D both cleanly back in sync with the
// restored file. Matches this project's own established preference for
// simple+correct over clever+fragile when a real state discontinuity like
// this is involved.
function onRestored() {
	window.location.reload();
}

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

// Same shape as highlightFootprintRings above, different source id array --
// see SbgViewer3D.vue/ThreeJsViewer.vue for the outline-overlay rendering.
const removedFootprintRings = computed(() => {
	const byId = footprintsById.value;
	const rings = [];
	for (const id of removedIds.value) {
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

// "Stuck in zoom hell" QoL fix -- one button, re-fits whichever view is
// currently active back to its full loaded extent. Both viewers already
// track their own real extent internally (see resetView() in
// ThreeJsViewer.vue/OrthoWebGLView.vue), so this just calls whichever one is
// on screen rather than duplicating that bookkeeping here.
function resetView() {
	if (mode.value === '2d') {
		boundaryToolRef.value?.resetView();
	} else {
		viewer3dRef.value?.resetView();
	}
}

// "Add a button to snap camera in 3D view from 2D view orientation" -- reads
// the 2D camera's current visible extent and points the 3D camera at the
// same real-world area. Deliberately an explicit action, not automatic on
// every mode switch (that dual-mode design was tried and dropped earlier
// per direct user instruction -- see the comment block above -- this is a
// much smaller, opt-in version of the same idea, not a revival of it).
//
// Two follow-up fixes after real testing showed content wasn't actually
// overlapping between the two views on switch:
// - topDown: true forces a clean straight-down orientation matching
//   OrthoWebGLView's 2D camera (see fitCameraToSelection's topDown branch)
//   -- previously the 3D camera kept whatever angle it already had, so even
//   a perfectly-matched center/zoom still LOOKED different.
// - halfWidth/halfHeight passed separately (2D's exact bounds, not a single
//   square radius) with fitOffset: 1.0 (no padding) -- goTo's default
//   fitOffset is 1.2 (a deliberate 20% margin for general search
//   navigation), but that margin means the 3D frame would show MORE area
//   than 2D's exact current view, i.e. look more "zoomed out" -- exactly
//   the "zoom's not perfect" symptom reported. 1.0 matches 2D's framing
//   exactly instead of padding it.
function snapTo2dView() {
	const bounds = boundaryToolRef.value?.getViewBounds();
	if (!bounds || !has3DBeenActivated.value) return;
	const { xmin, ymin, xmax, ymax } = bounds;
	const cx = (xmin + xmax) / 2, cy = (ymin + ymax) / 2;
	const halfWidth = (xmax - xmin) / 2;
	const halfHeight = (ymax - ymin) / 2;
	viewer3dRef.value?.goTo(cx, cy, halfWidth, halfHeight, { fitOffset: 1.0, topDown: true });
}

// Whole-island terrain overlay (see ThreeJsViewer.vue's own comment on why
// buildings now render through it unconditionally regardless of its real
// accuracy) -- still worth being able to turn off entirely, e.g. to compare
// against a clean view or reduce visual clutter. "Q" chosen as a free,
// easy-to-reach key with nothing else bound to it yet.
const showIslandTerrain = ref(true);

function toggleIslandTerrain() {
	showIslandTerrain.value = !showIslandTerrain.value;
}

function onKeydown(e) {
	// Don't hijack "q" while the user is typing into a text field (search
	// box, save note, decision note, etc.) -- same guard pattern any
	// global shortcut needs.
	const tag = e.target?.tagName;
	if (tag === 'INPUT' || tag === 'TEXTAREA') return;
	if (e.key === 'q' || e.key === 'Q') toggleIslandTerrain();
}

onMounted(() => window.addEventListener('keydown', onKeydown));
onUnmounted(() => window.removeEventListener('keydown', onKeydown));
</script>

<template>
	<div id="app-root">
		<div class="mode-bar">
			<button :class="{ active: mode === '2d' }" @click="activateMode('2d')">2D</button>
			<button :class="{ active: mode === '3d' }" @click="activateMode('3d')">3D</button>
			<LocationSearchBox @goto="onGoto" />

			<span class="menu">
				<button title="Camera view options" @click="toggleMenu('view')">View ▾</button>
				<div v-if="showViewMenu" class="menu-dropdown">
					<button @click="resetView(); closeMenus()">Reset View</button>
					<button v-if="mode === '3d'" @click="snapTo2dView(); closeMenus()">Snap to 2D view</button>
					<button title="Shortcut: Q" @click="toggleIslandTerrain(); closeMenus()">
						{{ showIslandTerrain ? 'Hide' : 'Show' }} terrain (Q)
					</button>
				</div>
			</span>

			<span class="menu">
				<button title="Undo / save / version history" @click="toggleMenu('edit')">
					Edit ▾<span v-if="sessionStatus.dirty" class="menu-badge">{{ sessionStatus.count }}</span>
				</button>
				<div v-if="showEditMenu" class="menu-dropdown edit-menu-dropdown">
					<div class="menu-row">
						<button title="Undo the last change" :disabled="!sessionStatus.dirty" @click="undo">Undo</button>
						<span v-if="sessionStatus.dirty" class="unsaved-badge">{{ sessionStatus.count }} unsaved</span>
					</div>
					<input
						v-model="saveNote"
						class="save-note"
						type="text"
						placeholder="note (optional)"
						:disabled="savingVersion"
					/>
					<button title="Commit the current state to version history" :disabled="savingVersion" @click="saveVersion">
						{{ savingVersion ? 'Saving…' : 'Save Version' }}
					</button>
					<button title="View / restore past saved versions" @click="showHistory = true; closeMenus()">History</button>
					<span v-if="saveError" class="save-error">{{ saveError }}</span>
				</div>
			</span>

			<span class="menu">
				<button title="Add a building three ways" @click="toggleMenu('add')">Add ▾</button>
				<div v-if="showAddMenu" class="menu-dropdown">
					<button @click="openAddHypothetical">Draw hypothetical</button>
					<button @click="openImportPanel('cityjson')">Import CityJSON</button>
					<button @click="openImportPanel('mesh')">Import mesh (STL/OBJ)</button>
				</div>
			</span>

			<span class="menu">
				<button title="Add a real OneMap building" @click="toggleMenu('onemap')">OneMap ▾</button>
				<div v-if="showOneMapMenu" class="menu-dropdown">
					<button @click="onAddFromOneMap(); closeMenus()">Add from OneMap…</button>
				</div>
			</span>

			<span class="status">
				{{ status2d }} / {{ status3d }}<span v-if="selectedObjid"> — selected: {{ selectedObjid }}</span>
			</span>
		</div>
		<VersionHistoryPanel v-if="showHistory" @close="showHistory = false" @restored="onRestored" />
		<ImportBuildingPanel
			v-if="showImportPanel"
			:path="importPanelPath"
			@close="showImportPanel = false"
			@building-added="onBuildingAdded"
			@start-mesh-placement="onStartMeshPlacement"
		/>
		<OneMapReviewPanel
			v-if="showOneMapPanel"
			:context="oneMapContext"
			:pending-add-to-old-set="pendingAddToOneMapOld"
			@close="onOneMapPanelClosed"
			@preview-changed="onOneMapPreviewChanged"
			@approved="onOneMapApproved"
			@proposal-ready="onOneMapProposalReady"
			@ack-add-to-old-set="onAckAddToOldSet"
		/>
		<div class="stage">
			<div class="pane" v-show="mode === '2d'">
				<BoundaryDrawTool
					ref="boundaryToolRef"
					:footprints="footprints"
					:removed-ids="removedIds"
					:mode="addHypotheticalActive ? 'add-hypothetical' : 'boundary'"
					:place-mesh-request="pendingMeshPlacement"
					@ring-changed="onRingChanged"
					@selection-changed="onSelectionChanged"
					@building-added="onBuildingAdded"
					@mesh-placement-done="onMeshPlacementDone"
					@mode-cancelled="onAddHypotheticalCancelled"
				/>
			</div>
			<div class="pane" v-show="mode === '3d'" v-if="has3DBeenActivated">
				<SbgViewer3D
					ref="viewer3dRef"
					:selected-objid="selectedObjid"
					:boundary-ring="boundaryRing"
					:highlight-footprint-rings="highlightFootprintRings"
					:removed-footprint-rings="removedFootprintRings"
					:added-footprints="addedFootprints"
					:last-undone="lastUndone"
					:review-old-footprint-rings="reviewOldFootprintRings"
					:review-citymodel="reviewCitymodel"
					:one-map-proposal-active="activeOneMapProposal !== null"
					:removed-ids="removedIds"
					:review-old-building-ids="activeOneMapProposal?.oldBuildingIds ?? []"
					:show-island-terrain="showIslandTerrain"
					:edited-ids="editedIds"
					@loaded="on3dLoaded"
					@object_clicked="onObjectClicked"
					@building_removed="onBuildingRemoved"
					@attributes_edited="onAttributesEdited"
					@replace_with_onemap="onReplaceWithOneMap"
					@add_to_onemap_replacement="onAddToOneMapReplacement"
					@height_edited="onHeightEdited"
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
.unsaved-badge {
	color: #ffb347;
	white-space: nowrap;
}
.save-note {
	width: 100%;
	box-sizing: border-box;
	padding: 3px 6px;
	background: #0b1220;
	color: #ddd;
	border: 1px solid #3a4358;
	border-radius: 4px;
	font-family: monospace;
	font-size: 12px;
}
.save-error {
	color: #ff8080;
	font-size: 11px;
}
/* Toolbar redesign: View/Edit/Add all collapse behind their own "X ▾"
   dropdown now (see project plan, toolbar-redesign section) -- one shared
   .menu/.menu-dropdown style instead of the old Add-Building-only one. */
.menu {
	position: relative;
}
.menu-dropdown {
	position: absolute;
	top: 100%;
	left: 0;
	margin-top: 4px;
	display: flex;
	flex-direction: column;
	gap: 4px;
	background: #1a2030;
	border: 1px solid #3a4358;
	border-radius: 4px;
	padding: 4px;
	z-index: 30;
}
.menu-dropdown > button {
	text-align: left;
	white-space: nowrap;
}
.edit-menu-dropdown {
	min-width: 180px;
}
.menu-row {
	display: flex;
	align-items: center;
	gap: 6px;
}
.menu-row button {
	flex-shrink: 0;
}
.menu-badge {
	margin-left: 4px;
	color: #ffb347;
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
