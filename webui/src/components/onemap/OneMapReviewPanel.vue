<script setup>
// Phase 4c (OneMap review-gate): the real implementation of
// onemap_landmarks/NOTES.md's propose -> numeric sanity check -> contextual
// 3D preview -> manual nudge -> explicit approve workflow. Nothing here
// mutates the working dataset until Approve -- Reject/Needs Adjustment just
// discard the in-memory proposal server-side (see sbg/ui/onemap_review.py).
//
// context: either { buildingId } (an existing SBG footprint -- point-in-
// polygon containment surfaces every real OneMap building it represents,
// "Replace with OneMap...") or {} (no existing footprint -- "Add from
// OneMap...", starts with a name search instead of automatic discovery).
//
// Redesign after real use: the original "click somewhere / view-center"
// discovery for the no-footprint case was dropped entirely -- direct user
// feedback ("if you know a building's shape is wrong or missing, you
// already know its name") -- replaced with a plain search box reusing the
// SAME /api/onemap/search endpoint LocationSearchBox.vue already calls.
// Also: this is no longer a modal (see below), and now supports pooling
// more than one existing building into a single replacement's "old" side.
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue';

const props = defineProps({
	context: { type: Object, required: true },
	// Set by App.vue (from the 3D info-panel's "Add to current OneMap
	// replacement" action) to request pooling another existing building
	// into this review's old-building side. Same prop-driven handoff
	// pattern already used for Phase 4b's placeMeshRequest.
	pendingAddToOldSet: { type: String, default: null },
});
const emit = defineEmits(['close', 'preview-changed', 'approved', 'proposal-ready', 'ack-add-to-old-set']);

const entryMode = props.context.buildingId != null ? 'replace' : 'add';

// Pooled old-building ids -- starts with the single selected building for
// "Replace", empty for "Add" (nothing to replace, just adding a real
// OneMap building that has no SBG footprint at all).
const oldBuildingIds = ref(props.context.buildingId != null ? [props.context.buildingId] : []);

const candidates = ref([]);
const loadingCandidates = ref(true);
const candidatesError = ref(null);
const selected = reactive({}); // gml_id -> bool, which candidates to fetch

async function fetchCandidatesForBuilding() {
	loadingCandidates.value = true;
	candidatesError.value = null;
	try {
		const params = new URLSearchParams({ building_id: props.context.buildingId });
		const res = await fetch(`/api/onemap/candidates?${params}`);
		const data = await res.json();
		if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
		candidates.value = data.candidates;
		for (const c of data.candidates) selected[c.gml_id] = true; // default: fetch all found
	} catch (err) {
		candidatesError.value = err.message;
	} finally {
		loadingCandidates.value = false;
	}
}
if (entryMode === 'replace') onMounted(fetchCandidatesForBuilding);
else loadingCandidates.value = false; // 'add' mode starts at the search box, not a loading state

// --- 'add' mode: search by name, then resolve to real OneMap 3D-tile candidates ---
const searchQuery = ref('');
const searching = ref(false);
const searchResults = ref([]);
const searchError = ref(null);
const pickedLocation = ref(null); // {name, address, lat, lng} once a result is picked

async function runSearch() {
	if (!searchQuery.value.trim() || searching.value) return;
	searching.value = true;
	searchError.value = null;
	searchResults.value = [];
	try {
		const res = await fetch(`/api/onemap/search?q=${encodeURIComponent(searchQuery.value)}`);
		const data = await res.json();
		if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
		searchResults.value = data.results;
		if (data.results.length === 0) searchError.value = 'No results for that search.';
	} catch (err) {
		searchError.value = err.message;
	} finally {
		searching.value = false;
	}
}

// A search-API hit and a 3D-tile-crawl point for the same real building
// won't always sit within a tight radius of each other -- try
// progressively wider radii rather than picking one guess, and only report
// "not in OneMap's 3D coverage" (a real, expected case per Phase 1.5's own
// findings) after the widest attempt genuinely comes up empty.
const SEARCH_RADII_M = [30, 100, 300];

async function pickSearchResult(r) {
	pickedLocation.value = r;
	loadingCandidates.value = true;
	candidatesError.value = null;
	candidates.value = [];
	try {
		for (const radius of SEARCH_RADII_M) {
			const params = new URLSearchParams({ lat: r.lat, lng: r.lng, radius_m: radius });
			const res = await fetch(`/api/onemap/candidates?${params}`);
			const data = await res.json();
			if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
			if (data.candidates.length > 0) {
				candidates.value = data.candidates;
				for (const c of data.candidates) selected[c.gml_id] = true;
				break;
			}
		}
		if (candidates.value.length === 0) {
			candidatesError.value = `OneMap has no 3D building data within ${SEARCH_RADII_M[SEARCH_RADII_M.length - 1]}m of "${r.name}" -- it may genuinely not be covered.`;
		}
	} catch (err) {
		candidatesError.value = err.message;
	} finally {
		loadingCandidates.value = false;
	}
}

function backToSearch() {
	pickedLocation.value = null;
	candidates.value = [];
	candidatesError.value = null;
}

const proposing = ref(false);
const proposeError = ref(null);
const jobStatus = ref(null); // {status, stage, log}
const proposal = ref(null); // job.result once done: {proposal_id, old_building_ids, old_footprint_rings, candidates}
let pollTimer = null;

const nudge = reactive({}); // gml_id -> {dx, dy, dz, rotation_deg}
const nudging = reactive({}); // gml_id -> bool, in-flight
const approveSelected = reactive({}); // gml_id -> bool, which to actually embed

async function proposeSelected() {
	const chosen = candidates.value.filter((c) => selected[c.gml_id]);
	if (chosen.length === 0 || proposing.value) return;
	proposing.value = true;
	proposeError.value = null;
	proposal.value = null;
	try {
		const res = await fetch('/api/onemap/propose', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ old_building_ids: oldBuildingIds.value, candidates: chosen }),
		});
		const data = await res.json();
		if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
		jobStatus.value = { status: 'pending' };
		pollJob(data.job_id);
	} catch (err) {
		proposeError.value = err.message;
		proposing.value = false;
	}
}

function pollJob(jobId) {
	if (pollTimer) clearInterval(pollTimer);
	pollTimer = setInterval(async () => {
		try {
			const res = await fetch(`/api/onemap/jobs/${jobId}`);
			if (!res.ok) return;
			const data = await res.json();
			jobStatus.value = data;
			if (data.status === 'done') {
				clearInterval(pollTimer);
				pollTimer = null;
				proposing.value = false;
				proposal.value = data.result;
				oldBuildingIds.value = data.result.old_building_ids;
				for (const c of data.result.candidates) {
					if (c.status !== 'ok') continue;
					nudge[c.gml_id] = { dx: 0, dy: 0, dz: 0, rotation_deg: 0 };
					// Default approve-selection to candidates with no sanity
					// flags -- a flagged one still shows up for review/nudge,
					// it just doesn't get silently pre-checked for embedding.
					approveSelected[c.gml_id] = c.sanity.flags.length === 0;
				}
				emit('proposal-ready', { proposalId: data.result.proposal_id, oldBuildingIds: data.result.old_building_ids });
				updatePreview();
			} else if (data.status === 'error') {
				clearInterval(pollTimer);
				pollTimer = null;
				proposing.value = false;
				proposeError.value = data.error;
			}
		} catch {
			// transient fetch failure -- next tick retries
		}
	}, 1500);
}
onUnmounted(() => { if (pollTimer) clearInterval(pollTimer); });

const okCandidates = computed(() => (proposal.value?.candidates ?? []).filter((c) => c.status === 'ok'));

async function applyNudge(gmlId) {
	if (nudging[gmlId]) return;
	nudging[gmlId] = true;
	try {
		const n = nudge[gmlId];
		const res = await fetch(`/api/onemap/proposals/${proposal.value.proposal_id}/nudge`, {
			method: 'PATCH',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ gml_id: gmlId, ...n }),
		});
		const data = await res.json();
		if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
		const c = proposal.value.candidates.find((c) => c.gml_id === gmlId);
		if (c) c.sanity = data.sanity;
		await updatePreview();
	} catch (err) {
		proposeError.value = err.message;
	} finally {
		nudging[gmlId] = false;
	}
}

async function updatePreview() {
	if (!proposal.value) return;
	const gmlIds = okCandidates.value.map((c) => c.gml_id);
	if (gmlIds.length === 0) {
		emit('preview-changed', { oldFootprintRings: proposal.value.old_footprint_rings, citymodel: null });
		return;
	}
	try {
		const res = await fetch(`/api/onemap/proposals/${proposal.value.proposal_id}/geometry?gml_ids=${gmlIds.map(encodeURIComponent).join(',')}`);
		if (!res.ok) return;
		const citymodel = await res.json();
		emit('preview-changed', { oldFootprintRings: proposal.value.old_footprint_rings, citymodel });
	} catch {
		// leave the previous preview showing -- a manual retry (another nudge) will refresh it
	}
}

// Pooling: widen (or narrow) the old-building side of an ALREADY-PROPOSED
// replacement without re-fetching any mesh -- see
// sbg/ui/onemap_review.py::update_old_buildings's own docstring. Triggered
// either by App.vue setting pendingAddToOldSet (a building clicked in 3D
// while this panel is open, see the 3D info-panel's new action) or by the
// "Replacing: ... [x]" row's own remove buttons below.
watch(() => props.pendingAddToOldSet, async (id) => {
	if (!id) return;
	if (!oldBuildingIds.value.includes(id)) {
		oldBuildingIds.value = [...oldBuildingIds.value, id];
		if (proposal.value) await refreshOldBuildings();
	}
	emit('ack-add-to-old-set');
});

async function removeOldBuilding(id) {
	oldBuildingIds.value = oldBuildingIds.value.filter((x) => x !== id);
	if (proposal.value) await refreshOldBuildings();
}

async function refreshOldBuildings() {
	try {
		const res = await fetch(`/api/onemap/proposals/${proposal.value.proposal_id}/old-buildings`, {
			method: 'PATCH',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ old_building_ids: oldBuildingIds.value }),
		});
		const data = await res.json();
		if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
		proposal.value.old_building_ids = data.old_building_ids;
		proposal.value.old_footprint_rings = data.old_footprint_rings;
		for (const upd of data.candidates) {
			const c = proposal.value.candidates.find((c) => c.gml_id === upd.gml_id);
			if (c) c.sanity = upd.sanity;
		}
		// Re-emit (not just on the initial propose) -- App.vue's real
		// per-object dim mechanism (ThreeJsViewer.vue's updateDimState)
		// needs the CURRENT pooled old-building set, not just whatever was
		// there at propose time, or a pooled-in building would keep
		// rendering fully opaque despite showing in the "Replacing" row.
		emit('proposal-ready', { proposalId: proposal.value.proposal_id, oldBuildingIds: data.old_building_ids });
		await updatePreview(); // re-emits with the refreshed old_footprint_rings
	} catch (err) {
		proposeError.value = err.message;
	}
}

const approving = ref(false);
const approveError = ref(null);
const decisionNote = ref('');

async function decide(decision) {
	if (approving.value || !proposal.value) return;
	approving.value = true;
	approveError.value = null;
	try {
		const gmlIds = Object.keys(approveSelected).filter((g) => approveSelected[g]);
		if (decision === 'approve' && gmlIds.length === 0) {
			throw new Error('Select at least one candidate to approve');
		}
		const res = await fetch('/api/onemap/approve', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({
				proposal_id: proposal.value.proposal_id, decision,
				gml_ids: decision === 'approve' ? gmlIds : [],
				note: decisionNote.value || null,
			}),
		});
		const data = await res.json();
		if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
		if (decision === 'approve') {
			emit('approved', {
				oldBuildingIds: data.old_building_ids,
				newFootprints: data.new_footprints,
				session: data.session,
			});
		}
		emit('close');
	} catch (err) {
		approveError.value = err.message;
	} finally {
		approving.value = false;
	}
}

function flagLabel(flag) {
	return {
		centroid_offset_high: 'position offset',
		area_ratio_off: 'footprint size mismatch',
		height_ratio_off: 'height mismatch',
	}[flag] || flag;
}

// Direct user complaint: the "position offset" flag was a plain, inert
// label -- "what does the position offset button even do, it just says
// position offset but doesn't do anything." sanity_check now returns a
// SIGNED offset vector (ref - mesh centroid, computed against wherever the
// mesh currently sits, i.e. post any prior nudge), so closing the gap is
// just adding that delta to the cumulative nudge and re-applying -- exactly
// what "at least make the button nudge it slightly closer" asked for.
async function autoNudgeToClosePosition(c) {
	const dx = c.sanity.centroid_offset_dx, dy = c.sanity.centroid_offset_dy;
	if (dx == null || dy == null || nudging[c.gml_id]) return;
	nudge[c.gml_id].dx = Math.round((nudge[c.gml_id].dx + dx) * 100) / 100;
	nudge[c.gml_id].dy = Math.round((nudge[c.gml_id].dy + dy) * 100) / 100;
	await applyNudge(c.gml_id);
}
</script>

<template>
	<div class="onemap-panel">
		<div class="onemap-panel-header">
			<span>OneMap {{ entryMode === 'replace' ? 'Replace' : 'Add' }}</span>
			<button class="close-btn" @click="emit('close')">×</button>
		</div>

		<div v-if="oldBuildingIds.length > 0" class="old-buildings-row">
			<span class="label">Replacing:</span>
			<span v-for="id in oldBuildingIds" :key="id" class="old-building-chip">
				{{ id }}
				<button class="chip-remove" title="Remove from this replacement" @click="removeOldBuilding(id)">×</button>
			</span>
		</div>

		<template v-if="entryMode === 'add' && !pickedLocation">
			<div class="hint">Search for the real building by name (e.g. "Art Science Museum") -- OneMap's own search, same as the location search box.</div>
			<div class="search-row">
				<input v-model="searchQuery" placeholder="Search a building name…" @keyup.enter="runSearch" />
				<button :disabled="searching" @click="runSearch">{{ searching ? '…' : 'Search' }}</button>
			</div>
			<div v-if="searchError" class="error">{{ searchError }}</div>
			<div v-if="searchResults.length" class="search-results">
				<div v-for="r in searchResults" :key="r.name + r.postal" class="search-result-row" @click="pickSearchResult(r)">
					<div class="sr-name">{{ r.name }}</div>
					<div class="sr-address">{{ r.address }}</div>
				</div>
			</div>
		</template>

		<template v-else>
			<button v-if="entryMode === 'add'" class="back-btn" @click="backToSearch">‹ Back to search</button>

			<div v-if="loadingCandidates" class="hint">{{ entryMode === 'add' ? 'Looking for OneMap 3D coverage near ' + pickedLocation.name + '…' : 'Searching cached OneMap data…' }}</div>
			<div v-else-if="candidatesError" class="error">{{ candidatesError }}</div>
			<div v-else-if="candidates.length === 0" class="hint">No real OneMap buildings found.</div>

			<template v-else-if="!proposal">
				<div class="hint">{{ candidates.length }} real OneMap building(s) found -- pick which to fetch and review:</div>
				<div class="candidate-pick-list">
					<label v-for="c in candidates" :key="c.gml_id" class="candidate-pick-row">
						<input type="checkbox" v-model="selected[c.gml_id]" />
						<span class="cname">{{ c.name || '(unnamed)' }}</span>
						<span class="cmeta">{{ c.height ? `${c.height}m` : '?' }} · {{ c.distance_m.toFixed(0) }}m away</span>
					</label>
				</div>
				<button class="propose-btn" :disabled="proposing" @click="proposeSelected">
					{{ proposing ? 'Fetching…' : 'Fetch selected & review' }}
				</button>
				<div v-if="proposeError" class="error">{{ proposeError }}</div>
				<div v-if="proposing && jobStatus" class="hint job-log">
					<div v-for="(line, i) in jobStatus.log || []" :key="i">{{ line }}</div>
				</div>
			</template>

			<template v-else>
				<div class="hint">Review the fetched mesh(es) below -- flagged numbers are advisory, not blocking. Click another building in the 3D view to pool it into "Replacing" above.</div>
				<div v-for="c in proposal.candidates" :key="c.gml_id" class="candidate-result">
					<div class="cr-header">{{ c.name || c.gml_id }}</div>
					<div v-if="c.status === 'failed'" class="error">Failed after {{ c.attempted_tiles }} tile(s): {{ c.error }}</div>
					<template v-else>
						<div class="sanity-grid">
							<span>footprint area</span><span>{{ c.sanity.old_area_m2 ?? '—' }} → {{ c.sanity.mesh_area_m2 }} m² (×{{ c.sanity.area_ratio ?? '—' }})</span>
							<span>height</span><span>{{ c.sanity.old_height_m ?? '—' }} → {{ c.sanity.mesh_height_m }} m (×{{ c.sanity.height_ratio ?? '—' }})</span>
							<span>position offset</span><span>{{ c.sanity.centroid_offset_m ?? '—' }} m</span>
						</div>
						<div v-if="c.sanity.flags.length" class="flags">
							<button
								v-for="f in c.sanity.flags" :key="f"
								class="flag-badge"
								:class="{ 'flag-actionable': f === 'centroid_offset_high' }"
								:disabled="f !== 'centroid_offset_high' || nudging[c.gml_id]"
								:title="f === 'centroid_offset_high' ? 'Click to nudge this mesh to close the position gap' : ''"
								@click="f === 'centroid_offset_high' && autoNudgeToClosePosition(c)"
							>{{ flagLabel(f) }}<span v-if="f === 'centroid_offset_high'"> → nudge closer</span></button>
						</div>
						<div class="nudge-row">
							<span class="label">nudge (m/deg):</span>
							<input type="number" v-model.number="nudge[c.gml_id].dx" placeholder="dx" step="any" />
							<input type="number" v-model.number="nudge[c.gml_id].dy" placeholder="dy" step="any" />
							<input type="number" v-model.number="nudge[c.gml_id].dz" placeholder="dz" step="any" />
							<input type="number" v-model.number="nudge[c.gml_id].rotation_deg" placeholder="rot°" step="any" />
							<button :disabled="nudging[c.gml_id]" @click="applyNudge(c.gml_id)">
								{{ nudging[c.gml_id] ? 'Applying…' : 'Apply' }}
							</button>
						</div>
						<label class="approve-check">
							<input type="checkbox" v-model="approveSelected[c.gml_id]" />
							Include in approval
						</label>
					</template>
				</div>

				<input v-model="decisionNote" class="note-input" type="text" placeholder="note (optional)" />
				<div class="decision-row">
					<button class="approve-btn" :disabled="approving" @click="decide('approve')">
						{{ approving ? 'Applying…' : 'Approve selected' }}
					</button>
					<button :disabled="approving" @click="decide('needs_adjustment')">Needs adjustment</button>
					<button :disabled="approving" @click="decide('reject')">Reject</button>
				</div>
				<div v-if="approveError" class="error">{{ approveError }}</div>
			</template>
		</template>
	</div>
</template>

<style scoped>
/* Docked panel, NOT a full-screen modal -- an earlier version used a
   position:fixed; inset:0 backdrop with @click.self to close, which (real
   user report) intercepted every pointer event over the 3D canvas too,
   making it impossible to orbit/pan while reviewing a proposed placement.
   Matches VersionHistoryPanel.vue's own already-proven docked-panel
   pattern exactly -- position:absolute, sized to content, closes only via
   the explicit × button. */
.onemap-panel {
	position: absolute;
	top: 44px;
	right: 8px;
	z-index: 30;
	width: 380px;
	max-height: calc(100vh - 60px);
	overflow-y: auto;
	background: rgba(11, 18, 32, 0.98);
	color: #ddd;
	border: 1px solid #3a4358;
	border-radius: 6px;
	padding: 12px 14px;
	font-family: monospace;
	font-size: 12px;
	display: flex;
	flex-direction: column;
	gap: 10px;
}
.onemap-panel-header {
	display: flex;
	justify-content: space-between;
	align-items: center;
	font-weight: bold;
	color: #4a9eff;
	word-break: break-all;
}
.close-btn {
	cursor: pointer;
	background: none;
	border: none;
	color: #ddd;
	font-size: 16px;
	line-height: 1;
	flex-shrink: 0;
}
.hint {
	color: #8a97ad;
}
.error {
	color: #ff8080;
}
.job-log {
	max-height: 120px;
	overflow-y: auto;
	white-space: pre-wrap;
	font-size: 11px;
}
.old-buildings-row {
	display: flex;
	flex-wrap: wrap;
	align-items: center;
	gap: 4px;
	padding-bottom: 8px;
	border-bottom: 1px solid #262e42;
}
.old-buildings-row .label {
	color: #8a97ad;
}
.old-building-chip {
	display: inline-flex;
	align-items: center;
	gap: 4px;
	background: #2a1f1f;
	color: #ffa07a;
	border: 1px solid #5a3a2e;
	border-radius: 3px;
	padding: 1px 4px 1px 6px;
	font-size: 11px;
}
.chip-remove {
	cursor: pointer;
	background: none;
	border: none;
	color: inherit;
	font-size: 13px;
	line-height: 1;
	padding: 0;
}
.search-row {
	display: flex;
	gap: 6px;
}
.search-row input {
	flex: 1;
	padding: 4px 6px;
	background: #0b1220;
	color: #ddd;
	border: 1px solid #3a4358;
	border-radius: 4px;
	font-family: monospace;
	font-size: 12px;
}
.search-row button {
	cursor: pointer;
	padding: 4px 10px;
	background: #262e42;
	color: #ddd;
	border: 1px solid #3a4358;
	border-radius: 4px;
	font-family: monospace;
	font-size: 12px;
}
.search-results {
	display: flex;
	flex-direction: column;
	gap: 2px;
	max-height: 200px;
	overflow-y: auto;
}
.search-result-row {
	cursor: pointer;
	padding: 4px 6px;
	border-radius: 3px;
}
.search-result-row:hover {
	background: #1a2338;
}
.sr-name {
	color: #ddd;
}
.sr-address {
	color: #8a97ad;
	font-size: 11px;
}
.back-btn {
	align-self: flex-start;
	cursor: pointer;
	background: none;
	border: none;
	color: #4a9eff;
	font-family: monospace;
	font-size: 11px;
	padding: 0;
}
.candidate-pick-list {
	display: flex;
	flex-direction: column;
	gap: 4px;
	max-height: 200px;
	overflow-y: auto;
}
.candidate-pick-row {
	display: flex;
	align-items: center;
	gap: 6px;
	cursor: pointer;
}
.candidate-pick-row .cname {
	flex: 1;
	overflow: hidden;
	text-overflow: ellipsis;
	white-space: nowrap;
}
.candidate-pick-row .cmeta {
	color: #8a97ad;
	white-space: nowrap;
}
.propose-btn {
	cursor: pointer;
	padding: 5px 10px;
	background: #262e42;
	color: #ddd;
	border: 1px solid #3a4358;
	border-radius: 4px;
	font-family: monospace;
	font-size: 12px;
}
.propose-btn:disabled {
	opacity: 0.6;
	cursor: default;
}
.candidate-result {
	border-top: 1px solid #262e42;
	padding-top: 8px;
}
.cr-header {
	font-weight: bold;
	color: #9fd4ff;
	margin-bottom: 4px;
}
.sanity-grid {
	display: grid;
	grid-template-columns: auto 1fr;
	gap: 2px 8px;
	font-size: 11px;
}
.sanity-grid span:nth-child(odd) {
	color: #8a97ad;
}
.flags {
	margin-top: 4px;
	display: flex;
	flex-wrap: wrap;
	gap: 4px;
}
.flag-badge {
	background: #4a2f1f;
	color: #ffb347;
	border: 1px solid #7a5a2e;
	border-radius: 3px;
	padding: 1px 6px;
	font-size: 10px;
	font-family: monospace;
	cursor: default;
}
.flag-badge:disabled {
	opacity: 1; /* non-actionable flags are inert labels, not "disabled" controls -- keep full-strength color */
}
.flag-actionable {
	cursor: pointer;
	border-color: #ffb347;
}
.flag-actionable:hover:not(:disabled) {
	background: #5a3a24;
}
.nudge-row {
	margin-top: 6px;
	display: flex;
	align-items: center;
	gap: 4px;
	flex-wrap: wrap;
}
.nudge-row .label {
	color: #8a97ad;
	font-size: 11px;
}
.nudge-row input {
	width: 55px;
	background: #0b1220;
	color: #ddd;
	border: 1px solid #3a4358;
	border-radius: 3px;
	padding: 2px 4px;
	font-family: monospace;
	font-size: 11px;
}
.nudge-row button {
	cursor: pointer;
	padding: 2px 8px;
	background: #262e42;
	color: #ddd;
	border: 1px solid #3a4358;
	border-radius: 4px;
	font-family: monospace;
	font-size: 11px;
}
.approve-check {
	display: flex;
	align-items: center;
	gap: 6px;
	margin-top: 6px;
	cursor: pointer;
}
.note-input {
	padding: 4px 6px;
	background: #0b1220;
	color: #ddd;
	border: 1px solid #3a4358;
	border-radius: 4px;
	font-family: monospace;
	font-size: 12px;
}
.decision-row {
	display: flex;
	gap: 6px;
}
.decision-row button {
	flex: 1;
	cursor: pointer;
	padding: 5px 10px;
	background: #262e42;
	color: #ddd;
	border: 1px solid #3a4358;
	border-radius: 4px;
	font-family: monospace;
	font-size: 12px;
}
.decision-row button:disabled {
	opacity: 0.6;
	cursor: default;
}
.approve-btn {
	background: #1f3a2e !important;
	color: #7cfc9a !important;
	border-color: #2e7a4a !important;
}
</style>
