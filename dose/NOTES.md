# Fluent → OpenMC working notes

Companion to [`PARTICLES_XML_FORMAT.md`](PARTICLES_XML_FORMAT.md) (file format) and
[`read_particle_tracks.py`](read_particle_tracks.py) (reader). This file holds the run log
and deferred design ideas.

---

## Run log

Domain: Kent Ridge, 400 × 394 m, ground z ≈ 20 m, tallest building ≈ 69 m, domain top
238.8 m. Mesh **identical across all three runs**: 212,230 cells / 949,600 faces /
576,937 nodes, min orthogonal quality 0.0423, max aspect ratio 204.4.

| | run 1 | run 2 | run 3 |
|---|---|---|---|
| cores | 1 | 4 | 4 |
| iterations | 120 | 410 | 333 |
| iteration wall time | 582 s | 519 s | 374 s |
| continuity residual | 1.72e-2 ✗ | 7.70e-3 ✗ | **9.80e-4 ✓** |
| all other residuals | k ✗ | ✓ | ✓ (1e-5…1e-7) |
| wind direction | −X (in xmax, out xmin) | +Y (in ymin, out ymax) | +Y |
| turbulence spec | TI 5% / visc-ratio 10 | TI 5% / visc-ratio 10 | **TI 20% / length 15 m** |
| injection | inlet surface, 88 locations | 1 point, 20 000 tries | 1 point, 20 000 tries |
| inlet DPM BC | `trap` | `escape` | `escape` |
| wall DPM BC | `reflect` | `reflect` | `reflect` |
| track coarsening | (coarse) | 1 | 1 |
| tracks / points | 38,600 / 3,415,761 | 20,000 / 4,299,728 | 20,000 / 3,634,057 |
| stillborn tracks | 5,032 (13%) | 0 | 0 |
| fate | 87% escape | 100% escape | 100% escape |
| median step length | 3.49 m | 1.09 m | 1.50 m |
| **σ_z at 90 m** | — | **0.06 m** | **6.87 m** |
| **σ_x at 90 m** | — | 0.06 m | 6.52 m |
| residence < 5 m AGL | — | 0.44% | **3.28%** |
| residence < 10 m AGL | — | 1.19% | **8.81%** |
| mean plume height | — | 48.0 m | 45.8 m |

Reference for run 3: Pasquill class D at 100 m gives σ_y ≈ 8 m, σ_z ≈ 5 m. Run 3 sits
just above that, which is correct for urban roughness.

### Root causes found, in order

1. **Run 1 — inlet DPM BC was `trap`, and the injection surface *was* the inlet.** 13% of
   tracks were deleted at t = 0, non-uniformly. Fixed by `escape` + point injection.
2. **Run 1 — surface injection is face-count weighted, not flux weighted.** Release-point
   density followed near-ground mesh refinement, so particles were not equal-weight and
   `Σdt` was not ∝ concentration. Fixed for free by moving to a single point source: one
   release location ⇒ all tries equal weight ⇒ no correction needed, and no Fluent
   face-area export needed either.
3. **Runs 1–2 — `Turbulent Viscosity Ratio = 10` specified a 0.87 mm eddy with a 2.6 ms
   lifetime.** This is the big one. DRW was enabled and working correctly the whole time;
   it was faithfully applying a fresh random 0.25 m/s kick every 2.6 ms, each displacing
   the particle 0.65 mm before being replaced by an independent one. Uncorrelated steps
   that small cancel out. Predicted σ at 90 m from those settings: 0.076 m. Measured:
   0.060 m. Back-solved eddy diffusivity K = 1.0e-4 m²/s against a real urban boundary
   layer's 1–10 m²/s — **four to five orders of magnitude low**.

   Not `massless`, not gravity, not DRW being off. Fixed by switching to
   `Intensity and Length Scale`, derived from surface roughness rather than picked:

   ```
   u* = kappa·Uref / ln((zref + z0)/z0)      k = u*²/sqrt(Cmu)      l = kappa·(z + z0)
   ```

   z₀ ≈ 0.5 m (urban) at Uref = 5 m/s, zref = 10 m ⇒ TI ≈ 20%, l ≈ 15 m at plume height.
   Note `l = kappa·z` — the ABL length scale depends on **height, not roughness**, which is
   why l ≈ 15 m across every terrain class.

   Worth remembering how insensitive this is: across the entire plausible z₀ range
   (0.03 m open grass → 1.0 m dense urban, a 30× span) σ_z at 90 m only moves 9.5 → 22.7 m.
   You cannot get this meaningfully wrong by picking the wrong terrain class. The failure
   mode was using Fluent's generic internal-flow default in an atmosphere, not picking the
   wrong value within a sensible range.

4. **Run 3 converged faster and in fewer iterations than run 2.** Not a coincidence — the
   near-laminar approach flow (ω = 642 s⁻¹, k annihilated within ~1 m of the inlet) was a
   stiff, badly-conditioned state for the solver. Realistic turbulence stabilised RANS.
   Independent confirmation that the old setting was pathological rather than merely
   different.

### Settled decisions

- **Keep `massless`.** No mass is needed anywhere in Fluent. With N equal-weight tracers and
  a continuous release of Q Bq/s: `activity in cell i = (Q/N)·Σdt_i`, concentration = that
  ÷ cell volume. `Q` is one scalar chosen at the end, and OpenMC normalises `strengths`
  internally anyway, so it only ever affects final tally scaling.
- **Keep walls on `reflect`.** Correct for a noble-gas plume (Kr-85, Xe-133), which does not
  deposit. Binary `trap` = infinite deposition velocity: every particle brushing a building
  dies, depleting the airborne field and distorting cloudshine. Proper deposition needs a
  deposition-velocity model, not a checkbox. Cloudshine-only is a complete PoC; groundshine
  comes later.
- **Keep gravity off.** A 1 µm aerosol falls 0.23 cm crossing the whole domain (77 s);
  10 µm falls 23 cm. Settling loses to turbulence by ~4 orders of magnitude, and noble gases
  do not settle at all. Gravity would only matter for a buoyant (hot) release, which needs
  the energy equation.
- **Track coarsening must stay at 1.** With coarsening > 1 the `dt` between written points
  spans several cells and the residence-time-per-cell semantics break.

### Open / deferred

- Continuity converged, but 333 iterations of steady RANS around bluff bodies — spot-check
  the residual history is flat, not still descending.
- Mesh quality untouched since run 1 (min ortho 0.0423, max AR 204). Fine for a PoC.
- Release sits **exactly on the inlet plane** (y = 30261.170 = y_min). Works, but means zero
  upstream fetch: a receptor upwind of the release cannot be modelled, and gamma mean free
  path in air at 1 MeV is ~120 m, so upwind receptors do receive real dose. Move the source
  inboard once the buffer domain exists.
- Proper ABL inlet **profiles** (log-law U with height, plus matching k and ω profiles from
  u\*, and a ground roughness height to sustain them) rather than single uniform values.
  Not a PoC-effort change.

---

## STL -> DAGMC .h5m

Built by [`stl_to_h5m.py`](stl_to_h5m.py) from `cfd_watertight.stl`; consumed by
[`run_dose.py`](run_dose.py). Validated by DAGMC's own tooling, not by our own
checks:

    check_watertight   0/0 unmatched edges, 0/2 unsealed surfaces, 0/2 unsealed volumes
    overlap_check      No overlaps were found.

The input STL is 95,774 facets welding to 47,889 vertices, **every edge used exactly
twice**, one body, euler 2 (genus 0).

### The fused solid is the EASY case for DAGMC

Worth stating plainly, because it inverts an earlier assumption. DAGMC wants a cell
complex of volumes sharing surfaces, and the long-running fear was that our fused
mesh soup was wrong for it. The opposite is true: because terrain and every building
are fused into a single closed body, the whole model collapses to **one shared
surface**, and the imprint/merge problem that a per-building model would hit never
arises at all.

    Surface 1 = the STL       GEOM_SENSE_2 = [solid, air]   shared, two-sided
    Surface 2 = an outer box  GEOM_SENSE_2 = [air, 0]       tagged boundary:vacuum
    Volume 1  = solid  (mat:concrete)
    Volume 2  = air    (mat:air)

For `GEOM_SENSE_2 = [fwd, rev]` the facet normal points OUT of `fwd`; STL normals
point out of the solid, so `fwd = solid`. Verified numerically via the divergence
theorem rather than assumed.

### Units and origin -- the silent-failure trap

A `.h5m` carries no unit metadata and **OpenMC assumes centimetres**, but the STL is
in metres. Loaded raw, the 400 m domain reads as 400 cm and a 30 cm wall becomes
3 mm — with no error anywhere. The geometry also sits ~2.2e6 cm from the origin,
where the STL's float32 vertices only resolve to ~0.2 cm. Both handled explicitly:

    p_local_cm = (p_world_m - origin_m) * 100        origin_m = (cx, cy, 0)

written to `cfd.transform.json`. The source mesh and every tally mesh must use the
same transform or the dose map is silently mis-georeferenced — the class of bug that
looks perfectly fine until someone overlays it on a map.

### Geometry proven live, not assumed

Leakage fraction for a 1 MeV point source at three positions:

| source | leakage |
|---|---|
| air, at the CFD release point (35.5 m) | 60.5% |
| air, 5 m above ground, domain centre | 28.9% |
| **inside the terrain slab** | **0.00000 ± 0.00000** |

Concrete mfp at 1 MeV is ~6.8 cm and the slab source sits ~20 m deep (~300 mfp), so
total absorption is exactly right. This is what confirms the `mat:` group assignment,
the shared-surface topology and the ray tracing all actually work.

### Two real defects found, both handled

1. **7 self-colliding facet pairs** (of 95,774). `check_watertight` cannot see these
   — it checks edge matching, not whether facets pass through each other — so a
   closed, edge-manifold, overlap-free mesh can still lose rays. Symptom:
   `No intersection found with DAGMC cell 1`. Measured loss rate ~5e-6, statistically
   negligible, so `max_lost_particles` is raised rather than pretending the defect is
   absent. Do NOT "fix" with meshlib `fixSelfIntersections` — it is voxel-based and
   requantises the whole mesh.
2. **2.35% of source samples were born inside the solid.** `MeshSpatial` samples
   uniformly within a cell, so a 10 m cell straddling a building emits throughout it
   even though all its residence time came from air. This is a genuine modelling
   error independent of the crash — that activity is self-shielded, biasing the dose
   low — and it is also what drove rays into the defect above. Fixed with source
   rejection (`domains=[air]`). Safe from infinite rejection: a cell fully inside the
   solid has zero CFD residence time, hence zero strength.

### Nuclear data

`/home/quentin/nuclear_data` originally had 235 neutron + **19 photon** elements —
no N or Ar, so air was not buildable. Extended to **84 photon elements** (32 MB) via
`openmc_data_downloader -l ENDFB-7.1-NNDC -p photon -e all`; the 19 pre-existing
files were verified byte-identical to the downloaded ones before merging.
`cross_sections.xml` backed up to `cross_sections.xml.bak-before-photon`. Real NIST
ordinary concrete and dry air are now used, no proxies.

Harmless noise: `Negative value(s) found on probability table for nuclide Ar36` is a
*neutron* URR table warning, irrelevant to photon transport.

---

## End-to-end PoC result (2026-08-26)

`particles3.xml` -> source term -> DAGMC -> OpenMC -> terrain-following dose map.
Runs in ~8 min: `run_dose.py --run` then `read_dose.py`.

    source     3,634,057 points / 20,000 tracks, 1.828e6 particle-s
               40x40x23 @ 10 m, 4,439 cells occupied (12.1%), 0.00% outside
    transport  10 batches x 200,000 photons, 458 s, leakage 66.3%
    receptor   1.5 m above LOCAL grade; ground 20.0 .. 60.0 m
               1,377 outdoor columns / 223 inside a building

At Q = 1e12 Bq/s of Cs-137 (inventory 9.14e13 Bq, 7.78e13 photon/s):

| | uSv/h |
|---|---|
| peak | 170 (at 21917.5, 30490.4 — 230 m downwind, on the release axis) |
| median (outdoor) | 103 |
| p05 / p95 | 42.6 / 150.5 |

Relative error 3.4% at the peak, 6.8% dose-weighted. All 1,377 outdoor columns
carry signal.

### Bug: OpenMC mesh bins are ordered X-FASTEST

The flat tally array is `[z][y][x]`, so `reshape(nx, ny, nz)` **silently scrambles the
map** — it does not raise, because the element count still matches. Symptom was a
40 m periodic banding carrying 64% of the row variance, with no counterpart in the
building mask (period 400 m) or the terrain (400 m) and correlating with neither
(-0.02, -0.03). Confirmed via `tally.get_pandas_dataframe()`, whose first rows are
x=1,2,3,4 at y=1,z=1. Correct form:

    mean = t.mean.reshape(nz, ny, nx).transpose(2, 1, 0)

Scrambled vs correct: peak 353 -> 170 uSv/h, median 159 -> 103, and the peak moved
from a domain corner to the release axis. A 2D (nz=1) tally is also affected — it
comes out transposed, which a square domain hides.

Also interpolate linearly between z bins when picking the receptor height: snapping
to the containing integer bin makes the receptor jump a full bin wherever the terrain
crosses a boundary, which renders as banding.

### Normalisation validated against an exact solution

Not just self-consistent -- checked against a hand calculation. Point source in clear
air, receptor 50 m away:

| | uSv/h |
|---|---|
| OpenMC, **uncollided only** | **19.22 ± 1.0%** |
| analytic `S e^(-mu r)/(4 pi r^2) D` | **19.33** |
| OpenMC total (with scatter) | 29.74 |

0.6% agreement, inside the statistical error, and the total/uncollided ratio of 1.55
is a sensible air buildup factor for mu*r = 0.47. This confirms OpenMC's
per-source-particle convention, the ICRP-116 coefficient application, the cm^3 volume
division and the `pSv cm^3/src -> uSv/h` chain all at once.

### The finding that matters most: the dose map is nearly flat

The photon mfp in air at 662 keV is **106.5 m**, and the domain is 400 m — under
**4 mfp**. Every receptor therefore integrates over most of the airborne inventory,
not its local concentration, so the dose field is heavily smoothed:

Spatial variation is only **3.5x** (p05 42.6 -> p95 150.5). There IS a real plume
footprint — a bright ridge on the release axis fading east and west, with visible
shadows around buildings — but it is broad and soft rather than sharp, and the
row/column means are dominated by a single 400 m monotonic trend (43% / 83% of
variance) with no shorter-scale structure.

(The earlier claim that structure tracked ground elevation was an artefact of the
scrambled reshape; the corrected correlation is -0.05, i.e. none.)

Consequences worth carrying forward:
1. For **cloudshine**, plume detail is smoothed away; geometry and terrain drive the
   spatial structure. The mesh fidelity work matters more here than plume fidelity.
2. A 400 m domain cannot resolve plume structure *in dose* — another argument for the
   buffer domain, this time radiological rather than fluid-dynamic.
3. **Groundshine would behave oppositely** (deposition is local), so it would show the
   plume footprint sharply — but it needs `trap` walls plus a deposition-velocity
   model, not the current `reflect`.

### Two normalisation bugs, both found by cross-checking

1. **`photons_per_s = Q * yield` was wrong by 91x.** Q is an activity RELEASE RATE
   [Bq/s]; what is actually decaying is the steady-state INVENTORY,
   `Q * mean residence time` = `(Q/N) * sum(dt)`. Using `Q*yield` silently
   under-predicts by the mean residence time (91.4 s here). Fixed in `run_dose.py`;
   it is a pure scalar so it does not require a re-run.
2. A cm^2 -> m^2 slip (1e8) in the scratch point-kernel benchmark, which is what made
   OpenMC first appear ~1e9 low. Bug 1 surfaced only because that same script printed
   `total 7.78e13 photon/s (check: Q*Y=8.51e11)` and the two disagreed.

Standing lesson: the semi-infinite cloud formula is the WRONG benchmark for an
elevated plume — it assumes the receptor is immersed. Use a point-kernel integration
over the real concentration field, or an exact point-source case, instead.

### The tally must follow the terrain

The terrain runs 20 -> 60 m here, so a tally slab at one constant elevation is
underground over most of the map and reports the dose inside rock (measured: peak
168 vs 365 uSv/h, i.e. ~2x low). `run_dose.py` now tallies a 3D column stack and
`read_dose.py` samples `data/dtm.tif` per column to pick the bin at local grade +
receptor height, masking columns whose receptor lands inside a building rather than
averaging shielded in-wall cells into the statistics.

Note the extreme low tail is noise, not shadow: the 13 cells below 1 uSv/h have a
median relative error of 66%, versus 5.6% for cells above 50.

---

## Groundshine (2026-08-27)

`run_groundshine.py` -> `read_dose.py --dir data/openmc_data/gs_run` -> `compare_dose.py`.
Same DAGMC geometry, same tally convention, same reader — only the SOURCE changes.

Deposition is computed in POST, not in Fluent. The CFD ran `reflect`, so no track ever
ends on a surface; that is deliberate (`trap` is the worse extreme — it deletes every
particle that brushes a wall, depleting the airborne field and corrupting cloudshine
too, and a UDF sticking probability is grid- and DRW-dependent, so it is not v_d).

    deposition flux [Bq/m2/s] = v_d * C_air(1 m above local grade)
    surface activity [Bq/m2]  = flux * duration

At v_d = 1e-3 m/s (≈1 µm aerosol), 1 h release, Q = 1e12 Bq/s:

    deposited 3.446e12 Bq of 3.6e15 released = 0.096%
    source points 8,960 of 25,600 carry deposit; 701 dropped as buried; 8,259 kept
    surface activity 3.301e12 Bq -> 2.809e12 photon/s
    leakage 29.3% (vs 66.3% for the airborne source — ground emits into the terrain)

**0.096% deposited independently confirms the non-depleting assumption** (predicted
~0.16% from v_d/mixing-depth x transit time). Deposition is a diagnostic, not a sink.

| | cloudshine | groundshine |
|---|---|---|
| peak | 170 | **526 uSv/h** |
| median (outdoor) | **103** | 7.27 uSv/h |
| p95/p05 | **3.5x** | **1771x** |
| rel. error, dose-weighted | 6.8% | 1.9% |

### The two are complementary, and groundshine is the one that localises the release

Cloudshine is nearly flat because the 662 keV photon mfp in air (106 m) is comparable
to the domain; every receptor integrates most of the inventory. Groundshine is sharp
because deposition is LOCAL — the map shows a clean plume ribbon on the release axis
falling off 2-3 orders of magnitude either side. Groundshine exceeds cloudshine in
only 16% of cells (median ratio 0.063) but wins decisively where it does.

**Crossover.** Cloudshine is a steady rate; groundshine accumulates linearly with
duration (no decay/weathering here — fine against a 30 y half-life). So at
v_d = 1e-3 m/s:

    hot spot     groundshine overtakes cloudshine after ~19 min of release
    domain median                                      ~14 h

At v_d = 1e-2 (elemental iodine) both are 10x sooner — ~2 min and ~1.4 h. Both scale
linearly in v_d and duration, so rescaling needs no re-run.

### Near-surface sampling is the weak link

Only 3.46% of residence sits within 2 m of local grade. Measured:

    steps within 2 m of grade      191,667 (5.3% of all steps)
    10 m columns with any signal   554 / 1600 (35%)
    median steps in a live column  122  -> Poisson error ~9%

So where the plume touches down it is decently resolved, but it only touches down over
about a third of the domain, and the zero columns mix genuinely-clean ground with
under-sampling. This matters for the DEPOSITION map; it matters much less for the DOSE
map, which integrates over tens of metres of surrounding ground and self-smooths.
More DRW tries is the fix if the deposition footprint itself is ever the deliverable.

### Scope, stated not hidden

* GROUND deposition only — roof deposit is shielded from a ground receptor by the
  building under it; wall deposit is second order. Adding them needs a surface-element
  source rather than a per-column one.
* DRY only. Wet deposition (rain scavenging) is often dominant in a real assessment and
  is orders of magnitude faster. Not a CFD question.
* No decay, weathering or resuspension.
* Source grid is 2.5 m, deliberately below the 1.5 m receptor height — a coarser grid
  puts a receptor directly over a point source and produces a 1/r^2 spike.

---

## Deferred feature: CFD buffer domain

**Status: idea only, user to clarify scope.** Raised because urban-CFD guidance wants
≥5H upstream, ≥15H downstream and ≥5H lateral of the region of interest, and our domain
walls currently *are* the ROI walls. With H ≈ 50 m that is ~250 m / ~750 m.

Run 3 gives the first measurement of how much this actually costs: only **0.20%** of
residence time falls within 25 m of a lateral symmetry plane, and 1.17% within 50 m, with
the plume centre 5.3σ from the nearest wall. So the artefact is real but small *at this
wind direction and release point* — it will get worse for others.

Design considerations, in rough order of how much thought they need:

1. **Terrain in the buffer is nearly free, and should be included.** `build_domain_dtm`
   already crops the whole-island `data/dtm.tif`; extending to a bigger box is just a
   bigger crop (~0.2 s). `conforming_terrain` builds the whole surface as one CDT, so
   buffer and ROI terrain are continuous by construction — there is no seam to engineer.
2. **The real question is buildings, not terrain.** Three options:
   - none in the buffer — flow reaches the ROI without urban roughness, overestimating
     wind speed and underestimating turbulence at the leading buildings;
   - real buildings — correct, but a 400 m ROI becomes ~1.4 km and cell count explodes;
   - **no buildings + a wall roughness height on the buffer ground** (urban z₀ ≈ 0.5–1 m).
     Standard urban-CFD compromise, costs one boundary-condition field. Recommended.
3. **The cliff problem** (user's own observation: an arbitrary flat buffer turns a hill at
   the cutout edge into a wall). Fix: flatten the terrain to a constant elevation only in
   the *outer ring*, blending smoothly from the real DTM inward — so the inlet/outlet faces
   are clean vertical planes on flat ground. A uniform "5 m/s normal to boundary" inlet on
   a sloped face is its own artefact, so this is wanted regardless. **This is exactly the
   apron mechanism `terrain.py::flatten_pads` already implements for building pads**, applied
   at the domain edge instead — reuse it rather than writing a new blend.
4. **Sizing is wind-direction dependent** (15H downstream, 5H elsewhere) and a geometry tool
   does not know the wind direction — *unless* you make wind direction a build input, which
   is the next section. If you do, the buffer can be sized asymmetrically along-wind and
   gets much cheaper. See below; the two features should be built together.

Implementation sketch: `--buffer <m>` and `--buffer-flatten <m>` on `build_domain_stl`;
domain polygon = ROI polygon buffered outward; buildings clipped to the ROI only; terrain
built over the full buffered polygon. `_polygon_clip_solid` already handles arbitrary
polygons, so the clip needs no change.

---

## Deferred feature: wind direction as a build input

**Status: idea only, user to clarify scope.** Motivated by a real workflow limitation: the
domain box is axis-aligned in EPSG:3414, and Fluent's `Magnitude, Normal to Boundary` inlet
blows perpendicular to a face — so only the four grid-aligned directions are reachable
(xmin↔xmax, ymin↔ymax). A 45° wind currently means rotating the mesh by hand, which is
painful and requires re-meshing.

**The clean fix: rotate the world, not the wind.** Take the wind bearing as an app input and
emit geometry already rotated so the wind is always along +Y. Then the Fluent setup *never
changes* — inlet is always `tunnel-ymin`, outlet always `tunnel-ymax`, symmetry planes stay
clean and axis-aligned, `Normal to Boundary` keeps working, and 45° is exactly as easy as 0°.

Sketch, deliberately minimal:

1. App input: wind bearing θ (meteorological convention — the direction wind comes *from*).
2. Build the ROI as a **rotated rectangle** (rotated by θ about the ROI centre) in true
   SVY21 coordinates.
3. Run the existing pipeline unchanged. Terrain, extraction and the polygon clip all work in
   true coordinates and already handle arbitrary polygons, so **nothing upstream is touched**.
4. As the last step before STL export, apply one rigid rotation of −θ about the ROI centre.
   The slanted box becomes axis-aligned; wind is +Y.
5. Emit a sidecar `{"origin": [cx, cy], "rotation_deg": θ}` next to the STL so results map
   back to real coordinates.

Two reasons this is safe: it is a rotation **about Z**, so the vertical is untouched (terrain
slopes and building heights rotate correctly with the scene, and gravity — if ever enabled —
stays −Z). And the only new code is one transform plus a sidecar; the polygon construction
lives app-side.

The particle-track reader in this directory should grow the inverse transform so
`Particle X/Y Position` can be returned in true SVY21 — otherwise every downstream dose map
is in a rotated frame and silently mis-georeferenced.

### Why this makes the buffer feature cheaper

Knowing the wind direction means the buffer no longer has to be symmetric. Guidance is 5H
upstream / 15H downstream / 5H lateral; with H ≈ 50 m and a 400 m ROI:

| | box size | area | meshes needed |
|---|---|---|---|
| symmetric 15H all round (direction-agnostic) | 1900 × 1900 m | 3.61 km² | 1 |
| **wind-aligned, asymmetric** | **1400 × 900 m** | **1.26 km²** | 1 per direction |

~3× less mesh per run. And since a wind rose needs 8–16 directions anyway, one mesh per
direction is the workflow you want regardless — so the "one mesh serves all directions"
argument for a symmetric buffer mostly evaporates once direction is a build input. Build
these two together rather than separately.

---

## Mesh workstream closed (2026-08-27) — what it means for this pipeline

Recorded here because it decides what geometry the dose work is built on.

**The mesh is frozen at the current sealed + voxel-remesh output.** Full reasoning in
`sbg/onemap_native/research_2/PROBLEM_BRIEF.md` §14; the short version:

- The CFD path was never blocked. snappyHexMesh meshed the raw export (1,030,259 cells,
  no errors) and Ansys fault-tolerant meshing already uses it. The long "0 non-manifold,
  0 self-intersection" chase was for the Ansys *watertight* workflow — one option of
  three, not the one in use.
- The one load-bearing fix is `seal_piece`: OneMap meshes are un-welded double-sided
  soup, so no isosurface extractor can sign the SDF and buildings shatter. Sealing is
  why voxel remesh now returns volume within 0.4–1.1% of ground truth as a single body.
- **Nothing downstream of that changes a dose number**, for three measured reasons:
  deposition is near-ground (3.46% of plume residence sits within 2 m of grade, and the
  entire groundshine map comes from there); roof detail is shielded from every receptor
  by the building beneath it and is sub-grid at ~3 m cells; and the current DAGMC model
  (one fused solid + air) already validated against an analytic solution to **0.6%**
  with leakage **0.00000** inside the slab.

Per-building DAGMC volumes (a cell complex, which is what DAGMC actually wants) would
only buy per-building materials or per-building deposition — neither is in scope. The
machinery exists and is verified (287/287 buildings at 0/0/0/0 via fTetWild + pymeshfix)
but is deliberately not wired in.

**Practical note for anything that writes STL here:** at absolute SVY21 coordinates
(~29,000 m) float32 spacing is ~2 mm, so any STL we ship is silently 2 mm quantised.
Irrelevant at 3 m CFD cells and for photon transport, but do coincident-surface work in
a domain-local frame.

**Next up:** the two deferred features above — CFD buffer domain and wind direction as a
build input.
