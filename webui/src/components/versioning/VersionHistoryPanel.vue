<script setup>
// Plain presentational-ish component modeled on JobProgressPanel.vue's
// established style (small, owns its own network calls per this app's
// convention -- see BoundaryDrawTool.vue's own comment on that pattern).
// Fetches /api/versions on open; each row's "Restore" button hits
// /api/versions/{hash}/restore then emits 'restored' upward -- App.vue
// reloads the page on that event rather than trying to hand-reconcile 2D/3D
// state after what's a genuinely large, rare, explicit change (see
// App.vue's own comment on why).
import { onMounted, ref } from 'vue';

const emit = defineEmits(['close', 'restored']);

const versions = ref([]);
const loading = ref(true);
const error = ref(null);
const restoringHash = ref(null);

async function load() {
	loading.value = true;
	error.value = null;
	try {
		const res = await fetch('/api/versions');
		if (!res.ok) throw new Error(`HTTP ${res.status}`);
		versions.value = await res.json();
	} catch (err) {
		error.value = err.message;
	} finally {
		loading.value = false;
	}
}

async function restore(hash) {
	if (restoringHash.value) return;
	if (!confirm('Restore to this version? Any unsaved changes will be lost.')) return;
	restoringHash.value = hash;
	error.value = null;
	try {
		const res = await fetch(`/api/versions/${hash}/restore`, { method: 'POST' });
		const data = await res.json();
		if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
		emit('restored', data);
	} catch (err) {
		error.value = err.message;
		restoringHash.value = null;
	}
}

function formatDate(iso) {
	return new Date(iso).toLocaleString();
}

onMounted(load);
</script>

<template>
	<div class="version-history-panel">
		<div class="header">
			<span>Version History</span>
			<button class="close-btn" @click="emit('close')">×</button>
		</div>
		<div v-if="loading" class="empty">loading…</div>
		<div v-else-if="error" class="error">{{ error }}</div>
		<div v-else-if="versions.length === 0" class="empty">No saved versions yet.</div>
		<div v-else class="rows">
			<div v-for="v in versions" :key="v.hash" class="row">
				<div class="row-main">
					<div class="subject">{{ v.subject }}</div>
					<div class="date">{{ formatDate(v.date) }} — {{ v.hash.slice(0, 8) }}</div>
					<pre v-if="v.body" class="body">{{ v.body }}</pre>
				</div>
				<button class="restore-btn" :disabled="restoringHash === v.hash" @click="restore(v.hash)">
					{{ restoringHash === v.hash ? 'Restoring…' : 'Restore' }}
				</button>
			</div>
		</div>
	</div>
</template>

<style scoped>
.version-history-panel {
	position: absolute;
	top: 44px;
	right: 8px;
	z-index: 30;
	width: 340px;
	max-height: 70vh;
	overflow-y: auto;
	background: rgba(11, 18, 32, 0.96);
	color: #ddd;
	border: 1px solid #3a4358;
	border-radius: 6px;
	font-family: monospace;
	font-size: 12px;
}
.header {
	display: flex;
	justify-content: space-between;
	align-items: center;
	padding: 6px 10px;
	border-bottom: 1px solid #3a4358;
	font-weight: bold;
	position: sticky;
	top: 0;
	background: rgba(11, 18, 32, 0.96);
}
.close-btn {
	cursor: pointer;
	background: none;
	border: none;
	color: #ddd;
	font-size: 16px;
	line-height: 1;
}
.empty,
.error {
	padding: 10px;
	color: #7d8aa0;
}
.error {
	color: #ff8080;
}
.rows {
	display: flex;
	flex-direction: column;
}
.row {
	padding: 8px 10px;
	border-bottom: 1px solid #262e42;
	display: flex;
	flex-direction: column;
	gap: 6px;
}
.subject {
	color: #4a9eff;
}
.date {
	color: #7d8aa0;
	font-size: 11px;
}
.body {
	margin: 0;
	white-space: pre-wrap;
	word-break: break-word;
	color: #aab4c4;
	font-size: 11px;
	max-height: 100px;
	overflow-y: auto;
}
.restore-btn {
	align-self: flex-start;
	cursor: pointer;
	padding: 3px 10px;
	background: #262e42;
	color: #ddd;
	border: 1px solid #3a4358;
	border-radius: 4px;
	font-family: monospace;
	font-size: 11px;
}
.restore-btn:disabled {
	opacity: 0.6;
	cursor: default;
}
</style>
