# `dose/` — Fluent plume → OpenMC dose maps

What this folder does, in what order, in which Python, and where the traps are.
Companions: [`NOTES.md`](NOTES.md) is the run log and the design decisions with their
reasons; [`PARTICLES_XML_FORMAT.md`](PARTICLES_XML_FORMAT.md) is the Fluent export format.

**Status: a validated research pipeline, not a product.** Every physics claim in here has
been checked against something external — an analytic point-source solution (0.6%),
DAGMC's own `check_watertight`/`overlap_check`, a leakage of exactly 0.00000 for a source
buried in the terrain slab. What it is *not* is packaged: hardcoded paths, three Python
environments, a manual Fluent step in the middle, and no test suite. That is a deliberate
ordering, not neglect — the interface changes again when the buffer domain lands, and
productising against an interface that is about to move is wasted work.

---

## The pipeline

```
  SBG  ──► cfd_watertight.stl ──► [Fluent, manual GUI] ──► particles3.xml
   │              │                                              │
   │              │  stl_to_h5m.py  (pymoab env)                 │
   │              ▼                                              │
   │           cfd.h5m + cfd.transform.json                      │
   │              │                                              │
   │              └──────────────┬───────────────────────────────┘
   │                             ▼
   │                     run_dose.py          (airborne cloud source)
   │                     run_groundshine.py   (surface deposit source)
   │                             │  writes materials/geometry/settings/tallies.xml
   │                             ▼
   │                       openmc  (system CLI)  ──► statepoint.*.h5
   │                             │
   └── data/dtm.tif ────────────►│  read_dose.py   ──► dose_map.csv + .png
                                 │        │
                                 │        ├─ plot_dose.py     (re-plot, no re-run)
                                 │        └─ compare_dose.py  (cloud vs ground + ratio)
```

The two `run_*.py` scripts are siblings, not a chain: **same geometry, same tally
convention, same reader — only the source term differs.** Cloudshine emits from the
airborne residence-time field; groundshine emits from a surface deposit computed in
post-processing from near-surface concentration (`flux = v_d · C_air`). Deposition is
*not* computed in Fluent, and `NOTES.md` explains at length why `reflect` walls plus
post-processing beats both `trap` and a sticking-probability UDF.

---

## Three environments — the first thing that trips people

| what | interpreter | why |
|---|---|---|
| everything except the two below | project `.venv` | has the OpenMC **Python API**, but no compiled `libopenmc.so` |
| `stl_to_h5m.py` | `~/tools/mamba/root/envs/moabpy/bin/python` | needs `pymoab`, which is conda-only |
| the transport itself | `/usr/local/bin/openmc` | the compiled CLI, spawned by `--run` with `OPENMC_CROSS_SECTIONS` set |

So the `.venv` can *write* OpenMC XML but cannot *run* transport, and the moabpy env can
build a `.h5m` but has none of the rest. Nuclear data lives at `/home/quentin/nuclear_data`
(84 photon elements, so real NIST concrete and dry air — no proxies).

Typical full cycle:

```bash
~/tools/mamba/root/envs/moabpy/bin/python dose/stl_to_h5m.py \
    data/openmc_data/cfd_watertight.stl -o data/openmc_data/cfd.h5m
.venv/bin/python dose/run_dose.py --run          # ~8 min
.venv/bin/python dose/read_dose.py
.venv/bin/python dose/plot_dose.py --hist
```

---

## The three coordinate frames

This is the single most important idea in the folder, and every georeferencing bug it has
had came from confusing two of them.

| frame | units | what lives in it | transform out |
|---|---|---|---|
| **World** EPSG:3414 | m | the ROI polygon, `data/dtm.tif`, the final dose map | — |
| **Rotated** (wind → +Y) | m | the STL, the Fluent case, the particle tracks, `lo_m`/`hi_m` | `<stem>.wind.json` |
| **DAGMC-local** | **cm** | the `.h5m` and everything OpenMC sees | `<stem>.transform.json` |

They compose by stem and chain in one direction:

```
world  ──.wind.json──►  rotated  ──.transform.json──►  h5m-local cm
p_rot = R(ψ)(p_world−c)+c                p_cm = (p_rot − origin_m)·100
```

Two rules that follow, both already load-bearing:

- **Never un-rotate before binning.** When the domain is wind-aligned the STL is exported
  already rotated, so the geometry OpenMC ray-traces lives in the rotated frame. Un-rotating
  tracks first would put the source where the geometry is not, and every photon would start
  in a vacuum — producing a plausible-looking near-zero map, the worst failure mode there is.
  Un-rotation belongs at **map export only** (`read_particle_tracks.to_world`).
- **A `.h5m` carries no unit metadata and OpenMC assumes centimetres**, but the STL is in
  metres. Loaded raw, a 400 m domain reads as 400 cm and a 30 cm wall becomes 3 mm, with no
  error anywhere. The source mesh and every tally mesh must use the same transform.

### Frame-awareness, as of 2026-08-31

| script | wind-aware? |
|---|---|
| `read_particle_tracks.py` | yes — `load_wind` / `to_world` / `to_rotated` |
| `run_dose.py` | yes — `resolve_extent` reads the sidecar, records the frame in `run_meta.json` |
| `read_dose.py` | yes — un-rotates for the DTM, stays rotated for `mesh.contains()` |
| `run_groundshine.py` | yes — `--wind`, shares `resolve_extent` |
| `plot_dose.py`, `compare_dose.py` | yes — reshape on `x_rot`/`y_rot`, label the frame honestly |
| `stl_to_h5m.py` | n/a — frame-agnostic by construction |

The two marked **no** were a real defect on **every** wind bearing except 180°, now fixed.
Worth stating precisely, because the obvious guess is wrong: `psi = (wind_from + 180) % 360`,
so `wind_from = 0` (from the north, flowing south) is a **180° rotation**, not the identity.
Only `wind_from = 180` needs no rotation at all. Measured on a real 2.76 km² envelope, the
un-fixed code would have sampled the DTM up to 2,370 m from the right place, for a mean
ground error of 8.8–12.2 m and a max of 40 m — the full terrain range of the domain.
The fix needs both frames simultaneously: rotated for tally indexing and `mesh.contains()`
(the STL is rotated), world for the DTM sample and the CSV/plot.

---

## File by file

| file | env | what it does |
|---|---|---|
| **`read_particle_tracks.py`** | any | Reads the Fluent/CFD-Post `<ParticleTracks>` XML. Also the home of the frame helpers, deliberately pure numpy+stdlib so it imports in the conda envs. |
| **`stl_to_h5m.py`** | moabpy | STL → DAGMC `.h5m` + `.transform.json`. |
| **`run_dose.py`** | `.venv` | Cloudshine. Residence-time field → `MeshSpatial` source → OpenMC XML, `--run` to transport. |
| **`run_groundshine.py`** | `.venv` | Groundshine. Same geometry/tally/reader; deposits the plume onto terrain in post, then emits from the ground. |
| **`read_dose.py`** | `.venv` | Statepoint → terrain-following dose map (CSV + PNG). |
| **`plot_dose.py`** | `.venv` | Re-plots the CSV. Decoupled from the statepoint on purpose. |
| **`compare_dose.py`** | `.venv` | Cloudshine vs groundshine on **one** shared scale, plus a diverging ratio panel. |

### The coupling, in one line

Fluent writes roughly one track point per control-volume crossing, so the time between
consecutive points of a track *is* the residence time in the cell traversed:

```
activity in cell i = (Q/N) · Σdt_i   [Bq]      concentration = that / cell volume
```

This holds only because every tracer is **equal weight**, which holds only because the
injection is a **single point source**. A surface injection off the inlet is face-count
weighted (it follows mesh refinement, not mass flux) and would need a per-track correction.

`Q` is one scalar applied at the very end, and OpenMC normalises `strengths` internally, so
it only ever rescales the final tally.

### Four traps that have each cost real time

1. **`photons_per_s ≠ Q · yield`.** `Q` is a release *rate*; what is decaying is the
   steady-state *inventory*, `Q · (mean residence time)`. Using `Q·yield` under-predicts by
   that residence time — a factor of **91** here.
2. **OpenMC mesh bins are X-fastest**, so the flat array is `[z][y][x]`. `reshape(nx,ny,nz)`
   silently scrambles the map — it does not raise, because the element count still matches.
   Correct form: `.reshape(nz,ny,nx).transpose(2,1,0)`. A 2D (`nz=1`) tally is also affected;
   it comes out transposed, which a square domain hides.
3. **The tally must follow the terrain.** Ground runs 20→60 m here, so a slab at one constant
   elevation is underground over most of the map and reports the dose inside rock (~2× low).
   Hence a 3D column stack plus per-column DTM lookup — and linear interpolation between z
   bins, because snapping to the containing bin renders as regular banding.
4. **Source births inside the solid.** `MeshSpatial` samples uniformly within a cell, so a
   cell straddling a building emits throughout it (2.35% of samples at a 10 m bin) even though
   all its residence time came from air. Fixed with `domains=[air]` rejection — safe from
   infinite rejection because a cell fully inside the solid has zero residence time.

---

## What is validated, and what is assumed

**Validated against something external:**

- Absolute normalisation — OpenMC uncollided 19.22 µSv/h vs analytic `S·e^(−μr)/4πr²·D` =
  19.33, i.e. **0.6%**. Confirms the per-source-particle convention, the ICRP-116 application,
  the cm³ volume division and the `pSv cm³/src → µSv/h` chain all at once.
- Geometry — `check_watertight` 0/0/0, `overlap_check` clean, and leakage **0.00000** for a
  source buried in the slab (~300 mfp of concrete). That is what proves the `mat:` assignment,
  the shared-surface topology and the ray tracing.
- The 0.096% deposited fraction independently confirms the non-depleting assumption used to
  justify computing deposition in post.

**Assumed, and stated rather than hidden:** Cs-137 monoenergetic; dry deposition only, ground
only, no roofs or walls; no decay, weathering or resuspension; `reflect` walls (correct for a
noble gas, an approximation for an aerosol); steady RANS.

**The finding that shapes interpretation:** the photon mfp in air at 662 keV is **106.5 m**
against a 400 m domain, so cloudshine is nearly flat (3.5× across the map) — every receptor
integrates most of the airborne inventory, not its local concentration. Groundshine behaves
oppositely (1771× range) because deposition is local. Mesh and terrain fidelity matter more
than plume fidelity for cloudshine; the reverse for groundshine.

---

## Known gaps

- `run_dose.py` uses `domain_rotated` for the source **and** the tally. With a buffer domain
  those should differ: source over the whole domain (truncating it loses real dose — the
  photon mfp in air is 106.5 m), tally over `core_roi.rotated_bounds` (tallying the buffer
  too costs ~2.8× relative error for cells nobody reads). See `NOTES.md`, "Dose with a
  buffer domain".
- 7 self-colliding facet pairs in the STL lose rays at ~5e-6. Handled by raising
  `max_lost_particles`, not by pretending it is absent. Do **not** "fix" with meshlib
  `fixSelfIntersections` — it is voxel-based and requantises the whole mesh.
