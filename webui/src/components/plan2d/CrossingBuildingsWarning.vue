<script setup>
defineProps({
	crossingIds: { type: Array, default: () => [] },
});
</script>

<template>
	<div v-if="crossingIds.length" class="crossing-warning">
		<strong>{{ crossingIds.length }} building{{ crossingIds.length === 1 ? '' : 's' }} cross the boundary</strong>
		<span class="hint">these will be dropped entirely on commit (no partial clipping), not just the part outside the line</span>
		<ul>
			<li v-for="id in crossingIds.slice(0, 20)" :key="id">{{ id }}</li>
		</ul>
		<div v-if="crossingIds.length > 20" class="more">…and {{ crossingIds.length - 20 }} more</div>
	</div>
</template>

<style scoped>
.crossing-warning {
	background: rgba(120, 40, 20, 0.9);
	color: #ffd9c7;
	padding: 8px 10px;
	border-radius: 6px;
	font-family: monospace;
	font-size: 12px;
	max-width: 280px;
	max-height: 220px;
	overflow-y: auto;
}
.hint {
	display: block;
	margin: 4px 0;
	color: #ffb89b;
}
ul {
	margin: 4px 0 0;
	padding-left: 18px;
}
.more {
	margin-top: 4px;
	color: #ffb89b;
}
</style>
