<script setup>
// Phase 4b, Paths 2 & 3 of "Add Building": import a real building from an
// uploaded file. Path 2 (CityJSON) needs no placement UI at all -- the
// file already carries real EPSG:3414 coordinates, so it's a plain
// file-picker + submit. Path 3 (mesh: STL/OBJ) offers two ways to place
// it: "Use coordinates as-is" (zero offset -- correct for a file already
// exported in real coordinates, e.g. onemap_landmarks/exports/*.obj,
// confirmed directly by inspecting one: vertex range ~32844-33106 x,
// ~31286-31537 y, matching real SVY21 magnitudes) or "Place manually",
// which hands off to the interactive ghost-preview placement flow in 3D
// (see ThreeJsViewer.vue's placeMeshFile prop) rather than doing anything
// itself -- this panel's job ends the moment placement starts.
import { ref } from 'vue';

const props = defineProps({
	path: { type: String, required: true }, // 'cityjson' | 'mesh'
});

const emit = defineEmits(['close', 'building-added', 'start-mesh-placement']);

const file = ref(null);
const note = ref('');
const importing = ref(false);
const error = ref(null);

function onFileChange(e) {
	file.value = e.target.files?.[0] ?? null;
	error.value = null;
}

async function importCityJson() {
	if (!file.value || importing.value) return;
	importing.value = true;
	error.value = null;
	try {
		const form = new FormData();
		form.append('file', file.value);
		const res = await fetch('/api/buildings/import-cityjson', { method: 'POST', body: form });
		const data = await res.json();
		if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
		emit('building-added', data);
	} catch (err) {
		error.value = err.message;
	} finally {
		importing.value = false;
	}
}

async function importMeshAsIs() {
	if (!file.value || importing.value) return;
	importing.value = true;
	error.value = null;
	try {
		const form = new FormData();
		form.append('file', file.value);
		if (note.value) form.append('attributes', JSON.stringify({ note: note.value }));
		const res = await fetch('/api/buildings/import-mesh', { method: 'POST', body: form });
		const data = await res.json();
		if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
		emit('building-added', data);
	} catch (err) {
		error.value = err.message;
	} finally {
		importing.value = false;
	}
}

function placeManually() {
	if (!file.value) return;
	emit('start-mesh-placement', { file: file.value, note: note.value });
}
</script>

<template>
	<div class="import-panel-backdrop" @click.self="emit('close')">
		<div class="import-panel">
			<div class="import-panel-header">
				<span>{{ path === 'cityjson' ? 'Import CityJSON' : 'Import mesh (STL / OBJ)' }}</span>
				<button class="close-btn" @click="emit('close')">×</button>
			</div>

			<input
				type="file"
				:accept="path === 'cityjson' ? '.json,.city.json' : '.stl,.obj'"
				@change="onFileChange"
			/>

			<input v-if="path === 'mesh'" v-model="note" type="text" placeholder="note (optional)" class="note-input" />

			<div v-if="path === 'cityjson'" class="import-actions">
				<button :disabled="!file || importing" @click="importCityJson">
					{{ importing ? 'Importing…' : 'Import' }}
				</button>
			</div>
			<div v-else class="import-actions">
				<button :disabled="!file || importing" @click="importMeshAsIs">
					{{ importing ? 'Importing…' : 'Use coordinates as-is' }}
				</button>
				<button :disabled="!file || importing" @click="placeManually">Place manually…</button>
			</div>

			<div v-if="error" class="import-error">{{ error }}</div>
		</div>
	</div>
</template>

<style scoped>
.import-panel-backdrop {
	position: fixed;
	inset: 0;
	z-index: 40;
	background: rgba(0, 0, 0, 0.5);
	display: flex;
	align-items: center;
	justify-content: center;
}
.import-panel {
	width: 360px;
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
.import-panel-header {
	display: flex;
	justify-content: space-between;
	align-items: center;
	font-weight: bold;
	color: #4a9eff;
}
.close-btn {
	cursor: pointer;
	background: none;
	border: none;
	color: #ddd;
	font-size: 16px;
	line-height: 1;
}
.import-panel input[type='file'] {
	color: #ddd;
	font-family: monospace;
	font-size: 12px;
}
.note-input {
	padding: 3px 6px;
	background: #0b1220;
	color: #ddd;
	border: 1px solid #3a4358;
	border-radius: 4px;
	font-family: monospace;
	font-size: 12px;
}
.import-actions {
	display: flex;
	gap: 6px;
}
.import-actions button {
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
.import-actions button:disabled {
	opacity: 0.6;
	cursor: default;
}
.import-error {
	color: #ff8080;
}
</style>
