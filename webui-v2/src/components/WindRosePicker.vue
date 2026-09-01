<script setup>
// Compass dial for choosing a SET of wind directions.
//
// Deliberately a plain SVG dial rather than the drag-to-rotate ghost overlay used
// for mesh placement: that pattern is for choosing ONE continuous angle by
// scrolling, whereas wind directions are a discrete set, usually a standard rose.
// A dial makes "which 8 am I building" legible at a glance.
//
// METEOROLOGICAL convention, stated in the UI and not just in code: a bearing is
// the direction the wind blows FROM. Wind from the north (0) flows southward, so
// the arrow points from the selected sector toward the middle.
import { computed, ref } from 'vue';

const props = defineProps({
  modelValue: { type: Array, default: () => [] },   // bearings, wind FROM
  highlight: { type: Number, default: null },        // bearing to emphasise, or null
});
const emit = defineEmits(['update:modelValue', 'update:highlight']);

// CX/CY leave room for the cardinal labels at R+12: at CX=70 they landed at x=144 in a
// 140-wide viewBox and were silently clipped (N/E/S/W simply did not render).
const R = 60, CX = 76, CY = 76;
const SECTORS = 16;                                  // 22.5-degree resolution on the dial
const CARDINALS = [[0, 'N'], [90, 'E'], [180, 'S'], [270, 'W']];

const selected = computed(() => new Set(props.modelValue.map((d) => Number(d))));

// Bearing (clockwise from north) -> SVG point. SVG y grows downward, so north is -y.
function pt(bearing, r) {
  const a = (bearing - 90) * Math.PI / 180;
  return [CX + r * Math.cos(a), CY + r * Math.sin(a)];
}

const ticks = computed(() =>
  Array.from({ length: SECTORS }, (_, i) => {
    const b = (i * 360) / SECTORS;
    const [x1, y1] = pt(b, R - 12);
    const [x2, y2] = pt(b, R);
    const [hx, hy] = pt(b, R - 6);
    return { b, x1, y1, x2, y2, hx, hy, on: selected.value.has(b) };
  }));

const labels = computed(() =>
  CARDINALS.map(([b, t]) => {
    const [x, y] = pt(b, R + 12);
    return { b, t, x, y };
  }));

// The flow arrow: FROM the selected bearing, THROUGH the centre -- so it reads as
// "this is the direction air is travelling", which is what the domain is aligned to.
const arrow = computed(() => {
  if (props.highlight == null) return null;
  const [x1, y1] = pt(props.highlight, R - 16);
  const [x2, y2] = pt((props.highlight + 180) % 360, R - 30);
  return { x1, y1, x2, y2 };
});

function toggle(b) {
  const next = new Set(selected.value);
  next.has(b) ? next.delete(b) : next.add(b);
  emit('update:modelValue', [...next].sort((a, c) => a - c));
}
function preset(n) {
  emit('update:modelValue', Array.from({ length: n }, (_, i) => (i * 360) / n));
}
function clear() { emit('update:modelValue', []); }

// The dial is 16 sectors because a wind rose usually is. That must not become a LIMIT:
// the backend accepts any float bearing, and a scientist with a real met record has no
// reason to round 14.94 deg to 22.5. Typed entry adds arbitrary values alongside the
// dial; they show up as chips and highlight like any other.
const typed = ref('');
const typedValid = computed(() => {
  const v = Number(String(typed.value).trim());
  return String(typed.value).trim() !== '' && Number.isFinite(v) && v >= 0 && v <= 360;
});
function addTyped() {
  if (!typedValid.value) return;
  const v = Number(String(typed.value).trim()) % 360;
  const next = new Set(props.modelValue.map(Number));
  next.add(v);
  emit('update:modelValue', [...next].sort((a, c) => a - c));
  typed.value = '';
}
</script>

<template>
  <div class="rose">
    <svg :width="CX * 2" :height="CY * 2" class="dial">
      <circle :cx="CX" :cy="CY" :r="R" class="ring" />
      <circle :cx="CX" :cy="CY" :r="R - 12" class="ring inner" />
      <line v-for="t in ticks" :key="t.b" :x1="t.x1" :y1="t.y1" :x2="t.x2" :y2="t.y2"
        :class="['tick', { on: t.on, hot: highlight === t.b }]" />
      <text v-for="l in labels" :key="l.t" :x="l.x" :y="l.y" class="card">{{ l.t }}</text>
      <line v-if="arrow" :x1="arrow.x1" :y1="arrow.y1" :x2="arrow.x2" :y2="arrow.y2"
        class="flow" marker-end="url(#wr-head)" />
      <defs>
        <marker id="wr-head" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">
          <path d="M0,0 L6,3 L0,6 z" fill="#ff9f43" />
        </marker>
      </defs>
      <!-- Hit targets last so they sit on top: a 22.5-degree wedge is far easier to
           click than the 2px tick that represents it. -->
      <circle v-for="t in ticks" :key="'h' + t.b" :cx="t.hx" :cy="t.hy" r="9"
        class="hit" @click="toggle(t.b)"
        @mouseenter="$emit('update:highlight', t.b)"
        @mouseleave="$emit('update:highlight', null)" />
    </svg>
    <div class="side">
      <div class="presets">
        <button @click="preset(4)">4</button>
        <button @click="preset(8)">8</button>
        <button @click="preset(16)">16</button>
        <button @click="clear()" :disabled="!modelValue.length">✕</button>
      </div>
      <div class="typed">
        <input v-model="typed" placeholder="e.g. 14.94" @keyup.enter="addTyped"
          :class="{ bad: typed && !typedValid }" />
        <button @click="addTyped" :disabled="!typedValid">Add °</button>
      </div>
      <div class="chips">
        <span v-for="d in modelValue" :key="d" class="chip"
          :class="{ hot: highlight === d }"
          @mouseenter="$emit('update:highlight', d)"
          @mouseleave="$emit('update:highlight', null)"
          @click="toggle(d)">{{ Number(d.toFixed ? d.toFixed(2) : d) }}°<i>✕</i></span>
        <span v-if="!modelValue.length" class="muted none">none selected</span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.rose { display: flex; gap: 10px; align-items: flex-start; }
.dial { flex: none; }
.ring { fill: none; stroke: var(--border); }
.ring.inner { stroke-dasharray: 2 4; }
.tick { stroke: var(--muted); stroke-width: 2; opacity: 0.45; }
.tick.on { stroke: var(--accent); stroke-width: 4; opacity: 1; }
.tick.hot { stroke: #ff9f43; stroke-width: 4; opacity: 1; }
.card { fill: var(--muted); font-size: 10px; text-anchor: middle; dominant-baseline: middle; }
.flow { stroke: #ff9f43; stroke-width: 2; }
.hit { fill: transparent; cursor: pointer; }
.side { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 8px; }
.presets { display: flex; gap: 4px; }
.presets button { flex: 1; padding: 4px 0; font-size: 12px; }
.typed { display: flex; gap: 4px; }
.typed input { flex: 1; min-width: 0; font-size: 11px; }
.typed input.bad { border-color: #ff9f43; }
.chips { display: flex; flex-wrap: wrap; gap: 4px; max-height: 96px; overflow-y: auto; }
.chip {
  font-size: 11px; padding: 2px 5px; border-radius: 10px; cursor: pointer;
  background: var(--bg); border: 1px solid var(--border); white-space: nowrap;
}
.chip.hot { border-color: #ff9f43; color: #ff9f43; }
.chip i { font-style: normal; opacity: 0.5; margin-left: 3px; }
.none { font-size: 12px; }
</style>
