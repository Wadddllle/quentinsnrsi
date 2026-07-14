<script setup>
// Pure display for one async pipeline job's polled state -- BoundaryDrawTool
// owns the actual fetch/poll loop (POST /api/pipeline/run, GET
// /api/pipeline/jobs/{id}) and just hands the latest status dict down here,
// matching this app's existing pattern of components owning their own
// network calls rather than a shared store.
const props = defineProps({
	job: { type: Object, default: null }, // { id, status, stage, log, result, error }
});

const STAGE_LABELS = {
	dtm: 'Building local terrain (DTM)',
	conforming_overlay: 'Draping buildings onto terrain',
	obj_export: 'Exporting OBJ',
	blender_export: 'Building solid mesh (Blender)',
	repair: 'Repairing / verifying watertightness',
	done: 'Done',
};

function stageLabel(stage) {
	return STAGE_LABELS[stage] || stage || 'starting…';
}
</script>

<template>
	<div v-if="job" class="job-progress-panel" :class="job.status">
		<div class="header">
			<span class="status">{{ job.status }}</span>
			<span class="stage">{{ stageLabel(job.stage) }}</span>
		</div>

		<div v-if="job.status === 'done' && job.result" class="result">
			<div>watertight: {{ job.result.watertight }}</div>
			<div>volume: {{ job.result.volume_m3.toFixed(1) }} m³</div>
			<div>faces: {{ job.result.faces.toLocaleString() }}</div>
			<a :href="`/api/pipeline/jobs/${job.id}/download`" download class="download-link">Download STL</a>
		</div>

		<div v-if="job.status === 'error'" class="error">{{ job.error }}</div>

		<details class="log">
			<summary>log ({{ (job.log || []).length }} lines)</summary>
			<pre>{{ (job.log || []).join('\n') }}</pre>
		</details>
	</div>
</template>

<style scoped>
.job-progress-panel {
	font-family: monospace;
	font-size: 12px;
	background: rgba(0, 0, 0, 0.75);
	color: #ddd;
	padding: 8px 10px;
	border-radius: 4px;
	max-width: 360px;
}
.header {
	display: flex;
	gap: 8px;
	align-items: center;
}
.status {
	text-transform: uppercase;
	font-weight: bold;
	color: #9fd4ff;
}
.job-progress-panel.done .status {
	color: #7cfc9a;
}
.job-progress-panel.error .status {
	color: #ff6b6b;
}
.result {
	margin-top: 6px;
	display: flex;
	flex-direction: column;
	gap: 2px;
}
.download-link {
	margin-top: 4px;
	color: #4a9eff;
}
.error {
	margin-top: 6px;
	color: #ff6b6b;
	white-space: pre-wrap;
}
.log {
	margin-top: 6px;
}
.log pre {
	max-height: 200px;
	overflow-y: auto;
	white-space: pre-wrap;
	font-size: 11px;
	margin: 4px 0 0;
}
</style>
