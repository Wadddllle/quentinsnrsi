# Terrain in the tile-native pipeline: what it is, what it does, and what cannot be fixed

Status as of 2026-08-04. This exists because the terrain half of `sbg/onemap_native/` has absorbed a
lot of work whose *conclusions are mostly negative*, and those negatives keep getting re-derived.
Read this before attempting any terrain change.

---

## 1. The elevation data, and its hard limits

**Source:** `NationalMapLine.geojson` (634MB, SLA via data.gov.sg). Full streaming scan of all
16,889 features:

| layer | features | features with nonzero Z |
|---|---|---|
| `Layers/Contour_250K` | 7,723 | **0** |
| `Layers/Major_Road` | 7,310 | **0** |
| `Layers/Expressway_Sliproad` | 1,129 | **0** |
| `Layers/Expressway` | 726 | **0** |
| `Layers/International_bdy` | 1 | **0** |

- **`Contour_250K` is the only elevation in the file.** 1:250,000 scale, **20m contour interval**.
- Elevation is a **string in the `NAME` property** (`"20"`, `"40"`, ...), not in the geometry. Every
  coordinate's Z is 0 across the entire file.
- Roads/expressways carry only `NAME`/`SYMBOLID`/`SHAPE.LEN` — no elevation attribute of any kind.
  They cannot be used to augment elevation. (At most they could serve as a smoothness/max-grade prior
  inside an optimisation — not as data.)
- **There is no coastline layer.** `International_bdy` is a single maritime-boundary feature, so not
  even a free "z = 0 at the shore" constraint exists here.

**Derived cache:** `data/dtm.tif` — whole-island, 20m, EPSG:3414, built once by `scipy.griddata`
(linear + nearest-fill) over the contour points. `build_domain_dtm()` crops and bilinearly resamples
it per domain (~2.7s for a 460x460 grid). Fresh per-domain `griddata` is the fallback when the cache
is absent and agrees with the cache to within the source's own uncertainty.

### What this means in practice (measured, per domain)

| domain | DTM range | distribution |
|---|---|---|
| CBD 2km (28500..30500) | 0–40m | p25=1.5, p50=12.3, p75=20.0 |
| NUH 900m (~22400,30700) | 20–60m | **>50% of cells at exactly 20.00m** |
| Kent Ridge | 20–60m | effectively only 20 / 40 / 60 exist |

At Kent Ridge the DTM has **three usable levels across the whole area**. Interpolation between
contour lines produces large dead-flat plateaus with steep linear ramps between them. Absolute ground
is uncertain to roughly ±10m in flat regions.

**No better bare-earth source is available to this project.** Global DEMs (SRTM, Copernicus GLO-30)
are *surface* models — buildings and vegetation are baked in — so for urban Singapore they would
double-count the very geometry being placed. High-resolution national LiDAR appears to be restricted.
Treat the 20m contour DTM as fixed input, not as something to go and improve.

---

## 2. What the pipeline actually does

`place_on_terrain_conforming(pieces, dtm, domain_polygon, seal=True, skirt=True)`:

1. **`build_domain_dtm`** — crop the island cache to the domain. (~2.7s at CBD scale.)
2. **`_piece_polygons`** — each piece's footprint, as the union of its `_footprint_rings` clusters.
3. **`_connectivity_groups`** — group pieces that are physically joined in 3D (see §4).
4. **per-group pad level** — `min` over members' own levels; each member's own level is the
   25th percentile of the DTM under its footprint compound.
5. **`conforming_terrain`** — constrained Delaunay triangulation (`triangle`, `'pY'` flags) with the
   footprint rings inserted as PSLG constraints, so terrain and building-base vertices share exact
   positions. Pads are flat at the group level.
6. **`_skirt`** — a **closed** prism per footprint cluster, plunging `PLUNGE_M = 30m` below pad level.
   Only emitted when `voxel_size > 0` (see §5).
7. **`terrain_flat_base_solid`** — extrude the terrain surface down to a flat plane at `base_z`
   (dynamic: `min(0, lowest building point - 1m)`), producing a closed solid.

Verified properties (CBD 2km, 1,853 pieces):

- terrain surface tracks the raw DTM: **median deviation +0.00m, 96.1% within 1m**, worst cut −8.01m.
- pads are dead flat under every building: **terrain z-spread under a footprint = 0.000** (median,
  p90 AND max), 0/94 footprints varying >1m.
- `terrain_flat_base_solid` output is **closed and fully manifold** — 91,856 faces, every edge used
  exactly twice.

### Cost (CBD 2km, 1,853 pieces)

| step | before | after optimisation |
|---|---|---|
| `_connectivity_groups` | 21.4s | **7.5s** (4 threads + per-piece face-bbox precompute + vertex-KDTree accept) |
| `seal_piece` x1029 | 6.3s | 6.3s (does **not** parallelise — Python/GIL bound in trimesh) |
| `build_domain_dtm` | 2.7s | 2.7s |
| `conforming_terrain` (CDT) | 2.2s | 2.2s |
| **total terrain step** | **~42s** | **~28s** |

---

## 3. THE CORE UNSOLVABLE PROBLEM

OneMap flattens **every** building base to z = 0 (verified: `base_z == 0.000` across **1,061 pieces
in 5 unrelated areas** — NUS/NUH, CBD, Queenstown, Jurong, Bishan). The capture therefore carries
**zero information** about how buildings sit relative to each other. All relative elevation must come
from the DTM.

Two real cases, both at Kent Ridge, both in one connectivity group each:

| group | what it is | reality | correct handling |
|---|---|---|---|
| 7 (7 members) | NUH blocks joined by a visible bridge | genuinely **coplanar** | one shared level |
| 2 (10 members) | Prince George's Park Residences, a linked string running up a hill | genuinely at **different levels** | each follows its own ground |

Measured ground spread for both:

```
group 2 (PGP hostels):  DTM 20.0..40.0m   spread 20.0m
group 7 (NUH blocks):   DTM 20.0..40.0m   spread 20.0m
```

**Identical spread. Opposite correct answers.**

Both straddle the same 20m contour band boundary, for the same reason, and the DTM has no finer
information to offer. **No rule derived from this DTM can distinguish them.** A spread threshold was
the obvious candidate and it is dead on arrival — measured, not assumed.

### So the choice is which error to accept

| policy | NUH | PGP-style hillside groups |
|---|---|---|
| group + one level (**current**) | correct | carves a flat terrace into the hillside |
| no grouping | ~20m shear through the bridge | correct |
| soft coupling (λ-weighted) | few-metre shear | few-metre steps |

On the Kent Ridge domain, **3 of 9 multi-member groups span >10m**, so grouping and not-grouping
damage a similar number of places — they just fail in different, differently-visible ways.

**Soft coupling** = minimise `Σ wᵢ(dzᵢ − groundᵢ)² + λ Σ_(i,j)∈E (dzᵢ − dzⱼ)²` over the connectivity
graph. Sparse Laplacian solve, milliseconds, one knob. λ=∞ is today's behaviour; λ=0 is
pre-NUH-fix behaviour. A finite λ bounds the error everywhere instead of letting it reach 20m in one
place. **Not implemented.** It is damage control, not a fix — it cannot recover information the DTM
does not contain.

### 3b. All three policies are now selectable (2026-08-04) — `--placement`

`place_on_terrain_conforming(mode=..., coupling_lambda=...)`, exposed as
`build_domain_stl(placement=, coupling_lambda=)`, CLI `--placement {group,drape,laplacian}` +
`--coupling-lambda`, and a dropdown (+ λ field, shown only for laplacian) in the web UI's Advanced
settings. Server-side whitelisted in `_BUILD_OPTS`, with `placement` validated against the allowed
set since it is the one free-form string a client can send.

| mode | what it does | who it's right for |
|---|---|---|
| `group` (**default**) | 3D-connected pieces share one flat level (`min` over the group) | bridge/podium-linked structures stay coplanar; hillside strings get carved in |
| `drape` | no 3D grouping at all — every piece on its own footprint's ground | hillside strings terrace correctly; bridged pairs shear (~20m measured) |
| `laplacian` | soft coupling, `(I + λL)z = g` over the connectivity graph | both resolve without classification; λ=∞ ≡ group, λ=0 ≡ drape |

**The solve is over COMPOUNDS, not pieces** — a compound is one triangulation constraint ring with one
pad z, so pieces whose footprints touch in plan physically cannot take different levels. Contracting
them (union-find, including compounds a single multi-ring piece straddles) is a hard constraint, not
an approximation. `_connectivity_edges` was split out of `_connectivity_groups` for this: the
component *labels* are not enough, since a chain and a compact blob share labels but behave
completely differently under Laplacian smoothing — which is the entire point of the mode.

Verified end-to-end, all strictly watertight (meshlib `holes=0` **and** a strict trimesh re-weld),
1 body, exact bounds:

| domain | mode | faces | volume m³ | edge histogram |
|---|---|---|---|---|
| Kent Ridge | group | 56,702 | 37,046,199 | `{2: 85053}` |
| Kent Ridge | drape | 56,136 | 37,628,380 | `{2: 84204}` |
| Kent Ridge | laplacian λ=10 | 59,400 | 37,664,924 | `{2: 89100}` |
| CBD 2km (1,853 pieces) | group | 336,668 | 208,292,597 | `{2: 505002}` |
| CBD 2km | laplacian λ=10 | 337,184 | 208,892,713 | `{2: 505776}` |

`group` is **bit-identical to the pre-change baseline** on both domains — adding the modes changed
nothing on the default path. λ moves geometry monotonically toward drape (max Δz vs group: 13.2m at
λ=30, 14.8m at λ=10, 17.5m at λ=3, 21.4m at full drape).

Two real things fixed while wiring this up:
- **`own_pad` semantics.** The first version took `min` over every compound a piece touches instead of
  the compound holding its representative point. Defensible in isolation, but it silently moved real
  geometry on the *default* path and re-exposed a latent clip-wall artifact (3 non-manifold edges, all
  exactly on `x=22850`, `z=1.0`, 16–25.6m long — the same coincident-surface signature §6 records for
  the CBD domain). Reverted to the original semantics; the default is untouched.
- **The zero-area-face drop now iterates.** `mr.fillHole` closes the openings the drop creates, but the
  patch it emits along a collinear clip-wall boundary can itself be zero-area, so one pass left some
  behind. Capped at 4 passes and stops on no progress. Note this alone did **not** rescue the case
  above — dropping those faces without refilling opens 8 boundary edges, i.e. they are load-bearing.
  The `own_pad` revert is what actually fixed it.

Incidental: the per-compound level loop was O(compounds × pieces) with a shapely `intersects` per cell;
it now reuses the membership map built once. **CBD terrain step 45.5s → 15.8s.**

---

### 3a. Drape re-enabled as a group-level knob, and what it measured (2026-08-04)

`DRAPE_SPREAD_M` used to be a *compound*-level test and was disabled (`inf`) because draping dropped
the ground out from under bottomless meshes and the voxel remesh ate them. **That blocker is gone** —
`seal_piece` makes every piece a closed solid, so nothing depends on the slab for backing any more.
It is now re-enabled at **connectivity-group** level, which is where the flattening actually happens
(a group can span several compounds, so a compound-level spread test could not see the real range).
A group over threshold gets `group_pad = None`: every member sits at its own local ground and its
compounds' terrain follows the DTM instead of being flattened.

**Default is still `inf`** (behaviour unchanged, verified end-to-end: strictly watertight, 1 body,
exact bounds, volume within 0.004% of the recorded baseline). The knob exists; the threshold does not
have a defensible value. Sweep on the Kent Ridge domain (94 pieces, 53 groups) — note NUH's shear is
measured across the whole group, the PGP string is group 1 (10 members):

| `DRAPE_SPREAD_M` | groups draped | pieces | NUH shear | max sink | pieces sunk >2m |
|---|---|---|---|---|---|
| `inf` (current) | 0 | 0 | **0.00** | **−20.00** | 15 |
| 25 | 0 | 0 | 0.00 | −20.00 | 15 |
| 15 | 6 | 46 | **19.82** | −8.43 | 2 |
| 12 | 9 | 51 | **19.82** | −0.18 | 0 |
| 5 | 12 | 54 | **19.82** | −0.18 | 0 |

The sinking really does go to zero — and NUH breaks the moment any threshold is low enough to catch
PGP, exactly as §3 predicts. **There is no row where both are acceptable.** This is the tradeoff
measured, not argued.

**Does graph topology separate them where elevation can't?** Weakly, and not defensibly. NUH is a
compact complex, PGP a long chain:

| group | n | edges | diameter | density | extent | spread |
|---|---|---|---|---|---|---|
| 6 (NUH) | 7 | 8 | 3 | 0.38 | 315m | 20.00m |
| 1 (PGP) | 10 | 9 | 6 | 0.20 | 359m | 20.00m |
| 26 | 25 | 26 | 10 | 0.09 | 312m | 16.60m |

A density threshold at ~0.3 separates the two labelled cases, but that is fitting a rule to n=2, and
group 26 (25 members, even chainier) has no ground truth either way. Not adopted.

**Soft coupling measured on the same domain** (dense solve, 94 pieces, `(I + λL)z = g`). Adjacent-pair
shear is the metric that matters for a bridge — not group span:

| λ | NUH adj. shear | NUH span | PGP span | max sink | max float |
|---|---|---|---|---|---|
| ∞ (≡ today) | 0.00 | 0.00 | 0.00 | −13.21 | +7.21 |
| 30 | 0.34 | 0.65 | 3.89 | −12.90 | +6.59 |
| **10** | **0.96** | **1.81** | **8.69** | −12.32 | +6.54 |
| 3 | 2.77 | 4.70 | 15.11 | −10.75 | +6.31 |
| 0 (full drape) | 19.82 | 19.82 | 20.00 | 0.00 | 0.00 |

**λ≈10 does what neither binary policy can**: NUH's bridge holds to sub-metre steps (0.96m adjacent,
1.81m across the whole complex) while PGP terraces over 8.7m — the two cases resolve differently
*without being classified*, purely because a dense graph pulls itself flat and a long chain
accumulates ground error along its length.

Two real caveats, both measured:
- **Least-squares gives the group MEAN, so buildings float** (11 pieces >2m at λ=10). Production uses
  `min()` deliberately because floating is the worse CFD artifact. Anchoring each group down so
  nothing floats works and preserves the ramp shape exactly, but rigidly shifts the whole group, so
  max sink goes back to −18.86m at λ=10. Anchoring is not free.
- **Terrain-side feasibility**: a 2D compound is ONE constraint ring with ONE pad z, so pieces whose
  footprints touch in plan cannot take different levels. Checked: NUH's 7 members span 7 compounds,
  PGP's 10 span 8 (largest 3 members) — so both are implementable, with same-compound members
  hard-constrained equal. This is not a blocker but it is real extra structure to build.

---

## 4. Connectivity grouping (`_connectivity_groups`)

Exists because per-piece placement shears physically-joined buildings apart. A user-reported case:
NUH's health-system block and Kent Ridge Wing 2 are visibly bridge-linked but were placed **20m
apart**.

- **Edge test is SURFACE distance, not vertex distance.** These meshes average **7–12m per triangle
  edge (max 98m)**, so two surfaces flush against each other can have their nearest *vertices* metres
  apart. The real NUH pair: **vertex gap 5.735m, true surface gap 0.066m — an 87× overestimate.**
- `_surface_gap` does exact point-to-triangle over only the **contact region** (faces in the overlap
  of the two eps-expanded bboxes). Sampling the contact patch instead was tried and measured at
  **134.6s** for one domain (160× the whole placement step) — do not reintroduce it.
- `CONNECT_EPS_M = 0.5`. With an honest metric this is *tighter* than the 2.0 that a broken metric
  seemed to require. Loose eps chains unrelated buildings (eps=1.0 → largest group 29 vs 24 at 0.25).
- The 2D footprint union (`unary_union`) **cannot** replace this: a bridge spans a gap, so the two
  footprints never touch, and the bridge's own ground contact is below `_MIN_RING_AREA = 8 m²`. The
  connection is real but invisible in plan view.

---

## 5. Skirts

A closed prism per footprint cluster, `PLUNGE_M = 30m` below pad level.

- **Purpose:** guarantee *volume overlap* into the terrain slab so the voxel remesh fuses building and
  terrain, rather than relying on coincident-surface contact. It is **not** an alternative to the
  conforming triangulation — the CDT closes the horizontal seam (shared exact vertices), the skirt
  guarantees vertical overlap. Both have always coexisted.
- **Only emitted when `voxel_size > 0`.** In the raw path it actively *hurts*: the skirt is built from
  the same footprint ring as the building it wraps, so they share vertices and weld into each other,
  leaving both open. Measured (component closure, no cross-object weld): with skirts **2,921/2,973
  closed**; without, **1,301/1,303 closed**.
- The old rationale "fixes podium-towers whose mesh starts ~19m up (CityLights)" is **stale** —
  `base_z` is 0.000 everywhere measured. Either those pieces include their podium, or the note
  predates the transform fix.

---

## 6. Things tried that DO NOT WORK (do not re-attempt without new information)

| attempt | outcome |
|---|---|
| `max_gap` NaN-filtering of the DTM | 90% of the whole-island grid NaN'd — a single distance threshold cannot separate "off-island" from Singapore's genuinely sparse flat-land contour crossings |
| raster **apron** grading pad→terrain | excavates a moat around each building on slopes; turned off |
| **drape** (`DRAPE_SPREAD_M`) | **re-enabled at group level and swept (§3a)**. Mechanically works — sinking goes −20.00m → −0.18m — but NUH shears 19.82m at every threshold low enough to catch PGP. No usable value; default stays `inf`. Its original blocker (eroding bottomless shells) is genuinely obsolete now that `seal_piece` closes every piece. |
| **spread threshold** to separate NUH from PGP | dead: both measure exactly 20.0m spread with opposite correct answers — confirmed by the §3a sweep |
| **graph topology** to separate NUH from PGP | weak signal only (density 0.38 vs 0.20, diameter 3 vs 6). A threshold fits n=2 labelled cases and a 25-member group sits on the wrong side of it with no ground truth. Not adopted (§3a) |
| **3D bbox reject** in connectivity | kills 0.2% of candidate pairs — buildings all overlap in z |
| `mr.fillHole` on `terrain_flat_base_solid` | **REGRESSION**: 91,856 faces / watertight → 95,050 faces / **2,292 non-manifold edges**. The "4 holes" meshlib reports are an artifact of vertex-splitting at winding seams; a strict trimesh weld shows the solid was already perfect. Reverted, warning comment left in the function. |
| global DEMs (SRTM / Copernicus GLO-30) | *surface* models with buildings and trees baked in — would double-count the geometry being placed |
| threading `seal_piece` | 0.9–1.1× — Python/GIL bound, no benefit |

---

## 7. Metric traps that produced false conclusions

Three separate times a measurement error produced a "defect" that did not exist. Check the metric
before trusting a terrain conclusion.

1. **Whole-mesh top-down silhouette, raw vs sealed.** Reported the base cap as inflating plan area
   +17%. Wrong: the raw mesh *has no bottom faces*, so any cap increases its silhouette. Measured
   correctly (cap area vs the real solid cross-section at base+0.5m): **median 1.10×, 9/10 pieces
   within ~15%**, only one genuine over-fill (1.39×, a building with 4 nested courtyard loops).
2. **Welding terrain and buildings together before counting components.** Reported 868 open
   components in the raw export. The conforming triangulation *deliberately* makes them share
   vertices, so welding fused them and flagged the junction as broken. Unwelded: **2 open, not 868**.
3. **Loading an STL with `process=False` and no `merge_vertices`.** Every triangle becomes its own
   component (475,834 of them). STL is triangle soup — it has no object boundaries, so per-component
   closure **cannot** be verified in an exported STL at all. Verify in memory, before export.

---

## 8. Open / not done

- ~~Soft coupling~~ — **implemented and shipped (§3b)** as `--placement laplacian` / the UI dropdown.
  Left **unanchored** deliberately (least squares settles a group near its mean, so some members float
  up to ~6.5m at λ=10 where `group`'s `min()` never floats) — anchoring was measured and is not free,
  it rigidly shifts the group and puts max sink back at −18.9m. Revisit only if the eye test says
  floating reads worse than sinking.
- ~~Re-enable drape~~ — **done (§3a, §3b)**, now a first-class `--placement drape` rather than a
  threshold. `DRAPE_SPREAD_M` survives inside `group` mode, still defaulting to `inf`: the sweep found
  no usable value, so prefer `laplacian` over tuning it.
- **Which mode should be the default** — `group` is, on inertia rather than evidence. Unresolved until
  the STLs above get a real eye test.
- **DTM smoothing** — the *creases* at contour lines are a `griddata` linear-interpolation artifact,
  not topography. Smoothing would remove definitely-fake faceting without claiming accuracy the data
  lacks. Not implemented.
- **Skip grouping + pad flattening on flat domains** — where the DTM range under a domain is small,
  every pad lands at the same level and grouping changes nothing. Most of Singapore is flat. Pure
  speed win. Not implemented.
- **`seal_piece` parallelism** — 6.3s and GIL-bound; would need a process pool or a C-side rewrite.
- **Beyond 2km / 1,853 pieces is untested.**
