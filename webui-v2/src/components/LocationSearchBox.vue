<script setup>
// Shared by both the 3D and 2D panels (drives App.vue's `center` state), so
// kept at the top level rather than under plan2d/ despite the project plan's
// original file-list sketch grouping it there -- it isn't a 2D-canvas-only
// concern. No basemap tiles in v1 (see plan); this + the coordinate-entry
// fallback below are the two precise-navigation features that replace one.
import { ref } from 'vue';
import proj4 from 'proj4';

const emit = defineEmits(['goto']); // ({x, y}) in EPSG:3414

// Singapore's official projected grid -- matches pyproj's EPSG:3414 used
// throughout the sbg/ pipeline (same tmerc parameters SVY21 is defined by).
const SVY21 = '+proj=tmerc +lat_0=1.366666666666667 +lon_0=103.8333333333333 +k=1 +x_0=28001.642 +y_0=38744.572 +ellps=WGS84 +units=m +no_defs';

const query = ref('');
const results = ref([]);
const searching = ref(false);
const error = ref(null);

const coordMode = ref('svy21'); // 'svy21' | 'latlng'
const coordA = ref('');
const coordB = ref('');

async function runSearch() {
	if (!query.value.trim()) return;
	searching.value = true;
	error.value = null;
	try {
		const res = await fetch(`/api/onemap/search?q=${encodeURIComponent(query.value)}`);
		if (!res.ok) throw new Error(`search failed: ${res.status}`);
		const data = await res.json();
		results.value = data.results;
		if (data.results.length === 0) error.value = 'no results';
	} catch (err) {
		error.value = err.message;
	} finally {
		searching.value = false;
	}
}

function pickResult(r) {
	const [x, y] = proj4('WGS84', SVY21, [r.lng, r.lat]);
	results.value = [];
	query.value = r.name;
	emit('goto', { x, y });
}

function goToCoords() {
	const a = parseFloat(coordA.value);
	const b = parseFloat(coordB.value);
	if (Number.isNaN(a) || Number.isNaN(b)) {
		error.value = 'enter numeric coordinates';
		return;
	}
	error.value = null;
	if (coordMode.value === 'latlng') {
		const [x, y] = proj4('WGS84', SVY21, [b, a]); // proj4 wants (lng, lat); form is (lat, lng)
		emit('goto', { x, y });
	} else {
		emit('goto', { x: a, y: b });
	}
}
</script>

<template>
	<div class="location-search">
		<div class="search-row">
			<input v-model="query" placeholder="Search a place, address, or postal code" @keyup.enter="runSearch" />
			<button @click="runSearch" :disabled="searching">{{ searching ? '…' : 'Search' }}</button>
			<ul v-if="results.length" class="results">
				<li v-for="r in results" :key="r.name + r.postal" @click="pickResult(r)">
					<strong>{{ r.name }}</strong><br /><small>{{ r.address }}</small>
				</li>
			</ul>
		</div>
		<select v-model="coordMode">
			<option value="svy21">EPSG:3414 (x, y)</option>
			<option value="latlng">Lat, Lng</option>
		</select>
		<input v-model="coordA" :placeholder="coordMode === 'svy21' ? 'x' : 'lat'" size="8" />
		<input v-model="coordB" :placeholder="coordMode === 'svy21' ? 'y' : 'lng'" size="8" />
		<button @click="goToCoords">Go</button>
		<span v-if="error" class="error">{{ error }}</span>
	</div>
</template>

<style scoped>
.location-search {
	display: flex;
	align-items: center;
	gap: 6px;
	font-family: monospace;
	font-size: 12px;
	color: #ddd;
}
.search-row {
	position: relative;
	display: flex;
	gap: 6px;
}
input,
select {
	font-family: monospace;
	font-size: 12px;
	padding: 3px 5px;
}
button {
	cursor: pointer;
}
.results {
	position: absolute;
	top: 100%;
	left: 0;
	margin-top: 4px;
	list-style: none;
	padding: 0;
	width: 320px;
	max-height: 160px;
	overflow-y: auto;
	z-index: 30;
	background: #10141f;
	border-radius: 4px;
}
.results li {
	padding: 4px 6px;
	cursor: pointer;
	border-bottom: 1px solid #232a3a;
}
.results li:hover {
	background: #1c2436;
}
.error {
	color: #ff6b6b;
}
</style>
