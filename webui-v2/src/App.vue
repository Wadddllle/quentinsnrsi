<script setup>
import { ref, shallowRef, computed, watch, onMounted, nextTick } from 'vue';
import OrthoWebGLView from './components/OrthoWebGLView.vue';
import LocationSearchBox from './components/LocationSearchBox.vue';
import StlViewer from './components/StlViewer.vue';
import WindRosePicker from './components/WindRosePicker.vue';

// --- state ---
const footprints = shallowRef([]);      // {id, rings, archetype, height}
const mapRef = ref(null);
// The drawn ring is the REGION OF INTEREST. When a CFD buffer is in play the
// polygon actually BUILT is larger (the server-computed envelope) and the buffer
// ring is bare terrain -- so kept/crossing below always describe the ROI, never
// the envelope. Do not conflate the two in the UI.
const ring = ref([]);                   // [[x,y], ...] EPSG:3414
const keptIds = ref([]);
const crossingIds = ref([]);
const stats = ref(null);                // {kept_count, crossing_count, area_km2, bounds}
const borderMode = ref('polygon');      // 'polygon' | 'rect' | 'buffer'
const bboxText = ref('');               // typed domain, same format as the CLI's --bbox
const bufferRadius = ref(750);          // meters, for point+buffer
let rectCorner = null;                  // first corner while drawing a rectangle

const view = ref('map');                // 'map' | 'result'
const job = ref(null);                  // job status dict
const jobId = ref(null);
let pollTimer = null;

// --- wind directions + CFD buffer -------------------------------------------------
// EVERY ring/arrow drawn for these comes from the server (wind_plan.py), which is the
// same code the build uses. The frontend does no trigonometry: a second implementation
// of the rotation convention would drift, and a mis-georeferenced dose map still looks
// entirely plausible.
const windEnabled = ref(false);
const windDegs = ref([]);               // bearings the wind blows FROM
const highlightDir = ref(null);         // bearing to emphasise on the dial + map
const hSource = ref('p90');             // 'p90' | 'max' | 'median'
const bufferMode = ref('auto');         // 'auto' (5H/15H/5H) | 'manual'
const bufferM = ref({ upwind: 250, downwind: 750, lateral: 250 });
const heights = ref(null);              // {count,max,p90,median} for the ROI
const windPlan = ref(null);             // server-resolved plan (rings, buffer, warnings)
const planError = ref(null);
const selectedOutput = ref(0);          // which direction the 3D viewer shows
const resultBox = ref(null);

const windActive = computed(() => windEnabled.value && windDegs.value.length > 0);

// Mirrors build.py's own resolution, and the counts shown next to it come from the OSM
// footprint index while the builder decides per OneMap piece -- so this is the right
// STORY, not a per-building promise. The two datasets agree approximately (measured 18
// crossing vs 21 pieces recovered on the same ROI), which is why the wording says
// "straddling" rather than naming buildings.
const crossingKept = computed(() => options.value.crossing === 'keep'
  || (options.value.crossing !== 'drop' && windActive.value));

function windPayload() {
  if (!windActive.value) return null;
  const w = { dirs: windDegs.value, h_source: hSource.value };
  // Send the resolved metres back verbatim in manual mode so the domain is frozen
  // exactly as previewed -- an auto buffer could otherwise resize if the height
  // index changed between preview and submit.
  if (bufferMode.value === 'manual') {
    w.buffer_m = {
      upwind: Number(bufferM.value.upwind),
      downwind: Number(bufferM.value.downwind),
      lateral: Number(bufferM.value.lateral),
    };
  }
  return w;
}

// Adopt the server's auto-sized buffer as the starting point for manual edits, so
// switching to manual never jumps to unrelated numbers.
function useManualBuffer() {
  if (windPlan.value) bufferM.value = { ...windPlan.value.buffer_m };
  bufferMode.value = 'manual';
}

// Turn the server's [tail, head] into a polyline WITH a head, so the map says which
// way the air moves. An undirected segment is the worst possible rendering here: the
// meteorological "wind from" convention is exactly what people get backwards. This is
// pure rendering off the server's own two endpoints -- it cannot invert the direction,
// because it never computes one.
function arrowPolyline([tail, head]) {
  const [x1, y1] = tail, [x2, y2] = head;
  const dx = x2 - x1, dy = y2 - y1;
  const len = Math.hypot(dx, dy) || 1;
  const ux = dx / len, uy = dy / len, s = len * 0.14;
  const bx = ux * 0.866, by = uy * 0.866, px = uy * 0.5, py = ux * 0.5;
  const b1 = [x2 - s * (bx - px), y2 - s * (by + py)];
  const b2 = [x2 - s * (bx + px), y2 - s * (by - py)];
  return [tail, head, b1, head, b2];   // retrace the tip to draw both barbs in one line
}

const overlays = computed(() => {
  const p = windPlan.value;
  if (!p) return [];
  const out = [{ points: p.envelope_world, color: 0x5a7fb5, width: 2, z: 0.4 }];
  const shown = p.directions.filter((d) =>
    highlightDir.value == null || d.wind_from_deg === highlightDir.value);
  // With no hover, showing all N rectangles at once is unreadable, so only the first
  // is drawn as a representative; hovering a chip/tick isolates that direction.
  const pick = highlightDir.value == null ? p.directions.slice(0, 1) : shown;
  for (const d of pick) {
    out.push({ points: d.rect_world, color: 0x4a9eff, width: 2, z: 0.6 });
    out.push({ points: arrowPolyline(d.arrow_world), color: 0xff9f43, width: 4, z: 0.7, closed: false });
  }
  return out;
});

// --- power-user build options (whitelisted server-side in _BUILD_OPTS) ---
const DEFAULT_OPTS = { voxel_size: 2.0, decimate_error: 2.5, target_reduction: 0.97, workers: 6, include_base: true, placement: 'drape', coupling_lambda: 10.0, crossing: 'auto' };
const options = ref({ ...DEFAULT_OPTS });
const showAdvanced = ref(false);
// Long-form explanation belongs behind an (i), not printed in the sidebar. This is a
// tool, not a README -- the panel should say WHAT, the popover says WHY.
const info = ref(null);
function toggleInfo(k) { info.value = info.value === k ? null : k; }
const jobWasRaw = ref(false);           // did the current job run in raw (voxel<=0) mode
const rawMode = computed(() => Number(options.value.voxel_size) <= 0);
// Remember the cell size across a trip through Raw, so switching back does not silently
// reset a deliberately-chosen value to the default.
let lastCellSize = DEFAULT_OPTS.voxel_size;
watch(() => options.value.voxel_size, (v) => { if (Number(v) > 0) lastCellSize = Number(v); });
function setWatertight() { options.value.voxel_size = lastCellSize || DEFAULT_OPTS.voxel_size; }
function resetOptions() { options.value = { ...DEFAULT_OPTS }; }

// Cancel is COOPERATIVE: the pipeline stops at its next stage boundary, because the
// heavy stages are single uninterruptible calls into meshlib/OpenVDB. The button says
// "Stopping…" afterwards rather than pretending it was instant.
const cancelling = ref(false);
async function cancelJob() {
  if (!jobId.value) return;
  cancelling.value = true;
  await fetch(`/api/stl/jobs/${jobId.value}/cancel`, { method: 'POST' }).catch(() => {});
}


const canGenerate = computed(() => ring.value.length >= 3 && !running.value);
const running = computed(() => job.value && (job.value.status === 'pending' || job.value.status === 'running'));
const result = computed(() => (job.value && job.value.status === 'done') ? job.value.result : null);
const downloadUrl = computed(() => jobId.value ? `/api/stl/jobs/${jobId.value}/download` : null);

// The backend runs under plain uvicorn with no reload, so a Python change needs a
// manual restart -- and an older backend silently IGNORES the `wind` field instead of
// rejecting it, returning one axis-aligned domain to someone who asked for eight
// rotated ones. Detect that up front rather than letting it look like it worked.
const staleBackend = ref(false);

// --- footprints ---
onMounted(async () => {
  fetch('/api/health').then((r) => r.json()).then((h) => {
    staleBackend.value = !(h.features || []).includes('wind');
  }).catch(() => {});
  const res = await fetch('/api/footprints');
  const data = await res.json();
  footprints.value = data.footprints;
});

// --- navigation ---
// --- typed domain entry ---------------------------------------------------------
// Parsed leniently (commas or whitespace) but validated strictly: a domain that is
// silently mis-parsed is worse than one that is refused.
function parseBbox(t) {
  const n = String(t).trim().split(/[\s,]+/).filter(Boolean).map(Number);
  if (n.length !== 4 || n.some((v) => !Number.isFinite(v))) return null;
  const [x0, y0, x1, y1] = n;
  if (x0 === x1 || y0 === y1) return null;
  return [Math.min(x0, x1), Math.min(y0, y1), Math.max(x0, x1), Math.max(y0, y1)];
}

const bboxValid = computed(() => parseBbox(bboxText.value) !== null);

// The ring is the single source of truth for the ROI, so a typed bbox just writes one
// -- no new backend path, and every downstream consumer (preview, wind plan, generate)
// is unchanged.
function applyBbox() {
  const b = parseBbox(bboxText.value);
  if (!b) return;
  const [x0, y0, x1, y1] = b;
  ring.value = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]];
  rectCorner = null;
  mapRef.value?.fitBounds?.(x0, y0, x1, y1);
}

// Read back the ROI's own bounds so a drawn domain can be written down. `bounds` comes
// from /api/domain/preview, computed server-side from the same polygon that gets built.
const roiBbox = computed(() => {
  const b = stats.value?.bounds;
  if (!b) return null;
  return b.map((v) => Math.round(v * 10) / 10).join(',');
});

function copyBbox() {
  if (roiBbox.value) navigator.clipboard?.writeText(roiBbox.value);
}

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
watch([ring, windEnabled, windDegs, hSource, bufferMode, bufferM], () => {
  clearTimeout(previewTimer);
  if (ring.value.length < 3) {
    keptIds.value = []; crossingIds.value = []; stats.value = null;
    windPlan.value = null; heights.value = null; planError.value = null;
    return;
  }
  previewTimer = setTimeout(runPreview, 250);
}, { deep: true });

async function runPreview() {
  const body = { ring: ring.value };
  const w = windPayload();
  if (w) body.wind = w;
  const res = await fetch('/api/domain/preview', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    // A 400 here is a real, actionable message (envelope too large, bad bearing...)
    // -- surface it instead of leaving a stale preview on screen.
    planError.value = (await res.json().catch(() => ({}))).detail || `preview failed (${res.status})`;
    windPlan.value = null;
    return;
  }
  planError.value = null;
  const d = await res.json();
  keptIds.value = d.kept; crossingIds.value = d.crossing;
  stats.value = { kept_count: d.kept_count, crossing_count: d.crossing_count,
                  area_km2: d.area_km2, bounds: d.bounds };
  windPlan.value = d.wind || null;
  if (d.wind?.h_stats) heights.value = d.wind.h_stats;
  // Asked for wind and got no plan back => the backend ignored the field.
  if (windActive.value && !d.wind) {
    staleBackend.value = true;
    planError.value = 'This backend ignored the wind request — restart the server to pick up the current code.';
  }
}

// Heights drive the buffer proposal, so fetch them as soon as the wind panel opens
// -- before any direction is picked, since the numbers are what the user needs to
// choose between max and p90.
watch(windEnabled, async (on) => {
  if (!on || ring.value.length < 3 || heights.value) return;
  const res = await fetch('/api/domain/heights', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ ring: ring.value }),
  });
  if (res.ok) heights.value = await res.json();
});

// --- generate STL ---
async function generate() {
  // Coerce only what is actually numeric. v-model on a number input yields strings, so
  // something has to cast -- but a hand-maintained "don't cast these" list ROTS: add a
  // string option, forget the list, and Number('auto') is NaN, which JSON.stringify
  // writes as null and the server then rejects. That has now happened twice (bearings,
  // then `crossing`). Deriving it from DEFAULT_OPTS' own types cannot rot: a new string
  // or boolean option is correct the moment it is added.
  const opts = {};
  for (const [k, v] of Object.entries(options.value)) {
    opts[k] = typeof DEFAULT_OPTS[k] === 'number' ? Number(v) : v;
  }
  // lambda is meaningless outside laplacian mode -- don't send it and imply it did something
  if (opts.placement !== 'laplacian') delete opts.coupling_lambda;
  jobWasRaw.value = rawMode.value;
  const body = { ring: ring.value, options: opts };
  const w = windPayload();
  if (w) body.wind = w;
  const res = await fetch('/api/stl/run', {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    job.value = { status: 'error', log: [], error: (await res.json().catch(() => ({}))).detail || `run failed (${res.status})` };
    return;
  }
  const d = await res.json();
  // Last line of defence: never let a wind run look like it succeeded when the server
  // quietly dropped the request and built a single axis-aligned domain instead.
  if (windActive.value && !d.wind) {
    staleBackend.value = true;
    job.value = { status: 'error', log: [], error: 'Server ignored the wind directions — it is running older code. Restart it and try again.' };
    return;
  }
  jobId.value = d.job_id;
  selectedOutput.value = 0;
  cancelling.value = false;
  job.value = { status: 'pending', stage: null, log: [] };
  pollJob();
}
function pollJob() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    const res = await fetch(`/api/stl/jobs/${jobId.value}`);
    job.value = await res.json();
    if (job.value.status === 'done') {
      // A single output has nothing to choose between, so show it. A wind run does:
      // jumping to 3D would display direction 0 only and scroll the N-direction table
      // out of sight, which is the actual deliverable.
      if ((job.value.result?.outputs?.length || 1) > 1) {
        nextTick(() => resultBox.value?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }));
      } else {
        view.value = 'result';
      }
      return;
    }
    if (job.value.status === 'error') return;
    pollJob();
  }, 2000);
}

const stageLabels = {
  tiles: 'Finding tiles', extract: 'Extracting buildings', terrain: 'Building terrain',
  write: 'Writing scene', fuse: 'Fusing (voxel remesh)', decimate: 'Decimating',
  clip: 'Clipping to domain', polish: 'Polishing (watertight)', raw: 'Exporting raw mesh',
  verify: 'Verifying', done: 'Done',
};

// --- results (1 output normally, N for a wind rose) --------------------------------
const outputs = computed(() => result.value?.outputs || (result.value ? [result.value] : []));
const isMulti = computed(() => outputs.value.length > 1);
const bundleUrl = computed(() => jobId.value ? `/api/stl/jobs/${jobId.value}/bundle` : null);
function fileUrl(name) { return `/api/stl/jobs/${jobId.value}/files/${encodeURIComponent(name)}`; }
const viewerUrl = computed(() => {
  if (!outputs.value.length) return null;
  const o = outputs.value[Math.min(selectedOutput.value, outputs.value.length - 1)];
  return isMulti.value ? fileUrl(o.filename) : downloadUrl.value;
});
function showOutput(i) { selectedOutput.value = i; view.value = 'result'; }
const optSummary = computed(() =>
  rawMode.value ? 'raw high-fidelity (not watertight)'
    : `voxel ${options.value.voxel_size}m · error ${options.value.decimate_error}m`);
// The server already tails to LOG_TAIL_LINES (400) and reports log_total, so slicing
// again here just threw away 394 of them -- which is why the panel looked like it could
// only hold ~6 lines and appeared to wipe itself on every 2 s poll.
const logBox = ref(null);
const logTail = computed(() => job.value?.log || []);
const logHidden = computed(() =>
  Math.max(0, (job.value?.log_total || 0) - logTail.value.length));
// Follow the tail, but only if the user is already at the bottom -- otherwise scrolling
// back to read an earlier stage would be yanked away by the next poll.
watch(logTail, () => {
  const el = logBox.value;
  if (!el) return;
  const pinned = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
  if (pinned) nextTick(() => { el.scrollTop = el.scrollHeight; });
});
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

    <div v-if="staleBackend" class="stale">
      ⚠ This server is running older code and will ignore wind directions — restart it
      (<code>python -m sbg.onemap_native.ui</code>) to enable wind-aligned domains.
    </div>

    <div class="body">
      <aside class="sidebar">
        <section>
          <h3>1 · Area of interest</h3>
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

          <!-- Typed domain entry. Clicking is fine for exploring, but a domain that
               cannot be written down cannot be reproduced, cited, or handed to a
               colleague -- and the CLI has had --bbox all along. Accepts that exact
               string so the two are interchangeable. -->
          <div class="bboxrow">
            <input v-model="bboxText" placeholder="xmin,ymin,xmax,ymax (EPSG:3414)"
              @keyup.enter="applyBbox" />
            <button @click="applyBbox" :disabled="!bboxValid">Set</button>
          </div>
          <p class="hint muted bboxhint">
            <span v-if="bboxText && !bboxValid" class="warn">Need four numbers, xmin,ymin,xmax,ymax.</span>
            <span v-else-if="roiBbox">
              current: <code>{{ roiBbox }}</code>
              <button class="link" @click="copyBbox">copy</button>
            </span>
            <span v-else>Or type a domain directly — same format as the CLI's <code>--bbox</code>.</span>
          </p>
          <div class="row">
            <button @click="undoPoint" :disabled="!ring.length">Undo</button>
            <button @click="clearBorder" :disabled="!ring.length">Clear</button>
          </div>
          <div class="stats" v-if="stats">
            <div><b>{{ stats.kept_count }}</b> buildings kept</div>
            <div :class="crossingKept ? 'muted' : 'warn'" v-if="stats.crossing_count">
              <b>{{ stats.crossing_count }}</b> straddling the edge
              <span v-if="crossingKept">— built whole (the buffer extends past them)</span>
              <span v-else>— dropped (they would be sliced flat at the boundary)</span>
            </div>
            <div class="muted">{{ stats.area_km2 }} km²</div>
          </div>
          <p class="hint muted" v-else-if="ring.length && ring.length < 3">Need at least 3 points.</p>
        </section>

        <section>
          <h3>2 · Wind &amp; CFD buffer <span class="muted opt">optional</span></h3>
          <label class="row toggle">
            <input type="checkbox" v-model="windEnabled" :disabled="ring.length < 3 || staleBackend" />
            Build wind-aligned domains
            <button class="ibtn" @click.prevent="toggleInfo('wind')" title="What this does">i</button>
          </label>
          <p class="hint muted" v-if="!windEnabled">
            One STL of the area you drew, axis-aligned, no buffer.
          </p>
          <div class="pop" v-if="info === 'wind'">
            <p><b>Off</b> — one STL of the area you drew, axis-aligned. The domain walls
              are your area's walls, with no buffer.</p>
            <p><b>On</b> — one STL <b>per wind direction</b>, each pre-rotated so the flow
              is +Y. The Fluent inlet is always <code>ymin</code>, so 45° costs no more
              setup than 0°. A terrain-only CFD buffer (5H&nbsp;upwind / 15H&nbsp;downwind
              / 5H&nbsp;lateral, COST&nbsp;732) is added around your area.</p>
            <p>The buffer lives on this switch — for one buffered domain instead of a
              rose, turn it on and pick a single direction.</p>
          </div>

          <template v-if="windEnabled">
            <p class="hint muted">Directions the wind blows <b>from</b> (meteorological):</p>
            <WindRosePicker v-model="windDegs" v-model:highlight="highlightDir" />

            <div class="hgroup" v-if="heights">
              <div class="muted small">
                heights from {{ heights.count }} surveyed buildings · tallest
                <b>{{ heights.max.toFixed(0) }} m</b> · p90
                <b>{{ heights.p90.toFixed(0) }} m</b>
              </div>
              <!-- Both are offered deliberately: one tall outlier doubles H and so
                   ~2.2x's the domain area. Silently picking max is a trap. -->
              <div class="modes">
                <button :class="{ active: bufferMode === 'auto' && hSource === 'p90' }"
                  @click="bufferMode = 'auto'; hSource = 'p90'">p90 ({{ heights.p90.toFixed(0) }} m)</button>
                <button :class="{ active: bufferMode === 'auto' && hSource === 'max' }"
                  @click="bufferMode = 'auto'; hSource = 'max'">Tallest ({{ heights.max.toFixed(0) }} m)</button>
                <button :class="{ active: bufferMode === 'manual' }" @click="useManualBuffer">Metres</button>
              </div>
            </div>

            <div class="bufgrid" v-if="bufferMode === 'manual'">
              <label>Upwind <input type="number" min="0" step="10" v-model="bufferM.upwind" /></label>
              <label>Downwind <input type="number" min="0" step="10" v-model="bufferM.downwind" /></label>
              <label>Lateral <input type="number" min="0" step="10" v-model="bufferM.lateral" /></label>
            </div>

            <div class="stats" v-if="windPlan">
              <div class="muted small">
                Buffer {{ windPlan.buffer_m.upwind.toFixed(0) }} /
                {{ windPlan.buffer_m.downwind.toFixed(0) }} /
                {{ windPlan.buffer_m.lateral.toFixed(0) }} m
                <span v-if="windPlan.buffer_source !== 'manual'">(5H / 15H / 5H)</span>
              </div>
              <div>
                <b>{{ windPlan.envelope_km2 }} km²</b> built once
                <span class="muted">({{ (windPlan.envelope_km2 / stats.area_km2).toFixed(1) }}× the area,
                  {{ windPlan.dirs.length }} direction{{ windPlan.dirs.length === 1 ? '' : 's' }})</span>
              </div>
              <div class="muted small">Buffer is bare terrain — no buildings outside your area.</div>
              <div class="warn small" v-for="w in windPlan.warnings" :key="w">⚠ {{ w }}</div>
            </div>
            <div class="err small" v-if="planError">{{ planError }}</div>
          </template>
        </section>

        <section>
          <h3>3 · Generate STL</h3>
          <!-- TWO modes, not three. The earlier "Watertight 2 m / Raw / Other" split was
               broken: Other was unreachable from Raw, and stepping the cell size up to 2
               silently flipped the active button back to Watertight, hiding the input
               mid-edit. Cell size is a PROPERTY of watertight mode, so it lives with it. -->
          <div class="modes">
            <button :class="{ active: !rawMode }" @click="setWatertight">Watertight</button>
            <button :class="{ active: rawMode }" @click="options.voxel_size = 0">Raw detail</button>
          </div>
          <label v-if="!rawMode" class="hint cellsize">Cell size (m)
            <input type="number" step="0.5" min="0.5" v-model.number="options.voxel_size" />
            <button class="ibtn" @click.prevent="toggleInfo('voxel')" title="What this does">i</button>
          </label>
          <p class="hint muted" v-else>
            Exact captured geometry, not watertight.
            <button class="ibtn" @click.prevent="toggleInfo('voxel')" title="What this does">i</button>
          </p>
          <div class="pop" v-if="info === 'voxel'">
            <p><b>Watertight</b> fuses terrain and buildings on a voxel grid, then
              decimates and clips. Closed, single body, what a strict mesher wants.</p>
            <p><b>Raw</b> skips all of that and ships the captured geometry as-is. Full
              detail, much faster, <b>not</b> watertight — accepted by Ansys
              fault-tolerant meshing and snappyHexMesh, rejected by Ansys watertight.</p>
            <p>Smaller voxels mean finer detail and more faces.</p>
          </div>
          <div class="optbar">
            <span class="muted">{{ optSummary }}</span>
            <button class="link" @click="showAdvanced = true">⚙ Advanced</button>
          </div>
          <button v-if="!running" class="primary wide" :disabled="!canGenerate" @click="generate">
            {{ rawMode ? 'Generate raw mesh' : 'Generate watertight STL' }}
            <template v-if="windActive"> × {{ windDegs.length }}</template>
          </button>
          <button v-else class="wide danger" :disabled="cancelling" @click="cancelJob">
            {{ cancelling ? 'Stopping at the next stage…' : 'Cancel' }}
          </button>
          <p class="hint muted" v-if="windActive">
            Heavy stages run <b>once</b>; each extra direction costs only a rotate + clip.
          </p>

          <div class="job" v-if="job">
            <div class="muted" v-if="job.status === 'cancelled'">Cancelled — no output written.</div>
            <div class="jobstage" v-if="running">
              <span class="spinner" /> {{ stageLabels[job.stage] || job.stage || 'starting…' }}
              <span class="muted small" v-if="job.substage">· {{ job.substage }}</span>
            </div>
            <div v-if="job.status === 'error'" class="err">Error: {{ job.error }}</div>
            <div class="logwrap" v-if="logTail.length">
              <div class="muted small" v-if="logHidden">… {{ logHidden.toLocaleString() }} earlier lines not shown</div>
              <pre class="log" ref="logBox">{{ logTail.join('\n') }}</pre>
            </div>
          </div>

          <div class="resultbox" v-if="result" ref="resultBox">
            <div v-if="jobWasRaw" class="warn">◆ high-fidelity raw mesh (not watertight, by design)</div>
            <div v-else :class="result.all_watertight ?? result.watertight ? 'ok' : 'warn'">
              {{ (result.all_watertight ?? result.watertight)
                ? (isMulti ? `✓ all ${outputs.length} watertight` : '✓ watertight')
                : '⚠ not watertight' }}
            </div>
            <div class="muted">{{ result.elapsed_s }}s total</div>

            <!-- One row per direction. Each carries its own .wind.json holding the
                 rotation back to true EPSG:3414. -->
            <table class="outputs" v-if="isMulti">
              <thead><tr><th>Wind from</th><th>Faces</th><th>MB</th><th></th></tr></thead>
              <tbody>
                <tr v-for="(o, i) in outputs" :key="o.filename"
                  :class="{ sel: i === selectedOutput }"
                  @mouseenter="highlightDir = o.wind_from_deg" @mouseleave="highlightDir = null">
                  <td>{{ o.wind_from_deg }}° <span v-if="!o.watertight" class="warn">⚠</span></td>
                  <td>{{ (o.faces / 1000).toFixed(0) }}k</td>
                  <td>{{ o.size_mb }}</td>
                  <td class="acts">
                    <button class="link" @click="showOutput(i)">3D</button>
                    <a :href="fileUrl(o.filename)" download><button class="link">↓</button></a>
                  </td>
                </tr>
              </tbody>
            </table>
            <div class="muted" v-else>{{ result.faces.toLocaleString() }} faces · {{ result.size_mb }} MB</div>

            <div class="row">
              <!-- Always offer the bundle: the STL alone carries no metadata, so a bare
                   .stl download loses the domain, the options and (for a wind run) the
                   rotation needed to georeference anything computed from it.
                   "View in 3D" used to sit here too -- removed, the 3D STL toggle in the
                   header already does exactly that and is always visible. -->
              <a :href="bundleUrl" download class="btnlink">
                <button class="primary">
                  Download {{ isMulti ? `all ${outputs.length}` : 'STL' }} + sidecars
                </button>
              </a>
              <a v-if="!isMulti" :href="downloadUrl" download class="btnlink">
                <button>STL only</button>
              </a>
            </div>
            <p class="hint muted" v-if="isMulti">
              Each STL is <b>already rotated</b> so the flow is +Y (inlet <code>ymin</code>).
              Its <code>.wind.json</code> holds the rotation back to true EPSG:3414 — apply it
              only when making a georeferenced product, never before binning particle tracks.
            </p>
          </div>
        </section>
      </aside>

      <main class="viewport">
        <OrthoWebGLView v-show="view === 'map'" ref="mapRef"
          :footprints="footprints" :ring="ring" :keptIds="keptIds" :crossingIds="crossingIds"
          :overlays="overlays" @click="onMapClick" />
        <div class="legend" v-if="view === 'map' && windPlan">
          <span><i class="sw roi" />area of interest</span>
          <span><i class="sw env" />built envelope</span>
          <span><i class="sw rect" />wind domain</span>
          <span><i class="sw arr" />flow</span>
          <span class="muted">{{ highlightDir == null
            ? 'hover a direction to isolate it' : `wind from ${highlightDir}°` }}</span>
        </div>
        <StlViewer v-if="view === 'result'" :url="viewerUrl" />
        <div class="viewbadge" v-if="view === 'result' && isMulti">
          wind from <b>{{ outputs[selectedOutput].wind_from_deg }}°</b> · flow is +Y (inlet ymin)
        </div>
      </main>
    </div>

    <div v-if="showAdvanced" class="modal-backdrop" @click.self="showAdvanced = false">
      <div class="modal">
        <h3>Advanced build settings</h3>
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
.small { font-size: 12px; }
.stale {
  padding: 7px 14px; background: rgba(255, 159, 67, 0.14);
  border-bottom: 1px solid var(--border); color: var(--accent-2); font-size: 13px;
}
.opt { font-size: 11px; text-transform: none; letter-spacing: 0; }
.toggle { align-items: center; gap: 8px; margin-top: 0; font-size: 14px; }
.hgroup { margin-top: 12px; display: flex; flex-direction: column; gap: 6px; }
.bufgrid { margin-top: 8px; display: flex; flex-direction: column; gap: 4px; }
.bufgrid label { display: flex; align-items: center; justify-content: space-between; font-size: 13px; }
.bufgrid input { width: 90px; }
.outputs { width: 100%; border-collapse: collapse; font-size: 12px; margin: 4px 0; }
.outputs th { text-align: left; color: var(--muted); font-weight: 500; padding: 2px 4px; }
.outputs td { padding: 2px 4px; border-top: 1px solid var(--border); }
.outputs tr.sel { background: rgba(74, 158, 255, 0.12); }
.outputs .acts { text-align: right; white-space: nowrap; }
.outputs .acts button { padding: 0 4px; font-size: 12px; }
/* The map legend and 3D badge are UI chrome over a full-bleed canvas, so they
   must opt out of the `.viewport > *` absolute-inset rule above. */
.legend, .viewbadge {
  position: absolute; inset: auto auto 10px 10px; z-index: 5;
  display: flex; gap: 12px; align-items: center; flex-wrap: wrap;
  background: rgba(12, 18, 30, 0.82); border: 1px solid var(--border);
  border-radius: 6px; padding: 5px 10px; font-size: 11px; pointer-events: none;
}
.legend span { display: flex; align-items: center; gap: 4px; }
.sw { width: 12px; height: 3px; border-radius: 2px; display: inline-block; }
.sw.roi { background: #ffd23f; } .sw.env { background: #5a7fb5; }
.sw.rect { background: #4a9eff; } .sw.arr { background: #ff9f43; }
.viewbadge { inset: 10px 10px auto auto; font-size: 12px; }
.job { margin-top: 12px; }
.jobstage { display: flex; align-items: center; gap: 8px; font-size: 14px; }
/* Buttons in a row were different heights because one sat inside an <a> and one did
   not, so they inherited different line boxes. Make the wrapper contribute nothing. */
button.danger {
  background: transparent; color: var(--danger, #ff6b6b);
  border: 1px solid var(--danger, #ff6b6b);
}
button.danger:not(:disabled):hover { background: rgba(255, 107, 107, 0.1); }

.btnlink { display: inline-flex; text-decoration: none; }
.btnlink > button { width: 100%; }
.row > button, .row > .btnlink > button { min-height: 32px; }

/* Every button now has a visible hover and press state -- previously they just sat
   there, which reads as disabled. */
button:not(:disabled):hover { border-color: var(--accent); }
button:not(:disabled):active { transform: translateY(1px); }
button.primary:not(:disabled):hover { filter: brightness(1.12); }

/* One accent for the primary action, neutral for the secondary -- the two download
   buttons were two different blues with no hierarchy between them. */
.btnlink > button:not(.primary) {
  background: transparent; color: var(--muted); border: 1px solid var(--border);
}
.btnlink > button:not(.primary):hover { color: var(--fg); }

.cellsize { display: flex; align-items: center; gap: 6px; }
.cellsize input { width: 80px; }

.ibtn {
  width: 16px; height: 16px; padding: 0; margin-left: 6px; border-radius: 50%;
  font-size: 10px; font-style: italic; line-height: 1; vertical-align: middle;
  background: transparent; color: var(--muted); border: 1px solid var(--border);
}
.ibtn:hover { color: var(--accent); border-color: var(--accent); }
.pop {
  margin-top: 6px; padding: 8px 10px; font-size: 11.5px; line-height: 1.5;
  color: var(--muted); background: var(--bg);
  border: 1px solid var(--border); border-left: 2px solid var(--accent); border-radius: 6px;
}
.pop p { margin: 0 0 6px; }
.pop p:last-child { margin-bottom: 0; }

.logwrap { margin-top: 8px; }
.log {
  margin: 0; font-size: 11px; color: var(--muted); white-space: pre-wrap;
  background: var(--bg); border: 1px solid var(--border); border-radius: 6px;
  padding: 8px; max-height: 260px; overflow-y: auto;
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

.bboxrow { display: flex; gap: 6px; margin-top: 8px; }
.bboxrow input { flex: 1; min-width: 0; font-family: ui-monospace, monospace; font-size: 11px; }
.bboxhint { margin-top: 4px; }
.bboxhint code { font-size: 11px; }
.bboxhint .warn { color: #ff9f43; }
</style>
