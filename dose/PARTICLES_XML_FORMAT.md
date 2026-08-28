# `particles.xml` — the CFD-Post `<ParticleTracks>` format

Investigation of `data/openmc_data/particles.xml` (92,744,720 bytes), the Fluent/CFD-Post
particle-track export that is meant to become the OpenMC source term. Everything below is
measured from that file, not from documentation. Reference reader:
[`read_particle_tracks.py`](read_particle_tracks.py) (parses the whole file in 2.9 s).

---

## 1. Container

```xml
<?xml version="1.0" encoding="iso-8859-1" standalone="no" ?>
<ParticleTracks>
  <Format version="1.0" />
  <Items>   ... column schema, once ...        </Items>
  <Tracks>  ... <Section> chunks of rows ...   </Tracks>
</ParticleTracks>
```

It is a **column store wrapped in XML**. `<Items>` declares the columns; each `<Section>`
is a horizontal slab of rows, and inside it each `<Data item="N">` is *one column* for
*those* rows. Nothing is per-particle-nested — you reassemble tracks yourself by grouping
on the Particle ID column.

### `<Items>` — the schema in this file

| id | name | type | units | notes |
|----|------|------|-------|-------|
| 0 | Injection | OPTION | | 1 option: `massless` |
| 1 | Particle ID | INTEGER32 | | global, 0…38 599, monotonic |
| 2 | Region | OPTION | | 8 options (see below) |
| 3 | Periodic Side | INTEGER32 | | always 0 |
| 4 | Particle Time | FLOAT | s | since that particle's release |
| 5 | Particle X Position | FLOAT | m | `component="X" componentOf="Particle Position"` |
| 6 | Particle Y Position | FLOAT | m | |
| 7 | Particle Z Position | FLOAT | m | |

`type="OPTION"` means the data column stores an integer **code**, and the `<Option>`
children map code → label. The codes are Fluent zone IDs, not 0-based indices:

```xml
<Option id="28387" data="fluid-region-1"   name="fluid" />
<Option id="24834" data="solid-1"          name="wall" />
<Option id="24831" data="tunnel-xmax"      name="velocity-inlet" />
<Option id="24829" data="tunnel-xmin"      name="pressure-outlet" />
<Option id="24833" data="tunnel-ymax"      name="symmetry" />   (+ ymin, zmax)
<Option id="28389" data="interior--fluid-region-1" name="interior" />
```

Note `data` is the zone name and `name` is the zone *type* — the opposite of what the
attribute names suggest.

### `<Section>` — row chunks

```xml
<Section id="0" length="10000">
  <Data item="0" constant="true"                  dataFormat="ASCII">0</Data>
  <Data item="1" minimum="0" maximum="89"         dataFormat="ASCII">0 0 0 … 89</Data>
  <Data item="4" minimum="0" maximum="135.351"    dataFormat="Base64/LE">AAAAAPmf…</Data>
  …
</Section>
```

- **342 sections**, 341 of `length="10000"` plus a final `length="5761"` →
  **3,415,761 rows** total.
- Sections are purely a writer chunk size. **A track routinely spans a section boundary**
  (section 0 holds IDs 0–89, section 1 holds 89–180 — note 89 appears in both). Never
  treat a section as a track.
- `minimum`/`maximum` are per-column-per-section hints. I checked them against the decoded
  arrays and they agree exactly, so they are trustworthy for cheap pre-scans (e.g. finding
  which sections touch a bounding box without decoding).
- `constant="true"` → the payload holds **one** value for the whole section, not `length`
  values. Broadcast it. Items 0, 2 and 3 are constant in every section here.

### Two payload encodings

`dataFormat="ASCII"` — whitespace-separated decimal, single line, wrapped in a newline and
indentation you must strip.

`dataFormat="Base64/LE"` — base64 of a raw little-endian array, single line, no internal
line breaks.

**`FLOAT` is `float32`, not `float64`.** Confirmed by arithmetic — 53 336 base64 chars →
40 000 bytes → 40 000/10 000 = **4.000 bytes per element** — and by round-trip: decoding
item 5 of section 0 as `<f4` reproduces `minimum="21712.529" maximum="22112.289"` to the
last printed digit, whereas `<f8` gives 5 000 values of nonsense. This costs you ~1 mm of
precision at these EPSG:3414 coordinates (~22 000 m), which is irrelevant here but worth
knowing before you diff positions.

Half the file's 1 368 `<Data>` blocks are ASCII and half Base64/LE — the ASCII ones are the
three constants plus Particle ID (which base64 would not compress meaningfully anyway).

---

## 2. What is actually in this file

| | |
|---|---|
| Track points | 3,415,761 |
| Tracks | 38,600 (IDs 0…38 599, contiguous, no gaps) |
| Points per track | min 2, median 81, mean 88.5, p95 217, max 1166 |
| Row order | already sorted by Particle ID, then by Particle Time |
| Spatial extent | X 21 712.5…22 112.5, Y 30 255.4…30 655.4, Z 20.0…238.8 (m, EPSG:3414) |
| Time extent | 0…2336.4 s; per-track end time p50 79.8 s, p95 225.2 s |
| Total residence | 3.754 × 10⁶ particle·s |

**Step semantics — the important one.** Consecutive points on a track are *not* uniform
time samples. Measured step statistics:

```
step length (m)   p1 0.039   p25 1.45   p50 3.49   p75 6.28   p99 18.1
step dt (s)       p1 0.010   p25 0.378  p50 0.799  p75 1.49   p99 5.70   max 130
implied speed     p1 0.49    p25 3.31   p50 4.78   p75 5.46   p99 7.88
```

Median step length 3.5 m against a mesh of 212 230 cells in ~3.5 × 10⁷ m³ (≈5.5 m nominal
cell size), and implied speed clustering at 4.8 m/s against a 5 m/s inlet. That is
**one point per control-volume crossing**, which is exactly the semantics you want: `Δt`
between consecutive points *is* the residence time in the cell just traversed. No
resampling or interpolation is needed to get a concentration field.

Minor artefacts: 285 steps (0.01 %) have `dt == 0` and 1 616 have zero length — guard
against divide-by-zero if you compute speeds, otherwise ignore.

---

## 3. What is **not** in this file (matters for OpenMC)

1. **No mass, no mass flow rate, no diameter, no concentration.** Injection type is
   `massless`, so each track is a pure kinematic tracer. Fluent's usual per-stream
   `ṁ = ρ·v·A` does not exist here.
2. **No per-particle weight, and the release is not uniform.** Start points are on the
   inlet plane (X ≈ 22 111–22 112.5, 88 distinct X values = inlet face centroids), but
   they are **face-count weighted, not flux- or area-weighted**. The 8×8 Y–Z histogram of
   release points is heavily bottom-loaded (6 000 in the densest near-ground bin vs 50 in
   the sparsest upper bin) because the mesh is refined near the ground. So *particle count
   is not proportional to emitted mass* and you cannot treat tracks as equal-weight
   samples without correcting for it. The correction needs inlet face area × normal
   velocity, **which is not in this file** — it has to come from a separate Fluent surface
   export.
3. **No termination reason / fate flag.** The `Region` column is `constant="true"` and
   equal to `fluid-region-1` in all 342 sections, so it never records the boundary a
   particle hit. Fate has to be inferred geometrically from the last point of each track.
4. **No velocity, turbulence, or per-step cell ID.** Position and time only.

---

## 4. Three real problems the data exposes

Fate reconstructed from each track's final point (2 m tolerance to each boundary):

| fate | tracks | share |
|---|---|---|
| reached outlet `tunnel-xmin` (escape) | 33 566 | 87.0 % |
| **terminated on the inlet plane** | 5 032 | **13.0 %** |
| stopped in the interior / on a wall | 2 | 0.005 % |
| left through a symmetry plane | 0 | 0 % |

**(a) 13 % of tracks are stillborn at the inlet.** 5 034 tracks have ≤2 points and end
where they started. This lines up exactly with the report: `tunnel-xmax` (the inlet, and
the injection surface) has `Discrete Phase BC Type = trap`. Particles released on that
face are deleted by the face's own DPM boundary condition before they travel. One eighth
of the release is being thrown away, and non-uniformly. Fix: set the inlet's DPM BC to
`escape`, or release from a surface slightly downstream of it.

**(b) Nothing deposits.** Exactly 2 of 38 600 tracks terminate anywhere other than a domain
boundary. The report has `solid-1` (buildings + terrain) at
`Discrete Phase BC Type = reflect`, so particles bounce off every surface and never
deposit. You described the setup as "set to trap" — that is the setting on the *inlet*,
not on the walls, and they are the wrong way round for a dose calculation. With reflecting
walls there is **no deposition field at all**, so no groundshine source term. Cloudshine
from the airborne field still works.

**(c) 0.36 % of sampled points sit inside the solid.** 109 of a random 30 000 points test
`inside` against `cfd_watertight.stl`. Small, and consistent with mesh-vs-STL snapping
tolerance rather than tracks tunnelling through buildings — but it means you must not use
raw STL containment to classify air vs solid cells without a tolerance band.

---

## 5. Turning this into an OpenMC source

The residence-time binning works and is cheap. Demo on a 10 m Cartesian grid (41×40×22 =
36 080 cells) using the midpoint of each step weighted by its `dt`:

```
occupied cells        14 521 (40.2 %)
total binned          3.754e6 particle-s   (= sum of all track durations, exactly)
per-cell residence    p50 136   p90 619   p99 1.82e3   max 5.12e3
dynamic range         max / p50 = 38
53.7 % of all residence time is in the lowest 30 m
```

That array is the shape `openmc.stats.MeshSpatial(mesh, strengths=...)` wants. The
conversion chain is:

```
Σ dt per cell  ──(÷ cell volume)──>  ∝ concentration
               ──(× emitted activity per particle)──>  Bq/cell  ──>  strengths
```

The missing factor is the "per particle" part — see §3.2. Two options:

- **Get the weights.** Export the inlet faces (area + normal velocity) from Fluent and
  weight each track by the `v·A` of its release face, matched on the 88 distinct start-X /
  start-YZ locations.
- **Change the injection.** A `surface` injection off the whole inlet models a uniform
  incoming background, which is not a radionuclide release anyway. A point or small-area
  injection at the actual release location, with a real mass flow rate, gives you weights
  for free and is closer to the physical problem. Worth deciding before re-running.

Either way it is a per-track scalar multiplying the `dt`s — the binning code does not
change.

---

## 6. Review of `Report - ADR2.html`

The setup is broadly what you described and is sound: 3D pressure-based, **steady**,
**SST k-ω**, air at 1.225 kg/m³, coupled pressure-velocity, second-order upwind on
momentum/k/ω, 5 m/s velocity inlet at `tunnel-xmax`, pressure outlet at `tunnel-xmin`,
symmetry on ±Y and +Z, no-slip wall on `solid-1`. Reference values are defaults and fine
for incompressible flow. 212 230 cells / 949 600 faces / 576 937 nodes, 120 iterations,
582 s on 1 core, 4.0 GB peak.

Five things I would flag, in order:

1. **It has not converged.** `continuity` sits at 1.72 × 10⁻² against a 10⁻³ criterion —
   17× over — and `k` at 1.78 × 10⁻³ is also over. Only the three velocity components and
   ω passed. 120 iterations is very few for a bluff-body urban case; steady RANS around
   sharp-edged buildings often stalls around 10⁻² because the real flow is unsteady. Run
   it much longer first, and if continuity plateaus rather than falls, that is the signal
   to either accept it explicitly or move to URANS.
2. **Wall DPM BC is `reflect`, not `trap`.** See §4(b). This is the difference between
   having and not having a deposition source term.
3. **Inlet DPM BC is `trap`.** See §4(a). Deletes 13 % of the release at t = 0.
4. **Mesh quality is marginal.** Min orthogonal quality 0.0423 (below the usual 0.1
   warning line) and max aspect ratio 204. Not fatal for a first-order dispersion answer,
   but it is a plausible contributor to the continuity residual in (1) — worth checking
   *where* those cells are before blaming the physics.
5. **The domain is too small for a proper CFD box.** 400 × 400 m with the tallest building
   ~49 m above ground. Standard urban-CFD guidance wants ≥5H upstream and ≥15H downstream
   of the region of interest, i.e. ~245 m and ~735 m here — so the buildings should sit
   inside a larger cutout, with the 400 m box as the region of interest rather than the
   whole domain. Vertical headroom is fine (219 m of air ≈ 4.5H). This one is a
   known-and-flagged pipeline issue, not new.

Items 2 and 3 are one-click fixes and change the physics materially. Item 1 is the one
that decides whether any of the rest is worth trusting.

---

## 7. Reading it

```bash
.venv/bin/python dose/read_particle_tracks.py data/openmc_data/particles.xml
```

```python
from dose.read_particle_tracks import read_items, read_tracks, by_name, residence_time
items = read_items(path)
c     = by_name(items, read_tracks(path, items))
pid, t = c["Particle ID"], c["Particle Time"]
xyz    = np.column_stack([c[f"Particle {k} Position"] for k in "XYZ"])
mask, dt = residence_time(pid, t)          # dt aligned to rows where mask is True
```

92 MB parses in 2.9 s and about 200 MB of RAM. `iterparse` + `el.clear()` per section
keeps it linear; do not build a full DOM.
