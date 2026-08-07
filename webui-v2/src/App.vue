<script setup>
import { ref, shallowRef, computed, watch, onMounted } from 'vue';
import OrthoWebGLView from './components/OrthoWebGLView.vue';
import LocationSearchBox from './components/LocationSearchBox.vue';
import StlViewer from './components/StlViewer.vue';

// --- state ---
const footprints = shallowRef([]);      // {id, rings, archetype, height}
const mapRef = ref(null);
const ring = ref([]);                   // [[x,y], ...] EPSG:3414
const keptIds = ref([]);
const crossingIds = ref([]);
const stats = ref(null);                // {kept_count, crossing_count, area_km2}
const borderMode = ref('polygon');      // 'polygon' | 'rect' | 'buffer'
const bufferRadius = ref(750);          // meters, for point+buffer
let rectCorner = null;                  // first corner while drawing a rectangle

const view = ref('map');                // 'map' | 'result'
const job = ref(null);                  // job status dict
const jobId = ref(null);
let pollTimer = null;

// --- power-user build options (whitelisted server-side in _BUILD_OPTS) ---
const DEFAULT_OPTS = { voxel_size: 2.0, decimate_error: 2.5, target_reduction: 0.97, workers: 6, include_base: true, placement: 'drape', coupling_lambda: 10.0 };
const options = ref({ ...DEFAULT_OPTS });
const showAdvanced = ref(false);
const jobWasRaw = ref(false);           // did the current job run in raw (voxel<=0) mode
const rawMode = computed(() => Number(options.value.voxel_size) <= 0);
function resetOptions() { options.value = { ...DEFAULT_OPTS }; }

const canGenerate = computed(() => ring.value.length >= 3 && !running.value);
const running = computed(() => job.value && (job.value.status === 'pending' || job.value.status === 'running'));
const result = computed(() => (job.value && job.value.status === 'done') ? job.value.result : null);
const downloadUrl = computed(() => jobId.value ? `/api/stl/jobs/${jobId.value}/download` : null);

// --- footprints ---
onMounted(async () => {
  const res = await fetch('/api/footprints');
  const data = await res.json();
  footprints.value = data.footprints;
});

// --- navigation ---
function onGoto({ x, y }) {
  mapRef.value?.flyTo(x, y, 600);
}
function resetView() { mapRef.value?.resetView(); }

// --- border drawing ---
function onMapClick([x, y]) {
  if (view.value !== 'map') return;
  if (borderMode.value === 'polygon') {
    ring.value = [...ring.value, [x, y]];
  } else if (borderMode.value === 'rect') {
    if (!rectCorner) {
      rectCorner = [x, y];
      ring.value = [[x, y]]; // seed so a marker shows
    } else {
      const [x0, y0] = rectCorner;
      ring.value = [[x0, y0], [x, y0], [x, y], [x0, y]];
      rectCorner = null;
    }
  } else if (borderMode.value === 'buffer') {
    const r = Math.max(50, Number(bufferRadius.value) || 0);
    ring.value = [[x - r, y - r], [x + r, y - r], [x + r, y + r], [x - r, y + r]];
  }
}
function setBorderMode(m) { borderMode.value = m; clearBorder(); }
function undoPoint() {
  if (borderMode.value === 'polygon') ring.value = ring.value.slice(0, -1);
  else clearBorder();
}
function clearBorder() {
  ring.value = []; rectCorner = null;
  keptIds.value = []; crossingIds.value = []; stats.value = null;
}

// --- live preview (debounced) ---
let previewTimer = null;
watch(ring, () => {
  clearTimeout(previewTimer);
  if (ring.value.length < 3) { keptIds.value = []; crossingIds.value = []; stats.value = null; return; }
  previewTimer = setTimeout(runPreview, 250);
}, { deep: true });

async function runPreview() {
  const res = await fetch('/api/domain/preview', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ ring: ring.value }),
  });
  if (!res.ok) return;
  const d = await res.json();
  keptIds.value = d.kept; crossingIds.value = d.crossing;
  stats.value = { kept_count: d.kept_count, crossing_count: d.crossing_count, area_km2: d.area_km2 };
}

// --- generate STL ---
async function generate() {
  // coerce option values to numbers (v-model on number inputs can yield strings) --
  // include_base is a real boolean and placement a real string, leave those alone.
  const NON_NUMERIC = new Set(['include_base', 'placement']);
  const opts = {};
  for (const [k, v] of Object.entries(options.value)) opts[k] = NON_NUMERIC.has(k) ? v : Number(v);
  // lambda is meaningless outside laplacian mode -- don't send it and imply it did something
  if (opts.placement !== 'laplacian') delete opts.coupling_lambda;
  jobWasRaw.value = rawMode.value;
  const res = await fetch('/api/stl/run', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ ring: ring.value, options: opts }),
  });
  const d = await res.json();
  jobId.value = d.job_id;
  job.value = { status: 'pending', stage: null, log: [] };
  pollJob();
}
function pollJob() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    const res = await fetch(`/api/stl/jobs/${jobId.value}`);
    job.value = await res.json();
    if (job.value.status === 'done') { view.value = 'result'; return; }
    if (job.value.status === 'error') return;
    pollJob();
  }, 2000);
}

const stageLabels = {
  tiles: 'Finding tiles', extract: 'Extracting buildings', terrain: 'Building terrain',
  write: 'Writing scene', fuse: 'Fusing (Blender voxel remesh)', decimate: 'Decimating',
  clip: 'Clipping to domain', polish: 'Polishing (watertight)', raw: 'Exporting raw mesh',
  verify: 'Verifying', done: 'Done',
};
const optSummary = computed(() =>
  rawMode.value ? 'raw high-fidelity (not watertight)'
    : `voxel ${options.value.voxel_size}m · error ${options.value.decimate_error}m`);
const logTail = computed(() => (job.value?.log || []).slice(-6));
</script>

<template>
  <div class="app">
    <header class="topbar">
      <div class="brand">SBG · CFD Domain → STL</div>
      <LocationSearchBox @goto="onGoto" />
      <div class="spacer" />
      <div class="viewtoggle">
        <button :class="{ active: view === 'map' }" @click="view = 'map'">2D Map</button>
        <button :class="{ active: view === 'result' }" :disabled="!result" @click="view = 'result'">3D STL</button>
      </div>
      <button @click="resetView" v-if="view === 'map'">Reset view</button>
    </header>

    <div class="body">
      <aside class="sidebar">
        <section>
          <h3>1 · Domain border</h3>
          <div class="modes">
            <button :class="{ active: borderMode === 'polygon' }" @click="setBorderMode('polygon')">Polygon</button>
            <button :class="{ active: borderMode === 'rect' }" @click="setBorderMode('rect')">Rectangle</button>
            <button :class="{ active: borderMode === 'buffer' }" @click="setBorderMode('buffer')">Point + buffer</button>
          </div>
          <p class="hint muted" v-if="borderMode === 'polygon'">Click points on the map to trace a polygon.</p>
          <p class="hint muted" v-else-if="borderMode === 'rect'">Click two opposite corners.</p>
          <label v-else class="hint">Radius (m):
            <input type="number" v-model="bufferRadius" min="50" step="50" style="width:90px" />
            <span class="muted"> then click a location.</span>
          </label>
          <div class="row">
            <button @click="undoPoint" :disabled="!ring.length">Undo</button>
            <button @click="clearBorder" :disabled="!ring.length">Clear</button>
          </div>
          <div class="stats" v-if="stats">
            <div><b>{{ stats.kept_count }}</b> buildings kept</div>
            <div class="warn" v-if="stats.crossing_count"><b>{{ stats.crossing_count }}</b> crossing the border (dropped)</div>
            <div class="muted">{{ stats.area_km2 }} km²</div>
          </div>
          <p class="hint muted" v-else-if="ring.length && ring.length < 3">Need at least 3 points.</p>
        </section>

        <section>
          <h3>2 · Generate STL</h3>
          <div class="optbar">
            <span class="muted">{{ optSummary }}</span>
            <button class="link" @click="showAdvanced = true">⚙ Advanced</button>
          </div>
          <button class="primary wide" :disabled="!canGenerate" @click="generate">
            {{ rawMode ? 'Generate raw mesh' : 'Generate watertight STL' }}
          </button>

          <div class="job" v-if="job">
            <div class="jobstage" v-if="running">
              <span class="spinner" /> {{ stageLabels[job.stage] || job.stage || 'starting…' }}
            </div>
            <div v-if="job.status === 'error'" class="err">Error: {{ job.error }}</div>
            <pre class="log" v-if="logTail.length">{{ logTail.join('\n') }}</pre>
          </div>

          <div class="resultbox" v-if="result">
            <div v-if="jobWasRaw" class="warn">◆ high-fidelity raw mesh (not watertight, by design)</div>
            <div v-else :class="result.watertight ? 'ok' : 'warn'">
              {{ result.watertight ? '✓ watertight' : '⚠ not watertight' }}
            </div>
            <div class="muted">{{ result.faces.toLocaleString() }} faces · {{ result.size_mb }} MB · {{ result.elapsed_s }}s</div>
            <div class="row">
              <a :href="downloadUrl" download><button class="primary">Download STL</button></a>
              <button @click="view = 'result'">View in 3D</button>
            </div>
          </div>
        </section>
      </aside>

      <main class="viewport">
        <OrthoWebGLView v-show="view === 'map'" ref="mapRef"
          :footprints="footprints" :ring="ring" :keptIds="keptIds" :crossingIds="crossingIds"
          @click="onMapClick" />
        <StlViewer v-if="view === 'result'" :url="result ? downloadUrl : null" />
      </main>
    </div>

    <div v-if="showAdvanced" class="modal-backdrop" @click.self="showAdvanced = false">
      <div class="modal">
        <h3>Advanced build settings</h3>
        <label>Voxel size (m)
          <input type="number" step="0.5" min="0" v-model="options.voxel_size" />
          <small class="muted">Fuse resolution — smaller = finer + more faces. <b>0 = raw high-fidelity</b>, skips remesh/decimate/clip (fast, NOT watertight).</small>
        </label>
        <label>Decimate error (m)
          <input type="number" step="0.5" min="0" v-model="options.decimate_error" :disabled="rawMode" />
          <small class="muted">Max geometric error allowed when simplifying. Lower = more detail. Tie to your CFD cell size (~3m).</small>
        </label>
        <label>Target reduction
          <input type="number" step="0.01" min="0" max="0.999" v-model="options.target_reduction" :disabled="rawMode" />
          <small class="muted">Face-removal cap (0.97). The error above usually governs unless this binds first.</small>
        </label>
        <label>Workers
          <input type="number" step="1" min="1" v-model="options.workers" />
          <small class="muted">Parallel tile decode (only matters when fetching tiles live).</small>
        </label>
        <label>Building placement on slopes
          <select v-model="options.placement">
            <option value="group">Group connected</option>
            <option value="drape">Drape — no grouping (default)</option>
            <option value="laplacian">Soft coupling (λ)</option>
          </select>
          <small class="muted">
            How buildings joined across a slope get levelled. There is no universally
            right answer — two real cases measure identical terrain spread and want
            opposite treatment.
            <template v-if="options.placement === 'group'">
              <b>Group:</b> bridge-linked buildings share one flat level and stay
              coplanar; a string of blocks up a hill gets a terrace carved into the
              hillside to hold that level.
            </template>
            <template v-else-if="options.placement === 'drape'">
              <b>Drape:</b> every building sits on its own ground. Hillside strings
              terrace correctly; anything joined by a bridge shears apart
              (measured ~20m on a real pair).
            </template>
            <template v-else>
              <b>Soft coupling:</b> connected buildings pull toward each other instead
              of being forced level. Compact complexes stay flat, long chains ramp
              gently — both resolve without being classified.
            </template>
          </small>
        </label>
        <label v-if="options.placement === 'laplacian'">Coupling λ
          <input type="number" step="1" min="0" v-model="options.coupling_lambda" />
          <small class="muted">
            Higher = flatter / more grouped (∞ ≡ Group), lower = more terracing
            (0 ≡ Drape). At 10 a real bridge-linked complex held to 0.96m of step
            while a hillside string terraced 8.7m.
          </small>
        </label>
        <label class="row" style="align-items: center; gap: 8px;">
          <input type="checkbox" v-model="options.include_base" />
          Include ground/terrain plane
          <small class="muted">Off = buildings only, no ground plate. In the watertight path the terrain still backs the voxel remesh internally and is subtracted at the end; in raw (voxel 0) mode it is simply not written.</small>
        </label>
        <div class="row modal-actions">
          <button @click="resetOptions">Reset defaults</button>
          <button class="primary" @click="showAdvanced = false">Done</button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.app { display: flex; flex-direction: column; height: 100%; }
.topbar {
  display: flex; align-items: center; gap: 12px; padding: 8px 14px;
  background: var(--panel); border-bottom: 1px solid var(--border);
}
.brand { font-weight: 700; letter-spacing: 0.3px; }
.spacer { flex: 1; }
.viewtoggle { display: flex; gap: 4px; }
.body { flex: 1; display: flex; min-height: 0; }
.sidebar {
  width: 300px; padding: 14px; background: var(--panel);
  border-right: 1px solid var(--border); overflow-y: auto;
  display: flex; flex-direction: column; gap: 20px;
}
.sidebar h3 { margin: 0 0 8px; font-size: 14px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; }
.modes { display: flex; gap: 4px; flex-wrap: wrap; }
.modes button { flex: 1; font-size: 13px; padding: 6px 6px; }
.hint { font-size: 13px; margin: 8px 0; }
.row { display: flex; gap: 6px; margin-top: 10px; }
.wide { width: 100%; }
.stats { margin-top: 12px; font-size: 14px; display: flex; flex-direction: column; gap: 4px; }
.stats .warn, .warn { color: var(--accent-2); }
.viewport { flex: 1; position: relative; min-width: 0; }
.viewport > * { position: absolute; inset: 0; }
.job { margin-top: 12px; }
.jobstage { display: flex; align-items: center; gap: 8px; font-size: 14px; }
.log {
  margin-top: 8px; font-size: 11px; color: var(--muted); white-space: pre-wrap;
  background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 8px; max-height: 140px; overflow: auto;
}
.resultbox { margin-top: 12px; display: flex; flex-direction: column; gap: 6px; }
.resultbox .ok { color: var(--ok); font-weight: 600; }
.err { color: var(--danger); font-size: 13px; margin-top: 8px; }
a { text-decoration: none; }
.spinner {
  width: 12px; height: 12px; border: 2px solid var(--border); border-top-color: var(--accent);
  border-radius: 50%; display: inline-block; animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.optbar { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; font-size: 13px; }
.link { background: none; border: none; color: var(--accent); padding: 2px 4px; }
.link:hover { text-decoration: underline; }
.modal-backdrop {
  position: fixed; inset: 0; background: rgba(4, 8, 16, 0.6);
  display: flex; align-items: center; justify-content: center; z-index: 100;
}
.modal {
  background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
  padding: 20px 22px; width: 380px; max-width: 92vw; max-height: 88vh; overflow-y: auto;
  display: flex; flex-direction: column; gap: 14px;
}
.modal h3 { margin: 0; font-size: 16px; }
.modal label { display: flex; flex-direction: column; gap: 4px; font-size: 14px; }
.modal label input { width: 120px; }
.modal label select { width: 220px; padding: 3px; }
.modal small { line-height: 1.35; }
.modal-actions { justify-content: flex-end; margin-top: 4px; }
</style>
