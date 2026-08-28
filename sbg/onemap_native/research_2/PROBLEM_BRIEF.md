# Watertight + high-fidelity: where we actually are, and what to go research

Written 2026-08-19. All numbers below were **re-measured today**, not recalled — several
correct earlier notes in the plan doc. Companion to `final_report.md` (the CGAL/GSoC
manifold-dual-contouring report), whose relevance is reassessed in §5.

---

## 0. TL;DR — read this first (2026-08-20)

**Approach that works:** group buildings by proximity → one voxel level set per GROUP → slice
open-bottomed at pad+1m → the slice rim IS the CDT constraint → conforming terrain. No shrink,
no boolean union, terrain never remeshed.

### Final state, three domains (group remesh, decimation 0.25, `add_v` 1mm)

| domain | pieces | groups | faces | closed | NM | bowtie | wind | selfX | RSS |
|---|---|---|---|---|---|---|---|---|---|
| **Duxton 400m** | 287 | 35 | 250,616 | ✅ **YES** | **0** | **0** | **0** | **38** | 0.9 GB |
| **Kent Ridge 900m** | 118 | 78 | 906,503 | ❌ NO (1,849) | 5 | 0 | 8 | 543 | 2.3 GB |
| **Queenstown 2.4km** | 830 | 684 | — | *stopped, OOM* | — | — | — | (shells 2,374) | 7.6 GB |

Kent Ridge variants (both zero-decimation, 9.17M faces):

| config | closed | NM | wind | selfX |
|---|---|---|---|---|
| `DEC_ERR=0`, `add_v` 1mm | NO (2) | 116 | 228 | 800 |
| `DEC_ERR=0`, `add_v` 1e-6 | ✅ **YES** | 112 | 224 | (skipped) |

### The one number that explains most of it

| domain | pieces per group | between-group selfX | per piece |
|---|---|---|---|
| Duxton | **8.2** | **3** | 0.010 |
| Kent Ridge | 1.5 | 308 | 2.6 |
| Queenstown | 1.2 | 2,374 | 2.9 |

Duxton's dense shophouse terraces genuinely touch, so proximity grouping merges them into big
groups and the level set fuses the party walls. Kent Ridge and Queenstown are mostly detached
buildings that overlap via podiums/bridges where the *surfaces* touch but the *vertices* are
metres apart — the vertex-distance grouping test misses them, they stay separate solids, and
every such pair becomes between-group self-intersection. **Duxton is ~260x cleaner per building
purely because grouping worked there.**

### Confirmed causes

| defect | cause | evidence |
|---|---|---|
| self-X *within* a group | `decimateMesh` | 0 on every raw level set; 53 appear after decimation at maxError 0.25; 0 again at 0.10 |
| dropped rings → not closed | `decimateMesh` moving ring vertices | 2 dropped at 0.25, **0** at DEC_ERR=0 |
| seam mismatch → not closed | `conf_holed`'s `add_v` 1mm dedup | seam exact 113,224/113,224 at 0.000m with 1e-6; closed=YES |
| self-X *between* groups | vertex-distance grouping misses deep overlaps | scales with pieces-per-group across 3 domains |
| self-X vs terrain | pad = min DTM over group, on relief | Duxton 35 of 38; deep (median 5.5m) |
| **112 NM / 224 wind** | **unresolved** | see §4h |

## 0b. v2 — decouple grouping / fusion / slicing (2026-08-20). Duxton: selfX 38 -> 3

Two independent changes (`scripts/group_v2.py`, both env-toggled):

- **`METRIC=surface`** — grouping uses `terrain.py::_surface_gap` (exact point-to-triangle)
  instead of the KD-tree vertex query.
- **`SPLIT_COMP=1`** — a group is now a **voxel BATCH, not a solid**. After remeshing, the
  fused result is split into connected components and each component gets its **own cut plane
  and its own pad_z**, instead of one plane + one `min DTM` for the whole batch.

### Duxton result

| | v1 group remesh | **v2** |
|---|---|---|
| closed | ✅ YES | ✅ **YES** |
| NM / bowtie / winding | 0 / 0 / 0 | **0 / 0 / 0** |
| **selfX** | **38** | **3** |
| faces | 250,616 | 249,614 |
| runtime | 18 s | **386 s** |

30 batches -> **43 solids** -> 37 placed, so 13 solids had been sharing a plane. `capped 0
loops` — all 88 boundary loops became legitimate rings, no degenerate fan caps needed.

**The decoupling removed the entire building-vs-terrain self-X class.** Shells-only self-X was
3 before and is 3 after, so the ~35 that disappeared were all terrain penetration caused by a
shared `pad_z = min DTM over batch` sinking members that sit on higher ground.

### Which change did the work

**The split, not the metric.** The surface metric found real extra merges (Duxton 35 -> 30
groups, Kent Ridge 78 -> 65, i.e. 40 -> 53 merges) but shells-only selfX did not move (3 -> 3).
The metric is a genuine but minor improvement; it does not transform Kent Ridge, whose
buildings really are mostly separate solids rather than a mass of missed contacts.

### Cost — the split as written is not viable at scale

`trimesh.split()` on ~1M-face fused meshes dominates: group-remesh 372.9 s of a 386 s run
(decimate is only 6.1 s), a **21x slowdown**. On Kent Ridge it was worse — **8.4 GB RSS, swap
engaged, still inside the first 25 of 65 groups at 293 s** — and was stopped to protect the
machine. Kent Ridge v2 therefore has **no result**.

**Fix:** use meshlib `MeshComponents.getAllComponents` on the mesh while it is still in meshlib
form, instead of round-tripping to trimesh. Same decoupling, without the cost.

### Also checked

`trimesh.collision` imports but `fcl` is not installed, so `CollisionManager` cannot run and
would be a new binary dependency. `_surface_gap` is already exact, already vectorised over the
contact region, and already in the codebase — no reason to add fcl.

**Artifact:** `data/wt_raw_test/duxton_400m_groupremesh_v2.stl`

## 0c. v3 — sparse-graph component split. Same result, 2.9x faster, 0.73 GB.

v2's decoupling was right but its implementation was not: `trimesh.split()` (with
`Trimesh(process=True)`) on the multi-million-face fused mesh was 372.9 s of a 386 s Duxton
run, and 8.4 GB / swap-thrash on Kent Ridge, which had to be killed.

**`scripts/group_v3.py::_split_fast`** labels connected components from the VERTEX graph —
one `coo_matrix` + `connected_components`, no Trimesh object, no cached adjacency:

| | v2 (trimesh split) | **v3 (sparse graph)** |
|---|---|---|
| runtime | 386 s | **132 s** |
| group-remesh stage | 372.9 s | **120.5 s** |
| peak RSS | multi-GB | **0.73 GB** |
| closed / NM / bowtie / wind | YES / 0 / 0 / 0 | **YES / 0 / 0 / 0** |
| selfX | 3 | **3** |
| faces | 249,614 | 249,614 |

Byte-identical output, ~3x faster, memory bounded. It finds **64 solids** from 30 batches
(v2's trimesh split found 43); 37 are placed either way, so the extra 21 are sub-4-face specks
correctly filtered.

`VS` is now an env var too, so a coarser voxel (`VS=1.0`, ~4x fewer raw faces) can be tested
against the same harness.

### Standing state, Duxton: **0 / 0 / 0 / 0 / 3**

closed ✅ · non-manifold ✅ 0 · bowtie ✅ 0 · winding ✅ 0 · **self-intersections 3**

Three triangle pairs out of 249,614 faces, and they are NOT inside any single solid (sampled
groups each measure selfX=0). Two candidate causes, cheaply separable:
1. **decimation** — this run had `DEC_ERR=0.25`, and decimation is already proven to
   manufacture self-intersections (0 -> 53 on Kent Ridge groups). Rerun with `DEC_ERR=0`.
2. a genuinely missed merge between two overlapping solids.

**Artifact:** `data/wt_raw_test/duxton_400m_v3.stl`

## 1. The goal, stated precisely

One surface mesh that is simultaneously:

- **(A) intersection-free and manifold** — so a surface-*remeshing* consumer (Ansys watertight
  workflow, DAGMC for OpenMC) will accept it, and
- **(B) exact captured geometry** — not resampled onto a voxel grid.

Every artefact we have satisfies exactly one of these. That's the whole problem.

Added constraint from this week: Ansys won't export a surface mesh without first building the
volume mesh, so "let Ansys wrap it and re-export a clean shell" is not an escape hatch. The
mesh has to be right *before* it reaches Ansys.

---

## 2. Current state — measured today

`data/wt_raw_test/*.stl`, defects via meshlib + a strict 0.1 mm re-weld:

| artefact | faces | holes | comps | **open edges** | **non-manifold** | **self-X tris** |
|---|---|---|---|---|---|---|
| kentridge_900m_voxel_baseline | 62,764 | 0 | 1 | 0 | 0 | **0** |
| kentridge_900m_watertight_raw | 94,166 | 4 | 127 | 0 | 1 | **21,799** |
| duxton_400m_watertight_raw | 44,232 | 4 | 345 | 0 | 4 | **11,334** |
| duxton_400m_slice_cdt | 368,192 | 48 | 38 | 958 | 3 | **2,148** |

Read this as: the voxel baseline is topologically perfect and geometrically compromised.
Raw is geometrically perfect and topologically unusable *for remeshers* (though fine for
snappyHexMesh, which ray-casts and never retriangulates). Slice+CDT sits between and is the
only one trending toward both.

**slice+CDT reproduces exactly** — re-ran `scratchpad/slice_cdt2.py` today, 38 s:

```
[union]  24 touching groups merged -> 255 solids
[rings]  299 constraints, 0 overlapping rings dropped (255 buildings kept)
[verify] faces=368,469 components=32 openEdges=869 nonManifoldEdges=0
[verify] selfIntersectingTris=1966
```

(958/2,148 in the table vs 869/1,966 here is just the STL round-trip; the script's in-memory
numbers are the real ones.)

---

## 2b. "Watertight" vs "manifold" vs "intersection-free" — three different things

These are independent properties and I had been conflating them. Full audit
(`scripts/topo_audit.py`, strict 0.1 mm re-weld — mandatory, since STL is triangle soup):

| property | test | why it matters |
|---|---|---|
| **closed / watertight** | every edge has exactly 2 faces | defines an inside; needed to say "this is a solid" |
| **edge-manifold** | no edge has ≥3 faces | a remesher/ray-tracer can't decide which surface to follow |
| **vertex-manifold** | faces around each vertex form ONE fan (no bowtie) | same, at a point |
| **consistently oriented** | no duplicate half-edge in the same direction | normals must agree, or inside/outside flips |
| **intersection-free** | no two triangles pass through each other | independent of ALL the above — a mesh can be perfectly closed AND manifold AND still self-intersect |

Measured, 2026-08-19:

| mesh | closed | edge-manif. | vertex-manif. | oriented | intersection-free |
|---|---|---|---|---|---|
| kentridge voxel baseline | ✅ 0 | ✅ 0 | ✅ 0 | ✅ 0 | ✅ **0** |
| duxton **raw** | ✅ 0 | ❌ 4 | ❌ 2 | ❌ 328 | ❌ **9,725** |
| duxton **slice+CDT (ringfix)** | ❌ 489 | ❌ 3 | ✅ 0 | ❌ 326 | ❌ **945** |
| `cfd_domain_8135dd35.stl` | ❌ 7,165 | ❌ 237,240 | ❌ 8 | ❌ 490,575 | ❌ **62,735** |

Note the raw mesh is **closed but not intersection-free** — that combination is exactly why
snappyHexMesh accepts it (ray-casting only needs closure) and Ansys' watertight workflow
rejects it (remeshing needs intersection-freeness). Closure was never the problem.

### Self-intersection magnitude, not just count

Counting pairs hides that they are not all the same *kind* of defect:

| mesh | pairs | % of faces | median penetration | <5 cm | >50 cm |
|---|---|---|---|---|---|
| duxton **raw** | 9,725 | 19.7 % | **10.99 m** | 0 % | **100 %** |
| duxton **slice+CDT (ringfix)** | 945 | **0.27 %** | **0.009 m** | **74 %** | 10 % |

This is the most important number on this page. slice+CDT didn't just cut the *count* 10×, it
changed the *character* of the defect: raw has deep structural interpenetration (median 11 m —
buildings genuinely inside each other and inside the terrain); slice+CDT has hairline grazing
contact (median **9 mm**, three orders of magnitude smaller), with a residual tail of 91 pairs
(10 %) over 50 cm that are real building-vs-building overlap. Two different problems needing
two different fixes: the 74 % shallow group is a snap/tolerance question, the 10 % deep tail is
a genuine geometry (union) question.

### About `cfd_domain_8135dd35.stl` specifically

**Do not feed this one to DAGMC.** 598,482 faces → only 436,842 unique triangles: **161,622
triangles appear exactly twice**. That is un-deduplicated *double-sided* OneMap soup — this
file predates `seal_piece`. Its 237,240 "non-manifold edges" and 490,575 winding conflicts are
mostly that duplication. Regenerating the same domain with the current pipeline gives sealed
geometry and a far better starting point.

## 3. What happened to per-building dual contouring + Delaunay-with-terrain

It works, it's the best thing we have, and it was left **two bugs short of done**. Recap of
the mechanism, because it's the thing worth defending:

1. Per building: `seal_piece` → meshlib level set (`meshToLevelSet`) → **Dual MC**
   (`gridToMesh`, iso 0, adaptivity 0) at 0.5 m → decimate (maxError 0.25).
2. **Slice** the remeshed solid at pad+1 m with `cap=False` → an *open-bottomed shell*.
3. The ring is the shell's **own boundary loop** — so the CDT constraint literally *is* the
   mesh cut boundary, rather than a separately-derived footprint that has to be reconciled.
4. Terrain = constrained Delaunay with those rings as constraints **and hole seeds**, so
   terrain is deleted under each building and exactly two faces meet at every seam edge.

**The seam works perfectly.** Measured: terrain-hole vertex → nearest shell vertex, *exact
match 29,441/29,441, max distance 0.000 m*. That is the part everyone assumes will be hard,
and it is solved — by construction, not by tolerance.

Progression across the fixes, for context on what each was worth:

| | dropped rings | open edges | self-X | components |
|---|---|---|---|---|
| initial | 81 | 7,903 | 29,014 | 100 |
| + courtyard nesting | 28 | 2,908 | 7,520 | 47 |
| + **boolean union of touching buildings** | 0 | 1,097 | 2,251 | 40 |
| + 0.05 m² ring threshold | 1 | 963 | 2,148 | 33 |
| + 3D-overlap grouping | 0 | **869** | **1,966** | 32 |

The boolean-union row is the notable one: `mr.boolean(..., Union)` **succeeds here**, after
failing in every earlier session. It failed before because the inputs self-intersected; the
per-building Dual-MC remesh outputs self-X = 0, which is exactly the precondition exact CSG
needs. So per-building remeshing didn't just bound memory — it unlocked exact CSG.

**Why it stalled:** the session ran out before fixing the two residual defects in §4, and the
"restage the artifact" step never completed, so the staged STL was stale until today.

---

## 4. The actual bug — ONE root cause, now found and half-fixed

**Correction to the first draft of this brief:** I described P1 (open edges) and P2
(self-intersections) as two independent problems. They are substantially **the same bug**,
and it is a small one. Found 2026-08-19 by instrumenting rather than reasoning.

### The root cause

`slice_cdt2.py::shell_rings()` has **three silent reject paths**, and the caller keeps the
shell regardless of whether its ring survived:

```python
if len(pts) < 4: continue                      # 5 loops rejected
if abs(median(z) - zc) > 0.25: continue        # off-plane, correct
q = Polygon(pts[:,:2])
if q.is_valid and q.area > 0.05: out.append(q) # 8 invalid + 34 tiny rejected
```

When a ring is rejected, the building's shell still goes into the assembly, but its ring never
becomes a CDT constraint. So terrain is never opened underneath it. Two consequences, which is
why this read as two bugs:

1. The shell's bottom boundary loop has no terrain to attach to → **open edges**.
2. Terrain sheet passes straight through the building's walls → **self-intersections**.

### Measured on Duxton (400 m, 255 solids)

| reject reason | count | areas |
|---|---|---|
| `not q.is_valid` | **8** | **338.1, 241.2, 218.9 m²**, then 5 × 0.0 |
| `q.area <= 0.05` | 34 | max 0.041 m² |
| `len(pts) < 4` | 5 | degenerate |

Those three big invalid ones are **real whole buildings**. Cross-checked against the open-edge
graph independently: the 3 largest open loops have hull areas 339.5 / 245.5 / 219.5 m² — the
same three. And every one of the 42 open loops is **perfectly planar (dz = 0.00)**, i.e. they
all sit at a cut plane. (This kills the first draft's claim of "holes in the building surface
up to 5.7 m from any ring" — there are none.)

**Why `is_valid` fails:** the slice outline self-touches in XY — a pinched / figure-8 profile
at the cut plane, which is ordinary for a real building. `shapely.Polygon` calls that invalid.

### The fix, tested

`buffer(0)` repairs the self-touch (it's what produced the area figures above), then take each
resulting part:

```python
if not q.is_valid:
    q = q.buffer(0)
for part in (q.geoms if q.geom_type == "MultiPolygon" else [q]):
    if part.geom_type == "Polygon" and part.is_valid and part.area > 0.05:
        out.append(Polygon(part.exterior))
```

Six lines. Result (`scripts/slice_cdt3_ringfix.py`, same 36 s runtime):

| | before | after | |
|---|---|---|---|
| ring constraints | 299 | **302** | the 3 real buildings recovered |
| **open edges** | 869 | **494** | −43 % |
| **self-intersections** | 1,966 | **945** | −52 % |
| components | 32 | 30 | |
| non-manifold edges | 0 | 0 | unchanged |

Both halved from one cause — confirming they were one bug.

### What is actually left

**Open edges (494).** Re-diagnosed after the fix: **43 loops, all flat (dz = 0.00), none larger
than 1 m², 40 of 43 under 0.05 m².** Total open area across the whole 400 × 400 m domain is
under ~2 m². **No real building is open any more** — these are the deliberate `area > 0.05` and
`len < 4` rejects.

*Remaining work:* have `shell_rings` return the rejected tiny loops as "cap me" instead of
dropping them, and close each with a planar triangulation at its (constant) z, **appending
faces only**. The planarity makes this the easy case. The one hard constraint still stands:
**no meshlib round-trip** — float32 at ~29,000 m coordinates quantises to ~1 mm and destroys
the exact seam (measured: exact matches 29,441 → 8,092, open edges → 59,836).

**Self-intersections (945).** This is the genuinely open one. Pre-fix breakdown was 73 % both
faces in the pad band, 26 % building-vs-building, 1 % overhang; the pad-band share is what the
ring fix mostly ate. What remains is expected to be real **adjacent buildings interpenetrating
above the cut plane** — the 5 cm XY shrink separates them at the base so ring-based grouping
misses them, but they still share walls higher up. Not re-characterised after the fix; that is
the next measurement to take, not a guess to act on.

Known dead ends here: raising the shrink is U-shaped (0.05 optimal; 0.00 → 20,453; 0.25/0.50
worse), and 3D-overlap grouping via `mr.boolean(..., Intersection)` found only **1** extra
group, likely because exact CSG can't see the razor-thin lens left by a 5 cm shrink.

### P3 — the accuracy bug that exists in production *today*

**What it is:** `extract.py::_footprint_rings` builds each building's footprint as a **convex
hull of its near-base points only** (`z <= base_z + 2.0`). That footprint is what the terrain
gets graded flat to, and what the plunge skirt is built from. But LiDAR capture near the ground
is sparse and occluded, so the hull covers a *subset* of the points, not the building's real
cross-section.

**Measured on Duxton, 170 buildings** (`scripts/footprint_bug.py`), production hull vs the true
cross-section 1 m above the base:

| | median | total |
|---|---|---|
| hull area (what production uses) | **38.1 m²** | 9,144 m² |
| true section area | **147.3 m²** | 38,115 m² |

- ratio true/hull: p10 **1.89**, median **3.92**, mean 6.57, p90 **14.11**, max 55.7
- **95 % (162/170) of buildings have a footprint that is too small.** Zero are too big.
- **30,116 m² of real building overhangs its own graded pad** — 79 % of total building area.
- Only 1,146 m² of pad sticks out past the building, i.e. the hull is essentially a *subset*
  of the true footprint, not displaced from it.

**Correction:** an earlier note in the plan doc said these hulls are "frequently disconnected
fragments in the wrong place". The positional part of that does **not** reproduce — my first
measurement here used `trimesh.to_planar()`, which returns a *local centred* frame, making
every building look displaced. In world coordinates the hull sits inside the true footprint.
The magnitude error (≈4× too small) is real; the displacement claim was a measurement bug.

**Consequence:** buildings sit on pads far smaller than themselves, so most of the building
floats over ungraded terrain. The slice approach fixes it by construction — the ring *is* the
true cross-section, 150/150 pieces yield a clean planar polygon.

### P4 — `fillHole` caps real openings

`mr.fillHole` fills each boundary loop as an **independent disc**, so a courtyard nested inside
an outer footprint loop gets filled solid instead of left as a hole in an annulus. Measured
cap-area / true-section-area: median 1.10, p90 1.17, **max 1.39** (4 nested courtyards). Two
fix attempts (loop-nesting, then even-odd, both feeding earcut) made it **worse**
(+36.6 % → +56.6 %). The even-odd *rule* is right; the failure is in the earcut step. Note this
is the same "triangulate a polygon with holes" problem as the open-edge capping above — solve
it once, correctly, and it serves both.

## 4b. DEFECT PROVENANCE — every violation traced to the stage that creates it

Spec, taken as non-negotiable for radionuclide plume + dose work:
**closed + edge-manifold + vertex-manifold + intersection-free.**

`scripts/stage_audit.py` (per-building stages) and `scripts/provenance_run.py` (assembly
stages), Duxton, 2026-08-19. `bnd*` excludes the by-design open cut plane.

| stage | faces | bnd* | non-manif | bowtie | winding | self-X |
|---|---|---|---|---|---|---|
| 1 raw from OneMap | 14,224 | 40 | **10,284** | 2 | **20,520** | 196 |
| 2 after `seal_piece` | 7,814 | 0 | 4 | 2 | 8 | 142 |
| 3 **after DMC remesh** | 793,438 | 0 | **0** | **0** | **0** | **0** |
| 4 after decimate | 82,858 | 0 | 0 | 0 | 0 | 0 |
| 5 after XY shrink | 82,858 | 0 | 0 | 0 | 0 | 0 |
| 6 after slice | 67,481 | 0 | 0 | 0 | 0 | 0 |
| 7 each shell post-union/cap | — | 0 | 0 | 0 | 0 | 0 |
| 7b **all shells concatenated** | 336,522 | (cut planes) | 0 | 0 | 0 | **867** |
| 8 CDT terrain surface | 31,864 | (hole rims) | 0 | 0 | **0** | 0 |
| 9 `terrain_flat_base_solid` | 32,342 | (hole rims) | 0 | 0 | **320** | 0 |
| 10 FINAL assembly (in memory) | 368,864 | **494** | **0** | **0** | **320** | **945** |

### The four defects, each with a single identified source

**1. Not closed — 494 boundary edges. Source: `shell_rings` rejects (§4).**
43 loops, all planar, 40 of 43 under 0.05 m², <2 m² total. Fix: return them to be capped.

**2. Non-manifold edges — 3. Source: FLOAT32 FILE EXPORT, not the pipeline.**
The in-memory assembly has **zero**. Proven with the same geometry in four containers
(`scripts/container_test.py`):

| container | boundary | non-manifold |
|---|---|---|
| in-memory float64 | 494 | **0** |
| binary PLY (trimesh writes float32) | 489 | **3** |
| STL (float32) | 489 | **3** |
| **STL, recentred to origin first** | 494 | **0** |

At EPSG:3414 x ≈ 29,000 m, **float32 spacing is 2.0 mm** — coarser than the 0.1 mm weld
tolerance, so distinct vertices collapse on write. This is the *same* float32-at-large-
coordinates failure already documented for meshlib, now shown to hit our own file export.
**Fix: translate to a local origin before writing, translate back on read** (or write float64).
Note PLY does *not* save you — trimesh writes float32 there too. This is free and total.

**3. Winding conflicts — 320. Source: `terrain_flat_base_solid`.**
Stage 8 → 9 goes 0 → 320: the CDT terrain surface is consistently oriented, and adding the
side walls + flat bottom cap introduces the conflicts. Localised, self-contained, fixable by
getting the wall/cap winding right relative to the top surface.

**4. Self-intersections — 945. Source: 92 % building-vs-building.**
Shells alone (no terrain) already carry **867**. Terrain alone has **0**. The assembly adds
only 78. So this is **adjacent buildings overlapping each other**, not buildings vs terrain —
which corrects the earlier "73 % pad band" reading. Magnitude: median **9 mm**, 74 % under
5 cm, with a real tail of 91 pairs over 50 cm. Two sub-problems: hairline grazing (tolerance)
and genuine overlap (needs union).

**5. Bowtie vertices — 0** at every stage after `seal_piece`. Already satisfied.

### What this means

Three of the four spec violations have small, local, understood fixes (cap loops, recentre on
export, fix one function's winding). Only self-intersection is a real geometry problem, and it
is now precisely scoped: adjacent buildings, 867 pairs, mostly hairline.

## 4c. FIX EXPERIMENT (2026-08-19): wind + NM solved, bnd only 73 % solved

Ran on the claim that winding and boundary edges were "easy". Two of three held; the
third did not. `scripts/slice_cdt7_fixes.py`, Duxton 400 m, ~40 s per run.

| property | before | after | verdict |
|---|---|---|---|
| **closed** (boundary edges) | 494 | **134** | ❌ 73 % closed, NOT solved |
| **edge-manifold** (NM edges) | 3 | **0** | ✅ solved |
| **vertex-manifold** (bowties) | 0 | **0** | ✅ already clean |
| **consistently oriented** (winding) | 320 | **0** | ✅ solved |
| **intersection-free** (self-X) | 945 | 1,007 | untouched (see note) |

### Fix A — non-manifold edges: solved, one line

Translate to a local origin before writing the file, translate back on read. Verified
four ways (§4b). **3 → 0.**

### Fix B — winding: solved, but it was TWO bugs, not one

1. `terrain_flat_base_solid` winds the **wall** opposite to the top surface. Isolated
   (`scripts/wind_isolate_test.py`): flip wall+cap → 160, flip cap only → 160, **flip wall
   only → 0**, and `trimesh.repair.fix_winding` independently agrees and reports positive
   volume. Confirmed in-pipeline: 320 → 0.
2. After fixing (1) the assembly still showed 321. Located them
   (`scripts/locate_wind.py`): 30 connected groups, each perfectly planar at a single z,
   away from the domain wall — the **caps** added by Fix C. Cause: caps were built on NEW
   appended vertices, so they were topologically detached and could not be oriented against
   the wall faces they abut. Rebuilt to reuse the shell's own boundary vertex indices and
   derive orientation from the adjacent half-edge direction (a cap must traverse each shared
   edge opposite to the face already using it). **321 → 0.**

Two wrong guesses were made and discarded on measurement before this: "the whole wall+cap
is flipped" (gave 160) and "force every cap triangle to face −z" (gave 306).

### Fix C — boundary edges: 494 → 134, and the residual has a DIFFERENT cause

Capping the sub-threshold loops works and is not the blocker. Instrumented, all loops are
now accounted for: **357 shell boundary loops, 318 legitimately became CDT rings, 39
capped, 0 skipped for any other reason.** No shell loop is left uncapped.

The remaining 134 open edges are **not** uncapped loops — they are a **seam vertex-count
mismatch**:

```
terrain hole rims : 29,938 verts
shell rims        : 30,058 verts
exact matches     : 29,934
unmatched         : 124 shell-side + 4 terrain-side  -> 134 open edges
```

So a handful of shell rim vertices have no terrain counterpart. Two candidate causes, both
identified, neither yet fixed:

1. **`conf_holed`'s `add_v` dedups on `round(x,3)` (1 mm).** Two distinct shell rim vertices
   closer than 1 mm in XY collapse into ONE terrain vertex; the shell keeps both, so one
   shell edge has no partner.
2. **`buffer(0)` on the 3 recovered invalid rings can move vertices.** Max shell→terrain
   distance is 5.658 m, far too large for rounding alone, so at least one ring's geometry
   genuinely differs from the shell loop it came from.

This is a real, narrow, well-localised bug — but it is **not** the "just cap the loops"
fix I claimed. Capping was necessary and insufficient.

### Note on the self-X number moving 945 → 1,007

Not a regression. meshlib stores float32 internally, so at ~29,000 m coordinates its
collision test was itself precision-limited. Recentring gives it finer effective precision
and it finds ~60 pairs that were previously masked. **1,007 is the truer count**, and the
implication is broader: *recentre before every meshlib operation, not just before export.*

## 4d. PROXIMITY VOXEL REMESHING — measured feasible, and it likely kills the ring bug too

**The idea (user's):** stop remeshing buildings individually and then shrinking them 5 cm to
stop neighbours welding. Instead group buildings by proximity and remesh each GROUP as one
level set. Party walls fuse by construction. No shrink, no boolean union, terrain never
remeshed.

### Feasibility — grouping criterion matters enormously

First measurement used **bbox** proximity and looked fatal:

| domain | groups | max members | max group bbox (dense f32) |
|---|---|---|---|
| Duxton 400 m | 12 | 194 | 0.29 GB |
| Kent Ridge 900 m | 25 | 92 | 2.38 GB |
| CBD 2 km | 133 | **1,547** | **30.27 GB** |

At 2 km a single chain swallowed 1,547 of 1,853 buildings — straight back to the whole-domain
memory wall. **But bbox overlap massively over-links** (L-shaped and interleaved buildings
overlap boxes without touching). Re-measured with real mesh-surface proximity, which is what
actually decides whether a level set fuses two surfaces:

| domain | groups | max members | groups >1 | max group bbox (dense f32) |
|---|---|---|---|---|
| Duxton 400 m | 35 | 49 | 21 | **0.02 GB** |
| Kent Ridge 900 m | 78 | 22 | 10 | **0.18 GB** |
| **CBD 2 km** | **542** | **161** | 145 | **1.11 GB** |

The 2 km case — 30 GB as a whole domain, 30 GB under bbox grouping — is **1.11 GB** for the
worst group, and that is the DENSE upper bound; OpenVDB's narrow band stores only a shell
around the surface, so the real figure is well below it. **Feasible, with headroom, and 542
independent groups are embarrassingly parallel.** (`scripts/proximity_real.py`)

Caveat, stated: grouping used vertex-vertex distance, which UNDER-estimates surface proximity
(this project has measured an 87× error once), so real groups may be somewhat larger. The
bbox→surface swing is 30 GB → 1.11 GB, so the conclusion survives a lot of slack, but the
largest few groups should be re-checked with the exact point-to-triangle test in
`terrain.py::_surface_gap` before building on it.

### It probably also removes the ring bug

Checked directly (`scripts/why_invalid.py`): across all 287 Duxton pieces, the number of
invalid slice loops on an INDIVIDUAL piece is **zero**. All 8 invalid loops (including the 3
real buildings) appear only after the **union of shrunken neighbours** is re-sliced. They are
an artifact of the shrink+boolean workaround, not of the buildings.

Remove the shrink and the union — which is exactly what proximity remeshing does — and that
class of loop plausibly disappears, taking `buffer(0)`, the ring divergence, and a large share
of the building-vs-building self-intersections with it. Not yet proven, but it is the single
change that attacks self-X, the ring bug, and the seam mismatch at once.

### What did NOT work this round: exact loop splitting

Replaced `buffer(0)` with a stack-based split at repeated vertices, to keep coordinates exact.
**Made it worse: open edges 134 → 509**, rings back to 299, self-X back to 1,966 — it
recovered none of the 3 big buildings. Reason, now known: those loops have no repeated vertex
to split at, and more importantly they do not exist at the per-piece stage at all, so the
whole touching-vs-crossing framing was aimed at the wrong stage of the pipeline. Reverted;
`scripts/slice_cdt7_fixes.py` remains the best configuration.

**Best config to date (`data/wt_raw_test/duxton_400m_slice_cdt_v7.stl`):**
closed ❌ 134 · edge-manifold ✅ 0 · vertex-manifold ✅ 0 · oriented ✅ 0 · intersection-free ❌ 1,007

## 4e. GROUP REMESH — BUILT AND TESTED. Four of five spec properties now clean.

`scripts/slice_cdt9_group.py`. Replaces per-piece remesh + 5 cm shrink + boolean union with:
group by real surface proximity → build ONE level set per group → slice → CDT. No shrink,
no boolean union, terrain never remeshed.

### Result (Duxton 400 m, 287 pieces → 35 groups, **18 s** end to end)

| property | per-piece + shrink (v7) | **group remesh** |
|---|---|---|
| **closed** | ❌ 134 boundary edges | ✅ **0** |
| **edge-manifold** | ✅ 0 | ✅ **0** |
| **vertex-manifold** | ✅ 0 | ✅ **0** |
| **consistently oriented** | ✅ 0 | ✅ **0** |
| **intersection-free** | ❌ 1,007 | ❌ **36** |
| faces | 368,864 | 250,616 |
| runtime | 38 s | **18 s** |

Fewer faces, half the runtime, and `allEdgesTwice=True` — a genuinely closed, orientable,
manifold surface. **Self-X down 1,007 → 36 (−96 %).**

### The seam became exact, in both directions

```
terrain hole rims 12,490 verts | shell rims 12,490 verts
shell → terrain : exact 12,490/12,490   max 0.000 m
terrain → shell : exact 12,490/12,490   max 0.000 m
```

The 134-open-edge seam mismatch is **gone**, and not by patching it — the union of shrunken
neighbours that produced the pathological rings no longer exists. Ring count fell 302 → 42,
boundary loops 357 → 98. Exactly the prediction in §4d.

### Where the residual 36 come from — measured, not assumed

| geometry | self-X |
|---|---|
| all shells together, **no terrain** | **3** |
| full assembly | 38 |

So **~92 % of the residual is building-vs-TERRAIN**, not building-vs-building. Group remesh
essentially eliminated the building-vs-building class (867 → 3). Penetration is deep (median
5.50 m, max 6.92 m), consistent with a group whose flat pad sits well below local grade while
the terrain outside its ring rises back up and cuts into the building — the rigid-mesh-on-a-
slope limit from TERRAIN.md, now amplified because a 49-member group takes `min` over a much
larger area.

Widening the grouping gap does **not** help (0.5 m → 38, 2.0 m → 38, 5.0 m → 41), confirming
these are not missed groupings. The fix belongs in terrain placement, not in grouping:
per-group pad closer to local grade, an apron grading terrain down to the pad over a collar
(`flatten_pads` already has `apron_m`), or punching the terrain hole slightly larger than the
ring.

### Robustness

openEdges = 0 at **every** gap tested (0.5 / 2.0 / 5.0 m), so closure is not a knife-edge
result. Group counts: 35 / 30 / 16, max members 49 / 49 / 103.

**Artifact:** `data/wt_raw_test/duxton_400m_groupremesh.stl`

### Still to check before this is more than a prototype

- Run it on Kent Ridge 900 m and CBD 2 km (feasibility measured in §4d, never executed).
- Grouping uses vertex-vertex distance, which under-estimates surface proximity; the exact
  point-to-triangle test in `terrain.py::_surface_gap` should be used for the largest groups.
- Per-building identity is lost inside a group (fine for CFD/dose, matters if attributes are
  ever needed).

## 4f. MULTI-DOMAIN TEST — Duxton does NOT generalize, and the residual self-X is DECIMATION

Ran the group-remesh prototype across domains with the shells-only vs full-assembly split on
every run (`scripts/group_multidomain.py`).

| domain | pieces | groups | max mem | closed | NM | bowtie | wind | selfX | peak RSS |
|---|---|---|---|---|---|---|---|---|---|
| **Duxton 400 m** | 287 | 35 | 49 | ✅ **YES** | 0 | 0 | 0 | **38** | 0.86 GB |
| **Kent Ridge 900 m** | 118 | 78 | 22 | ❌ **NO (1,849)** | **5** | 0 | **8** | **543** | 2.30 GB |

Duxton's clean sweep was **not representative**. Kent Ridge — hilly, with bridges and podiums —
breaks three properties that Duxton passed. Testing more domains was the right call.

### Cause 1 (the big one): decimation creates the self-intersections

Kent Ridge's shells-only self-X was 308, and one group's OWN solid had selfX=8 — which a level
set cannot produce. Isolated it on the 8 largest Kent Ridge groups
(`scripts/decim_selfx.py`), measuring self-X immediately after `gridToMesh` and again after
`decimateMesh`:

| maxError | total self-X over the 8 groups | total faces |
|---|---|---|
| **no decimation** | **0** | 5.05 M |
| **0.25 (what we ship)** | **53** | 484 k |
| **0.10** | **0** | 958 k |
| **0.05** | **0** | 1.61 M |

**Every group's raw level set is self-intersection-free. All 53 are created by decimation at
maxError = 0.25.** Dropping to 0.10 removes all of them for ~2× the faces. Per-group detail:

```
grp mem  faces_raw sxRaw |  0.25          |  0.10          |  0.05
 42  22  1,233,608    0  | 123,696f sx= 0 | 239,112f sx=0  | 396,220f sx=0
  2   7    956,176    0  | 114,180f sx= 8 | 220,242f sx=0  | 329,956f sx=0
 11   5  1,756,788    0  | 127,546f sx=39 | 264,858f sx=0  | 490,872f sx=0
 57   2    283,464    0  |  34,354f sx= 2 |  64,582f sx=0  | 101,234f sx=0
 12   2    499,556    0  |  30,824f sx= 4 |  66,960f sx=0  | 128,682f sx=0
```

This supersedes the Duxton-only reading in §4e that "~92 % is building-vs-terrain". That split
holds for Duxton (flat, 35 of 38), but on Kent Ridge shells-only is 308 of 543 — because
decimation damage scales with building complexity, and Kent Ridge has the large multi-tier
structures. **meshlib's decimation preserves manifoldness by construction but NOT
non-self-intersection** — a distinction worth remembering, and consistent with `adaptivity>0`
having been rejected earlier for the same reason.

### Cause 2: dropped overlapping rings reopen the mesh

Kent Ridge: `2 overlapping rings dropped` (Duxton: 0). A dropped ring means no terrain hole,
so that shell's rim has nothing to attach to — 1,849 boundary edges, plus the 5 NM and 8
winding conflicts that follow from an unclosed surface. Group remesh reduced ring overlap
dramatically but did not eliminate it: two groups still produce rings that overlap at the cut
plane, presumably where a bridge or podium spans between groups that were not merged.

### Cause 3: building-vs-terrain (unchanged, real, terrain-side)

Duxton 35/38; deep penetration (median 5.5 m). Belongs to pad placement, not meshing — a group
takes `min` DTM over all members, so a large group on relief sits well below local grade.

### Where that leaves the general picture

| defect | general cause | fix |
|---|---|---|
| self-X (within group) | **decimation at maxError 0.25** | use 0.10 (2× faces) |
| self-X (vs terrain) | pad = min over group, on relief | per-group pad / apron grading |
| not closed, NM, winding | **dropped overlapping rings** | merge the overlapping groups, or keep the ring and split it |

All three now have an identified mechanism rather than a symptom. None is a mystery.

### Cause 4 (new, found by running Queenstown): the prototype does not fit in memory at scale

Queenstown (2.4 x 2.0 km, the largest domain tried) was **stopped at ~9 minutes with RSS
climbing monotonically 1.1 -> 2.2 -> 3.8 -> 5.1 -> 5.7 -> 6.8 GB** on a 9.7 GB box, with
available memory down to 743 MB and swap about to engage. Killed deliberately rather than let
it thrash. CBD 2 km never started.

This is NOT the level-set memory the §4d feasibility model predicted (worst group 1.11 GB
dense for CBD). It is **accumulation across groups**, and it is the prototype's bookkeeping,
not the algorithm:

- `sealed` holds every raw sealed piece for the whole domain,
- `trees` holds a cKDTree per piece,
- `items` holds every group's finished shell simultaneously (78+ shells at 100k+ faces each on
  Kent Ridge; Queenstown is far larger),
- nothing is freed until the very end.

The per-group peak really is small — the measured feasibility numbers stand. The fix is to
stream: build a group, slice it, keep only its rings + shell, drop the level set and the
group's source meshes, and free `sealed[i]` as each piece is consumed. That is a prototype
restructuring, not a change to the method.

**Consequence for the earlier claim:** §4d said proximity remeshing is feasible at 2 km with
headroom. That remains true *per group*, but is **unproven end-to-end** — no domain larger
than Kent Ridge (900 m) has completed. Duxton 0.86 GB and Kent Ridge 2.30 GB both finished.

### Domains completed

| domain | area | result |
|---|---|---|
| Duxton 400 m | 0.16 km² | completed, closed, selfX 38 |
| Kent Ridge 900 m | 0.81 km² | completed, NOT closed (1,849), selfX 543 |
| Queenstown 2.4 km | 4.8 km² | **stopped, out of memory** |
| CBD 2 km | 4.0 km² | not run |

## 4g. ZERO-DECIMATION ISOLATION RUN (Kent Ridge) — decimation caused TWO of the three defects

Run with `DEC_ERR=0` to remove decimation as a variable entirely rather than tune it to 0.1
(`scripts/group_nodecim.py`, `DOM=kentridge DEC_ERR=0`). Tuning would have masked the coupling
below; removing it exposed it.

| measurement | maxError 0.25 | **no decimation** |
|---|---|---|
| self-X **within** each group | 8, 39, 2, 4, … (53 over 8 groups) | **0** on every group |
| self-X **shells only** (between groups) | 308 | **470** |
| **overlapping rings dropped** | **2** | **0** |
| seam: terrain rim verts vs shell rim verts | 34,955 vs 36,801 (off by 1,846) | 113,219 vs 113,226 (**off by 7**) |
| seam exact matches (terrain -> shell) | — | **113,217 / 113,219**, max 0.256 m |
| group solids | 78, 0 failed | 78, 0 failed, 76 s |
| shell faces | ~0.86 M | 9,049,260 |

### What this isolates

1. **Decimation caused the ring drops too, not just the self-X.** This was not in the earlier
   diagnosis. Decimation moves vertices enough that two rings overlap at the cut plane; the
   overlapping ring is then dropped; no terrain hole is punched; the shell's rim has nothing to
   attach to. That is the whole 1,849-boundary-edge / 5-NM / 8-winding failure on Kent Ridge.
   With decimation off: **0 rings dropped, seam off by 7 vertices instead of 1,846.**

2. **Every individual group solid is intersection-free.** Confirms the level set is always
   clean and that `decimateMesh` is the sole within-group source.

3. **Between-group self-X is the only remaining building-geometry defect**, and it went UP
   (308 -> 470). Not a regression: full-resolution meshes expose overlaps that decimation was
   shrinking away. **470 is the truer number.** Cause is unchanged from §4e — grouping uses
   vertex-vertex distance at GAP=0.5 m, which over-reports gaps (documented 87x error), so
   deeply interpenetrating pairs whose vertices are far apart are never merged. The fix is the
   exact point-to-triangle test in `terrain.py::_surface_gap`.

### Caveats

- 9 M faces is a **diagnostic configuration, not shippable** — the question it answers is "what
  breaks when decimation is not in the loop", nothing more.
- **The final assembly audit did not complete.** meshlib's `findSelfCollidingTriangles` over
  ~9.2 M faces ran >400 s at a stable 7.4 GB without returning. The seam figures above
  (2 unmatched terrain verts, ~9 unmatched shell verts) imply the assembly's open-edge count
  would be very low, but that is a **projection, not a measurement** — it has not been verified.
- An earlier attempt at this run had to be killed: the audit's Python half-edge set held ~27 M
  tuples and pushed RSS past 6.5 GB. Vectorised it (numpy `unique` on directed edges); the
  pipeline itself stays at ~2.5-3 GB. Part of the earlier Queenstown memory scare was this
  instrumentation, not only the prototype's bookkeeping — worth correcting.

### Revised cause table

| defect | cause | status |
|---|---|---|
| self-X within group | `decimateMesh` | **confirmed sole cause** (0 without it) |
| dropped rings -> open/NM/winding | `decimateMesh` moving ring vertices | **newly confirmed** (0 without it) |
| self-X between groups | vertex-distance grouping misses deep overlaps | **only remaining defect**; fix = exact surface gap |
| self-X vs terrain | pad = min DTM over group, on relief | unchanged, terrain-side |

## 4h. Assembly audit landed — removing decimation is NOT a free win

The Kent Ridge `DEC_ERR=0` assembly audit finished (peak RSS **8.5 GB**, ~10 min in
meshlib's self-collision test alone):

| | maxError 0.25 | **no decimation** |
|---|---|---|
| faces | 906,503 | **9,173,848** |
| **closed** | ❌ NO (**1,849**) | ❌ NO (**2**) |
| **NM edges** | 5 | **116** |
| **winding** | 8 | **228** |
| **selfX** | 543 | **800** |
| peak RSS | 2.3 GB | **8.5 GB** |

Closure is essentially solved (1,849 -> **2**), confirming decimation caused the ring drops.
But NM and winding got **~23-28x worse**, which was not predicted. Removing decimation fixes
one defect class and inflates two others — so it is a diagnostic setting, not a fix.

### Alternative explanation tested and REFUTED

Hypothesis: at 9.2 M faces the 0.1 mm audit weld merges distinct vertices, manufacturing the
NM/winding. Swept the weld tolerance on the exported STL (`scripts/weld_sens.py`):

```
weld 1e-6 m (1 micron): boundary=2  NM=116  winding=228
```

**Identical to the 1e-4 result.** These are real geometry, not an instrumentation artifact.

### The `add_v` 1 mm quantisation — hypothesis SUPPORTED at the seam

`conf_holed`'s `add_v` dedups CDT vertices on `round(x,3)` — **1 mm**. Without decimation the
rings are ~3x denser (terrain rim verts 34,955 -> 113,219), so far more ring vertices fall
within 1 mm of each other: merged into one terrain vertex while the shell keeps both, which
shows up as 3-face edges and half-edge conflicts at the seam.

Re-ran with the quantisation raised to 1e-6 (`scripts/group_addv.py`, `ADDV_DEC=6`, which also
moves the shell snap and pad rounding to match):

| seam measurement | `add_v` 1 mm | **`add_v` 1e-6** |
|---|---|---|
| terrain solid open edges | 113,220 | 113,228 |
| shell open edges | 113,225 | **113,228 (equal)** |
| terrain-hole vert -> nearest shell vert | exact 113,217 / 113,219 | **exact 113,224 / 113,224** |
| max distance | 0.256 m | **0.000 m** |

**The seam becomes exactly matched in both directions.** Ring/cap behaviour is unchanged
(85 constraints, 0 dropped; 8 loops capped vs 10). This is strong support for the mechanism.

### Prediction REFUTED — the seam fix gives closure but NOT manifoldness

The audit finished:

```
RESULT kentridge ASSEMBLY | faces=9,173,862 | closed=YES | NM=112 | wind=224
```

| | `add_v` 1 mm | **`add_v` 1e-6** |
|---|---|---|
| **closed** | ❌ NO (2) | ✅ **YES** |
| NM edges | 116 | **112** |
| winding | 228 | **224** |

**Closure is achieved** — the first fully closed Kent Ridge assembly in this whole
investigation. But NM and winding barely moved (116->112, 228->224). I predicted they would
fall with the seam fix; they did not. The seam and the non-manifoldness are independent
problems, and the `add_v` quantisation only ever explained the seam.

**Lead on what actually causes them:** 224 = exactly 2 x 112. An edge shared by 4 faces
contributes exactly 2 duplicate half-edges, so **NM and winding are almost certainly one
defect counted two ways** — 112 edges with 4 faces on them, not two separate problems.
Since the seam is now exact, these must be interior. Candidates, untested:
- two groups whose surfaces are exactly coincident (welded at 0.1 mm) but were never merged,
- the near-zero-area fan caps added for degenerate loops (8 capped on this run).

That is the next thing to locate spatially — the same way `scripts/locate_wind.py` located the
earlier winding conflicts to the caps.

## 5. Where the CGAL / GSoC dual-contouring work actually fits

I dismissed this too quickly the first time, on the strength of one sample, and framed it as
"not our bottleneck". The provenance table above says something more interesting.

**DMC is not a source of defects here — it is the stage that removes them.** Stage 2 → 3:
non-manifold edges 4 → **0**, winding conflicts 8 → **0**, self-intersections 142 → **0**.
Raw OneMap geometry arrives with 10,284 non-manifold edges and 20,520 winding conflicts;
`seal_piece` handles most, and the DMC remesh cleans the rest completely. Across 90 isolated
buildings in two domains it produced 2 non-manifold edges total, and across the 255 solids of
a full Duxton run, **zero**.

**But "empirically clean on our sample" is not "guaranteed manifold", and for a dose
calculation the difference matters.** meshlib's `gridToMesh` offers no manifoldness guarantee;
it happened to be clean on the geometry we fed it. The report's whole point is that DMC
*without* the extra machinery does not guarantee manifoldness — ambiguous MC configurations
and tunnel cases can still produce non-manifold edges — and it implements TMC integration plus
tunnel recovery to close exactly that gap, in CGAL's `Isosurfacing_3`.

So the honest position: it is not where our current 3 non-manifold edges come from (those are
float32 export, §4b), but it **is** the reference for the one stage of our pipeline that has
no guarantee and that we are entirely depending on. If a domain ever shows DMC-origin
non-manifold edges — a tunnel-like configuration through a building, which is plausible for
overhangs, archways and podium voids — this is the fix, and CGAL is where a
guarantee-bearing implementation already exists. Worth reading properly rather than shelving.

The one measured caveat that stands: `adaptivity > 0` on `gridToMesh` looks like a free 8×
face-count cut and **introduces self-intersections** (28–105 measured). Keep it at 0.

## 6. What to research — and the distinction I blurred in the first draft

You called this out correctly: the pointers below are **not** fixes for §4's bugs. They are a
different bet. Keeping them separate:

### Track A — finish our own pipeline (no research needed, it's engineering)

§4 is now: cap ~43 tiny planar loops, and characterise + reduce the remaining 945 self-X. The
only genuinely unsolved sub-problem is **robust triangulation of a polygon with nested holes**
(shared with P4), where our own attempts failed twice. That one *is* worth a search:
"constrained Delaunay polygon with holes", "earcut hole winding order", "even-odd fill rule
triangulation" — though the likely answer is to stop hand-rolling it and feed rings + hole
seeds to `triangle`, which already does exactly this correctly for our terrain.

### Track B — replace the final stage with an off-the-shelf guarantee

This is a *different strategy*: accept a bounded approximation instead of chasing exactness.
Worth researching precisely because it makes §4's remaining self-X irrelevant rather than
fixed. Both of these ingest self-intersecting input **by design**:

- **CGAL 3D Alpha Wrapping** (`CGAL::alpha_wrap_3`) — provably watertight, manifold,
  intersection-free, within a bounded Hausdorff distance of the input. Two knobs (alpha,
  offset). Essentially Ansys FTM's wrap but offline and controllable, which sidesteps the
  export-requires-volume-mesh problem entirely. **Search:** "CGAL alpha wrap 3", "Portaneri
  Alliez alpha wrapping SIGGRAPH 2022", "alpha wrapping performance large scene".
- **fTetWild** — built to ingest self-intersecting non-manifold input and emit a valid tet
  mesh with a manifold boundary. Doubly relevant since OpenMC wants a volume anyway.
  **Search:** "fTetWild self-intersecting input", "Hu Schneider tetrahedral meshing in the
  wild", "fTetWild envelope tolerance".
- Also: **ManifoldPlus**, **libigl `remesh_self_intersections`** (exact mesh arrangements).

The bar to beat: our voxel path needs ~30 GB for 2 km at 0.5 m. Ask of any candidate: does it
survive 2 km of city at metre-ish tolerance, and what's the memory curve?

### Track C — your OpenMC / DAGMC path has its own requirements and tooling

OpenMC's CAD route is **DAGMC**, which has a hard watertightness requirement and ships
dedicated tooling. **Search:** "DAGMC make_watertight", "DAGMC check_watertight", "MOAB
imprint merge", "OpenMC DAGMC CAD workflow". Worth establishing early whether DAGMC's
criterion is even the same as Ansys' — I'd expect it to care about closure and shared-surface
imprinting more than self-intersection, but that is a guess, not a measurement. If DAGMC
tolerates what Ansys rejects, the whole priority order changes.

## 7. Do NOT research these — already refuted with measurements

- Increasing the XY shrink to separate party walls — U-shaped, 0.05 is optimal.
- `adaptivity > 0` on `gridToMesh` — cuts faces 8× but introduces 28–105 self-intersections.
- Standard Marching Cubes instead of Dual MC — **32× worse** (4,734 vs 146 non-manifold on the
  same test set).
- Manifold dual contouring generally — see §5, not our bottleneck.
- Snapping/iteration tuning in snappyHexMesh — more iterations made bad faces *worse* (8→13);
  finer cells also worse (8→90).
- Coincident domain walls as a cause of snappy's bad faces — insetting the background box 80 m
  clear still left them.
- `mr.fixSelfIntersections` — its own docs say it converts to voxels and back, i.e. it *is* a
  voxel remesh, so it gives up the fidelity we're trying to keep.
- `mr.localFixSelfIntersections` — measured on 10 pieces: Relax cleaned 3/10, CutAndFill 4/10,
  and CutAndFill lost **53 %** of one building's volume.
- Triangle Steiner points on ring segments (`pY` vs `pYY`) — byte-identical output.
- Whole-domain voxel remesh at 0.5 m for a 2 km domain — ~30 GB, same class as the FTM memory
  problem it was meant to avoid.

---

## 8. Honest position

Closer than the first draft implied. The ring-recovery fix took the artifact from
869 open / 1,966 self-X to **494 open / 945 self-X**, and the residual open edges are now
provably cosmetic (43 flat slivers, <2 m² total, no real building affected). Self-X is the
only substantive unknown left, and it has not been re-characterised since the fix.

Even if Track B wins, Track A is worth finishing: it fixes P3, which is a **live accuracy bug
in the shipped pipeline** (footprints 4.3× too small), and a cleaner input makes any wrap or
repair step behave better than feeding it raw's 11,334 self-intersections.

Artifacts: `data/wt_raw_test/duxton_400m_slice_cdt.stl` (before) and
`duxton_400m_slice_cdt_ringfix.stl` (after). Scripts in `scripts/`.

---

# 9. Session 2026-08-21 — the grouping architecture was scaffolding, and fTetWild

Two independent findings, both of which invalidate large parts of the design above.

## 9.1 The level set is SPARSE — grouping was never necessary

`§4d`'s claim that *"level-set cost scales with bounding VOLUME, not member count"* is **WRONG**,
and it is the premise the whole proximity-grouping architecture was built on.

Measured (`scripts/sparse_test.py`) — level-set one building, then the same building with a
far-away speck added so the bbox inflates while the SURFACE AREA is unchanged:

| input | dense-equiv voxels | time | ΔRSS |
|---|---|---|---|
| building alone | 2,486,846 | 0.12 s | +48 MB |
| + speck at 5× bbox | 507,255,091 | 0.07 s | +2 MB |
| + speck at 10× bbox | 3,109,236,559 | 0.09 s | +2 MB |

1250× the bounding volume, time went *down*. `meshToLevelSet` AND `gridToMesh` are both
narrow-band sparse; cost is bound by **surface area**. The 372.9 s in the v2 Duxton log was
`trimesh.split()` (exactly what `_split_fast`'s docstring says it replaced), not voxel work.

**Consequence — one global level set for the whole domain** (`scripts/global_ls.py`):

| domain | pieces | faces | levelset | extract | open | NM | wind | selfX | peak RSS |
|---|---|---|---|---|---|---|---|---|---|
| Duxton | 287 | 2,550,584 | 1.2 s | 0.4 s | 0 | 0 | 0 | 0 | 1.6 GB |
| Kent Ridge | 118 | 10,719,968 | 3.5 s | 1.9 s | 0 | 0 | 0 | 0 | 4.9 GB |

Kent Ridge in **18 s**, versus not finishing in 16 minutes under group-remesh. Grouping,
proximity metrics, the voxel budget and adaptive-VS can all be deleted; every
between-group self-X ceases to be a category because there are no groups.

### Verify by STRICT EXACT-POSITION weld, not by rounding
Counting on the mesh's own indices gave 0/0/0/0, but an exact float32-position weld (what a
consumer's STL import does) found NM=4 (Duxton) / NM=6 (Kent Ridge), `wind = 2 × NM` in both.
Rounding to 1e-4 to weld is **finer than float32 spacing at 29 km coords (~2 mm)** and
manufactures fake defects — do not round.

### Iso-shift removes the residual pinches
A pinch is where the SDF is exactly tangent to 0. Shifting `isoValue` off zero removes the
tangency. **Not monotonic** — it is generic-position perturbation, so it is a *search*, not a
constant (Duxton clean at 0.02, Kent Ridge at 0.1). ~20 s per attempt.

| Kent Ridge iso | faces | NM | wind | selfX | Δvolume |
|---|---|---|---|---|---|
| 0.000 | 10,719,968 | 6 | 12 | 0 | — |
| 0.030 | 11,040,844 | 2 | 4 | 3 | +0.33 % |
| **0.100** | 11,099,856 | **0** | **0** | **0** | +1.03 % |

### Relief, for the record
Duxton relief **6.8 m** over 400 m; Kent Ridge **40.0 m** over 900 m. Duxton is nearly flat,
so its results do NOT generalise — treat it as a smoke test, never as evidence.

## 9.2 The real complaint is ALIASING, not fidelity

A 30 cm wall in a 0.5 m grid: the solid interior is thinner than the sample spacing, so the
wall exists only where a grid point happens to land inside it → *pillar-gap-pillar*. This is
Nyquist. It does **not** degrade gracefully at any voxel size — going coarser destroys the
feature more thoroughly, not less. Measured on Duxton: median 4.2 % of a building's faces sit
on sub-0.5 m-thick walls, worst 26.1 % (`scripts/find_thin.py`).

## 9.3 fTetWild — the only method that survives the spec

`wildmeshing` is already in `.venv`. It never samples on a grid; it inserts the actual input
triangles and keeps the output within an envelope ε of the input, so a sub-ε feature cannot be
missed between samples. It also produces the COARSEST mesh within ε, so flat walls stay cheap —
which is why it needs no decimation step.

Single building (piece 26, worst thin-wall fraction), `scripts/ftw_test.py`:

| method | faces | open | NM | wind | selfX | volume | cov<10cm | time |
|---|---|---|---|---|---|---|---|---|
| DMC voxel 0.5 | 116,984 | 0 | 0 | 0 | 0 | 46,501 | 20.6 % | 0.1 s |
| DMC voxel 2.0 | 7,020 | 0 | 0 | 0 | 0 | 47,686 | 13.0 % | 0.0 s |
| ManifoldPlus d=8 | 168,732 | 0 | 870 | 1,740 | 243 | 46,478 | 80.6 % | 4.1 s |
| ManifoldPlus d=10 | 2,496,942 | 0 | 24,816 | 49,632 | 415 | 46,475 | 99.2 % | 51.1 s |
| **fTetWild its=0** | **1,662** | 0 | 0 | 0 | 0 | 46,473 | **100.0 %** | **7.0 s** |

**`max_its=0` is a pure win** (`scripts/ftw_tune.py`) — the optimization stage exists to make
well-shaped TETS, which we discard, keeping only the boundary surface. The envelope guarantee
holds from insertion onward.

| config | faces | tets | selfX | cov<10cm | time |
|---|---|---|---|---|---|
| baseline (its=80, q=10) | 6,556 | 10,643 | 0 | 100.0 % | 55.3 s |
| **its=0** | **1,662** | 2,667 | 0 | 100.0 % | **7.0 s** |

7.9× faster, 4× FEWER faces, identical volume. Set `max_threads=1` when running per-building
in a process pool or TBB oversubscribes badly.

### ManifoldPlus — REJECTED, with reasons
Cloned + built at `ManifoldPlus/build/manifold`. Octree occupancy (voxel occupied if it
*intersects* a triangle — no signed distance), then projection back onto the reference.
- **Raw soup input is catastrophic**: volume **138 m³** vs the correct 46,475. Its advertised
  "zero-volume structure preservation" faithfully reproduces our double-sided capture as a
  zero-thickness shell. `seal_piece` remains mandatory.
- **Sealed input self-intersects** (243–536). Its guarantee is *inversion-free*, which is a
  local no-triangle-flip constraint — **not** intersection-free.
- Its manifoldness comes from splitting vertices at coincident positions (84,368 → 83,580
  welded), which a positional weld undoes by construction.
- License is non-commercial only.

### Whole-domain beats per-building + boolean union
Both tested on Duxton (`scripts/ftw_domain.py`, `scripts/ftw_union.py`):

| | faces | comps | open | NM | wind | selfX | volume | cov | time | RSS |
|---|---|---|---|---|---|---|---|---|---|---|
| **one fTetWild call** | 49,890 | 78 | 0 | 798 | 1,596 | **0** | 872,907 | 98.4 % | 239 s | 654 MB |
| per-bldg + exact union | 212,904 | 329 | 0 | 384 | 776 | **16,641** | 812,285 | 93.8 % | 59 s | 519 MB |

One call wins on every axis except wall-clock, and needs **no fusion step at all**. Scaling was
the feared risk and it is fine: 239 s / 654 MB for the whole domain.

The union path was implemented with all four hard-won rules (binary merge tree not sequential
fold; `mr.boolean` only on pairs that ACTUALLY intersect per `findCollidingTriangles`, plain
concatenation otherwise; nothing domain-spanning early; check
`res.mesh.topology.numValidFaces() > 0` because `valid()` can be True with zero faces) and
still lost. Per-building fTetWild in a 10-process pool is fast (287 solids in 53 s).

## 9.4 The one remaining defect: 798 base-cap pinches

Every bad edge has **exactly 4 faces** (X-junction), everything else exactly 2, zero open edges.

**Two hypotheses tested and REFUTED — do not retry:**
1. *Nudge coincident vertices apart.* Pushing 1 mm inward gave Duxton selfX 0 → **51**, Kent
   Ridge 0 → **156**, and KR NM got *worse* (6 → 13). A pinch IS a knife edge — the solid has
   no thickness there, so any inward move punches through the far wall.
2. *Coincident party walls between adjacent shophouses.* A 3 cm per-piece XY inset moved
   798 → **792**. Not party walls. (Components did split, 78 → 149, so the inset worked; the
   pinches simply are not there.)

**Where they actually are** — 798 edges, mesh spanning z −11.2 … 72.6:
`p25 = −10.27, median = −5.25, p75 = −1.60`; 195 within 0.5 m of the base; edge lengths are
long (median 2.77 m, max 12.24 m), not slivers. **~75 % sit in the bottom ~10 m of an 84 m mesh
— they are concentrated at the BASE, not in the preserved facades.**

**Hypothesis 3 (`seal_piece`'s fillHole base cap) — TESTED and REFUTED**
(`scripts/pinch_cause.py`). Face normals at the bad edges are **74.7 % near-VERTICAL**
(|nz|<0.1) and only 16.8 % near-horizontal, against 31.2 % horizontal for the mesh as a whole —
they are on facades, not caps. Multi-loop pieces *are* enriched 2.5× (5.32 vs 2.14 pinches
each), so nested-courtyard fillHole is a real contributor, but a minority one: 489 of 798
pinches come from single-loop pieces, and 182 of 287 pieces own at least one.

Also correcting the "concentrated at the base" read above: the domain mesh is recentred by
subtracting mean z, so a pinch at z = −5.25 is raw z ≈ +6 m — low-to-mid facade, not the cap.

**Hypothesis 4 (party walls INSIDE one `_BATCHID` piece) — TESTED and REFUTED**
(`scripts/pinch_isolate.py`). The six worst pinch-owning pieces, run through fTetWild
**in isolation**:

| piece | faces | pinches in domain | isolated: open | NM | wind | selfX |
|---|---|---|---|---|---|---|
| 208 | 162 | 15 | 0 | **0** | 0 | 0 |
| 174 | 88 | 14 | 0 | 2 | 4 | 0 |
| 173 | 256 | 13 | 0 | **0** | 0 | 0 |
| 197 | 58 | 13 | 0 | **0** | 0 | 0 |
| 166 | 112 | 13 | 0 | 2 | 5 | 0 |
| 230 | 176 | 12 | 0 | **0** | 0 | 0 |

12–15 pinches each in the domain, **0–2 alone**. The pinches are **INTER-piece contact**.

### The actual mechanism: the inset must exceed ε
Hypothesis 2's inset was the right idea set wrong. The inset was **3 cm** while **ε = 5 cm**.
fTetWild's guarantee is that the output stays within ε of the input, so it is *free* to close
any gap smaller than ε — the 3 cm separation was invisible to it. This is why the inset split
components (78 → 149) yet left the pinch count unchanged (798 → 792).

**Rule: per-piece inset must be > ε, or ε must be < the inset.** Untested at time of writing.

Note the standing tension: DMC has few pinches *because* it destroys thin features; fTetWild
preserves them, and preserved thin features are where surfaces come close enough to touch.

## 9.5 Still untested
Terrain integration with fTetWild; Kent Ridge whole-domain fTetWild; whether a larger ε merges
the pinches; whether the pinch-owning pieces are the multi-loop ones.

### 9.6 CORRECTION — the inset/dilate tests were implemented wrong

`ftw_domain.py`'s original `SHRINK` was a **scale about each piece's XY bbox centre**
(`factor = 1 - d/half_extent`). That does NOT displace uniformly: a vertex on the bbox edge
moves the full `d`, one halfway in moves `d/2`, one near the centre moves ~0. Contacts inside a
combined multi-building `_BATCHID` piece sit nowhere near the bbox edge, so they were barely
displaced at all.

**Every inset/dilate number below the 15 cm row is therefore not a valid test of the idea:**

| config (BROKEN offset) | faces | comps | NM | wind | selfX | volume | cov |
|---|---|---|---|---|---|---|---|
| baseline, no offset | 49,890 | 78 | 798 | 1,596 | 0 | 872,907 | 98.4 % |
| inset 3 cm | 51,496 | 149 | 792 | 1,588 | 0 | 865,656 | 98.2 % |
| inset 15 cm | 48,100 | 279 | **178** | 356 | 3 | 788,378 | 92.6 % |
| dilate 5 cm | 50,802 | 69 | 806 | 1,616 | 3 | 877,616 | 98.8 % |

15 cm "worked" only because it is large enough that even mid-piece vertices move a usable
amount. Replaced with a **true per-vertex normal offset** (`SHRINK>0` insets, `<0` dilates).

### 9.7 What a bad edge ACTUALLY is (measured, `scripts/inspect_edge.py`)

Should have been the FIRST thing measured rather than the seventh hypothesis. Of 798 bad edges:

- **772 (97 %)** have a pair of near-**OPPOSED** normals → two sheets back-to-back,
  i.e. a **zero-thickness gap**.
- **742 (93 %)** have a pair of near-**PARALLEL** normals → coincident duplicate faces.

Typical angle set between the 4 faces: `0, 3, 178, 178, 179, 179` — two sheets, each
contributing two faces, flush against each other. Consistent with two adjacent buildings whose
walls are co-located, which the penetration measurement supports (median penetration **0.000 m**,
p99 0.018 m; near-miss gaps p50 **0.022 m**, 176/199 pairs < 5 cm = ε).

### 9.8 Running tally of hypotheses

| # | hypothesis | test | outcome |
|---|---|---|---|
| 1 | nudge coincident verts apart | 1 mm inward | REFUTED — selfX 0→51 / 0→156; a pinch is a knife edge with no thickness to push into |
| 2 | coincident party walls between pieces | 3 cm inset | INVALID — broken offset, see §9.6 |
| 3 | `seal_piece` fillHole base caps | face normals | REFUTED — 74.7 % of bad-edge faces are vertical, not caps |
| 4 | party walls INSIDE one `_BATCHID` | isolate pieces | REFUTED — 0–2 alone vs 12–15 in domain |
| 5 | deep interpenetration | measure depth | REFUTED — median penetration 0.000 m |
| 6 | ε swallows the natural ~2 cm gap | pending | supported by §9.7 but not yet cleanly tested |
| 7 | dilation fuses the flush walls | 5 cm dilate | INVALID — broken offset, see §9.6 |

**NOTE ON PHYSICS (user, and it reframes the target):** CFD cells are ~3 m and the real domain
is ~2 km. A 5 cm gap is sub-grid — no airflow passes it and the solver discards it. Two
buildings 2 cm apart **are one obstacle** at that resolution, so *fusing* them is the physically
correct simplification, not a workaround.

**FORMAT NOTE:** OBJ is indexed and preserves the split vertices (verified: 8 verts survive an
OBJ round-trip, 12 after STL). STL cannot, and **no converter can recover it** — STL is where
the information is destroyed, so "export OBJ then convert" is not a fix. MOAB has an OBJ reader
added for DAGMC workflows, so OBJ is a viable fallback. But preserving the split only silences
the checker; the zero-thickness gap is still there for a ray tracer. Fusing removes it outright.

**COST WARNING:** ε = 0.01 was launched without estimating it and ran >46 min before being
killed. fTetWild cost goes roughly as 1/ε²–1/ε³, so 0.05→0.01 is ~25–125× a 240 s baseline.
Estimate before launching. ε = 0.05 on Duxton 400 m is 240 s / 654 MB.

---

## 9.9 CRITICAL: fTetWild is NON-DETERMINISTIC

Same input, same parameters, 8 runs each (`scripts/split_nudge.py` harness):

```
12 threads (default)   NM: [12, 4, 10, 8, 2, 8, 2, 8]   faces: 832 … 966
1 thread               NM: [ 6, 4, 12, 16, 0, 2, 0, 2]   faces: 884 … 984
```

NOT a threading artifact — single-threaded is just as variable. fTetWild uses
randomised perturbation internally (standard for robust geometry kernels).

**Every single-run A/B in §9.6 is therefore suspect.** "inset 3cm 798→792" and
"dilate 5cm 798→806" are within noise and mean nothing. A pair reported as NM=12
came back NM=0 on rerun, so the "minimal reproducer" and the 16-of-45 failing-pair
scan are both contaminated. Effects that likely survive (much larger than the
spread): true-dilate 5cm 798→422, inset 15cm 798→178, the synthetics that returned
0 across many configs, and the bisect linearity (6 consistent points).

**Rule: never compare fTetWild configs on one run.** Run N trials, report the
distribution. Where possible call `tetrahedralize()` ONCE and vary only the
`get_tet_mesh()` flags — that holds the randomness fixed and gives a controlled A/B.

## 9.10 Where the non-manifold edges actually come from — RESOLVED

The extraction was checked first and is NOT at fault: the tet-face occurrence
histogram is `{1: boundary, 2: interior}` with nothing at 3+, so the
tetrahedralization is a valid manifold tet complex and "boundary = faces used once"
is correct.

Controlled A/B (one `tetrahedralize()`, two `get_tet_mesh()` calls):

| flag | tets | verts | NM |
|---|---|---|---|
| `manifold_surface=True` | 2,677 | 1,090 | 26 |
| `manifold_surface=False` | 2,624 | 1,045 | 13 |

**`manifold_surface=True` DOUBLES the defect for our output path.** It achieves
manifoldness by SPLITTING the pinch vertex into one copy per lobe — correct, and
standard — but the copies sit at IDENTICAL coordinates. Any positional weld (which
STL forces) merges them straight back into a 4-face edge. So the flag converts N
genuine pinches into 2N welded ones.

The residual ~13 are genuine: where two buildings touch along a line, their union is
a solid that pinches to an edge, and the boundary of a pinched solid IS non-manifold.
No mesher can fix that without changing the geometry. fTetWild is robust against bad
INPUT; it cannot make a genuinely non-manifold SOLID manifold.

## 9.11 THE FIX: split + microscopic nudge — 0/0/0/0

Keep `manifold_surface=True` (it does the splitting we need), then move each copy of
a coincident vertex ~1 mm INTO ITS OWN LOBE along that copy's own inward normal. The
copies become distinct positions, so the weld cannot merge them.

10 real pieces, eps=0.10, 5 trials each (non-determinism explicitly sampled):

| config | open | NM | winding | selfX |
|---|---|---|---|---|
| `ms=True` | [0,0,0,0,0] | [24,26,24,26,14] | [48,52,48,52,28] | [0,0,0,0,0] |
| `ms=False` | [0,0,0,0,0] | [12,13,12,13,7] | [24,26,24,26,14] | [0,0,0,0,0] |
| **`ms=True` + 1 mm nudge** | **[0×5]** | **[0×5]** | **[0×5]** | **[0×5]** |

Note `ms=True` is exactly 2× `ms=False` — confirming every genuine pinch becomes two
coincident copies. 32 vertices moved.

**Why the 2026-08-21 nudge (§9.8 #1) failed and this one works:** that test was on
VOXEL/DMC output, where a pinch is a knife edge with no thickness to push into
(selfX 0→51). Here a lobe is a whole building — metres of solid. Same idea, opposite
outcome, because of what the lobes are.

Cost: pure post-processing on the extracted surface, no re-meshing. 1 mm is ~33×
float32 resolution at domain coords (spacing ~30 µm at 280 m) so it survives STL
export, and ~3000× smaller than a 3 m CFD cell.

## 9.12 Other settled numbers

- **eps = 0.10 strictly beats 0.05**: same 0/0/0/0, same 100 % coverage, 4.0 s vs
  6.3 s. Rule: **eps < half the minimum wall thickness** (30 cm walls → eps ≤ 0.15;
  it breaks between 0.10 and 0.25 because at eps=0.25 the two faces of a 30 cm wall
  are within 2·eps and may be collapsed).
- **Volume is far more robust than surface fidelity**: ±1 % even at eps=2.0 while
  coverage falls to 50 %. Blockage — what drives dispersion — survives coarsening.
- **Grouping is a SPEED fix only**, never implemented beyond stats. T=0.30 (rule
  **T > 2·eps**, because two pieces meshed separately can each drift eps toward the
  other): 32 groups, largest 49 buildings / 4,688 faces, 44 s on 10 cores vs 239 s
  whole-domain, memory bounded by the largest group, and the critical path (~43 s)
  does NOT grow with domain size. Contacts live inside groups, so it does nothing
  for the NM count.
- **Bisect**: NM is linear in piece count, ~2.4 per piece, flat across N=10→287.

## 9.13 Still open
Full-domain confirmation of split+nudge; terrain integration; Kent Ridge; and the
practicality question — 240 s for a 400 m domain is slower than Ansys FTM, which is
the standing argument against this whole direction.

---

# 10. fTetWild: ROOT CAUSE FOUND, APPROACH REJECTED ON COST

## 10.1 The single mechanism behind every non-manifold edge

> **eps closes any gap smaller than eps. A closed gap is a tangential pinch.
> A pinch has a non-manifold boundary.**

That is the whole thing. Whether the sub-eps gap is BETWEEN two buildings (measured
median 2.2 cm, p90 5.4 cm) or INSIDE one building (lightwell, gap between wings) is
irrelevant — same mechanism, one cause, not several.

**Proof** (`scripts/why_intrinsic.py`) — the 8 worst individually-defective pieces,
run alone, sweeping eps:

| piece | faces | eps=0.10 | eps=0.02 | eps=0.005 |
|---|---|---|---|---|
| 178 | 94 | NM=4 | 0 | 0 |
| 135 | 108 | NM=4 | 1 | 0 |
| 138 | 182 | NM=1 | 1 | 0 |
| 185 | 112 | NM=1 | 0 | 0 |
| 144 | 136 | NM=6 | 3 | 1 |

Shrink eps and they vanish. So it is NOT broken source geometry, NOT `seal_piece`'s
fillHole, NOT `_BATCHID` multi-structure pieces — it is eps collapsing real gaps.

This retroactively explains the whole investigation:
- isolated pieces are mostly clean → most buildings have no sub-10 cm internal gap
- 25 of 287 are not → those do (measured: **40 intrinsic NM total**, mean 0.14/piece,
  max 4, `scripts/intrinsic_nm.py`; an earlier claim of ~0.5/piece was wrong by 3.5×)
- pieces in contact create them → the 2.2 cm inter-building gap is below eps
- displacement repair is impossible → a pinch has **zero** local thickness
  (raycast: median 0.000 m, 1366/1633 split vertices below 1 mm), so ANY inward
  motion exits the solid, which is why selfX was magnitude-independent
- dilation was the only intervention that helped → it is the only one that converts
  an *ambiguous sub-eps gap* into an *unambiguous overlap*
- the two-box synthetic showed NM=0 at every gap → boxes have no sub-eps features

**Design rule (tool-independent):** eps must be BELOW the smallest gap you need to
preserve, OR every gap below eps must be deliberately closed by dilating > eps/2.
Those are the only two self-consistent configurations.

## 10.2 Domain-scale scoreboard — nothing reaches 0/0/0/0

Duxton 400 m, 287 pieces, eps=0.10, `manifold_surface=False`:

| approach | NM | selfX | time |
|---|---|---|---|
| soup + floodfill | 356 | 0 | 128 s |
| + split/nudge (any magnitude) | **0** | ~160 | 128 s |
| + raycast-capped nudge | 212 | 381 | 128 s |
| + true dilate 10 cm | 235 | 0 | 324 s |
| + true dilate 20 cm | OOM at 6 GB | — | — |
| native CSG union | worse at every dilation | 0 | ~same |

NM=0 **xor** selfX=0, never both.

**fTetWild native CSG** (`set_meshes` + `get_tet_mesh_from_csg`, no source build
needed) works but is strictly worse than soup+floodfill — 51 vs 18 NM undilated,
and worse at every dilation level. Branch closed.

## 10.3 Why eps=0.01 — the one configuration that WOULD work — is not viable

The mechanism predicts eps=0.01 gives 0/0/0/0 (it is below both the 2.2 cm
inter-building gap and the sub-10 cm internal gaps). The arithmetic kills it:

- measured: eps=0.05 → 235 s, eps=0.10 → 128 s (so t ~ 1/eps^0.88 locally)
- measured: eps=0.01 whole-domain was **killed at >46 min, unfinished** → against
  eps=0.05 that is >=11.7x for 5x finer eps → exponent **>=1.5**, i.e. cost
  accelerates as eps shrinks
- extrapolating eps=0.10 -> 0.01 at exponent 1.5: 128 s x 10^1.5 ≈ **67 min**
- largest group is 4,688 / 39,232 faces = 12 % → at t ~ faces^0.81 that is 18.5 % of
  domain time → **critical path ≈ 12 min**, the floor with unlimited cores
- grouping inflates TOTAL work 1.8x (measured: serial 428 s vs 235 s whole-domain),
  so 122 min serial / 12 cores ≈ 10 min — the critical path dominates

**400 m at eps=0.01, grouped, 12 cores ≈ 12–15 min. 2 km ≈ 4 hours.**
Ansys fault-tolerant meshing does the same job in **80 s**, and the existing
non-watertight raw export already works with it.

## 10.4 Verdict

REJECTED on cost, not on correctness. The quality problem is fully understood and
the eps that fixes it is known — it just costs ~10x more than the tool already in
use, before scaling to the real 2 km domain.

Carry forward: the mechanism in §10.1 is tool-independent. **Any envelope- or
tolerance-based mesher will fuse sub-tolerance gaps into tangential pinches.**
OneMap buildings sit 2.2 cm apart with sub-10 cm internal gaps, so any such tool
needs a tolerance under ~1 cm on this data. That is a property of the SOURCE
GEOMETRY, not of fTetWild, and it will apply to whatever is tried next.

## 10.5 Worth keeping regardless
- `max_its=0` — 7.9x faster, 4x fewer faces, zero fidelity cost (optimization only
  improves TETS, which are discarded; the envelope guarantee holds from insertion on)
- `eps=0.10` over 0.05 — 128 s vs 235 s at domain scale, identical quality
- `manifold_surface=False` — the True setting DOUBLES the defect for any
  positionally-welded output format, by splitting vertices into coincident copies
- fTetWild is NON-DETERMINISTIC (§9.9) — never compare configs on a single run
- measure local thickness BEFORE attempting any displacement-based repair

---

# 11. Restricted Power Diagram repair (SIGGRAPH '25) — REJECTED, and it unifies the picture

Paper: Wen et al., *Feature-Preserving Mesh Repair via Restricted Power Diagram*,
SIGGRAPH Conference Papers '25 (`papers/3721238.3730671.pdf`). Judged on the paper
only — **no code is published**, nothing was run.

## 11.1 What it does

1. Alpha-wrap the defective mesh (Portaneri 2022, CGAL) into a manifold proxy S.
2. Blue-noise sample N points off S, project them onto the original surface M.
3. QEM-shift each sample (Eq. 1): samples far from feature lines stay put, samples
   near feature lines converge onto them.
4. Build a Restricted **Power** Diagram on S, weight = squared shift distance.
5. The RPD's dual regular triangulation IS the repaired mesh.

Table 2 reports 0/0/0/0 (open boundary / non-manifold vertex / non-manifold edge /
self-intersection). Unlike fTetWild this is a **by-construction** guarantee, not a
repair pass — which is why it was worth reading.

## 11.2 Why it fails on this data: the constraint is sampling density

Their own Limitation 2: the RPD dual "may fail when the number of sampled points is
too small … especially when two feature lines are in close proximity." Two feature
lines in close proximity is the entire dataset (§10.1).

Measured surface area, Duxton 400 m domain (287 sealed pieces): **426,408 m²** of
building surface + ~160,000 m² terrain.

| sample spacing | why | N samples |
|---|---|---|
| 0.15 m | half a 30 cm wall — floor for walls to survive at all | 26,062,562 |
| 0.10 m | comfortable on walls | 58,640,765 |
| 0.011 m | half the 2.2 cm inter-building gap — what the RPD actually needs | 4,846,344,182 |

Their largest reported run is **18,000 samples ≈ 60 s** (Fig 13), on models
normalized into a unit box. Limitation 1 is "inefficiencies, particularly when the
number of sample points increases" — extraction already grows super-linearly across
their own 3K→18K sweep. The coarsest useful row is 1,450x their largest run
(≈24 h if linear, which it is not); the row that would actually work is 270,000x.

Two further blockers:
- **No code, and the missing piece is the research contribution.** Eigen/Libigl/
  Ceres are fine; the RPD follows Xiao et al. 2023 with no reference implementation.
- **Limitation 3 names our input**: "limited in its ability to handle meshes with
  large holes or near-zero volumes." OneMap pieces are open-bottomed LiDAR shells
  with large occluded gaps. Their 2K-mesh validation was Thingi10K/ABC — CAD parts,
  far cleaner than this.

## 11.3 The mechanism is the SAME as §10.1, in different notation

fTetWild collapses sub-ε gaps into tangential pinches. The RPD does not collapse
anything — it just needs samples finer than the smallest gap. Different mechanism,
identical binding constraint: **the required resolution is set by the smallest gap in
the source data, and OneMap's smallest gaps are ~2 cm.** Third independent
confirmation of §10.4's tool-independence claim. Not an escape from it.

## 11.4 Alpha wrapping ≡ what we are already doing (the useful finding)

The paper's step-1 proxy is Alpha Wrapping: carve a Delaunay triangulation inward
from infinity with a probe ball of radius α, over a surface offset ε outside the
input. Watertight + manifold + intersection-free by construction, within ε of the
input, indifferent to soup/self-intersection/open boundaries. Consequences fall out
of the mechanism: any gap the α-ball cannot enter is **sealed over**, the surface
sits ε **outside** the truth, and interior geometry is discarded.

That is the same operation as a wrapper-style CFD mesher, and as our own pipeline:

| tool | sealing mechanism | scale param | status |
|---|---|---|---|
| Blender voxel remesh | OpenVDB level set | VS = 2.0 m | **in production** |
| meshlib `doubleOffsetVdb` | VDB dilate-then-erode | ±1.5 m | measured: 1.3–6.4% vol error, corners rounded |
| CGAL alpha wrap | Delaunay carving | α, ε | this paper's proxy |
| Ansys FTM wrap | octree shrink-wrap | cell size | **already works on the raw export, 80 s** |

**The production pipeline has been running an alpha wrap all along.** That is exactly
why VS=2.0 eats the 30 cm walls — it is the α-ball problem stated in voxels (§9.2's
Nyquist aliasing is the same thing again).

Table 2 does independently confirm Alpha Wrapping ALONE scores 0/0/0/0 on all four
topology metrics, losing only sharp features (worst ECD in the table, 3.298). That is
the trade already characterized and already rejected.

## 11.5 Verdict

REJECTED, on two independent grounds:
- **Cost** — same surface-area/α² (or /spacing²) wall as §10.3, billions of cells.
- **Value** — even free, it buys nothing. A watertight-by-construction wrap at a
  usable scale is a capability we already have three implementations of, one of them
  shipping. Building a fourth yields a slower open-source FTM, and FTM already runs
  on the raw non-watertight export in 80 s.

---

# 12. Per-building fTetWild CACHE + OpenMC/DAGMC — the audit was measuring the wrong thing

Session 2026-08-21. Everything here is measured; scripts in `scripts/`.

## 12.1 The idea

§10 rejected fTetWild on PER-CUTOUT cost. A cache pays that cost ONCE. And the
split matters: at domain scale Duxton soup gave **356** non-manifold edges while
**intrinsic** (each piece alone) was only **40** across 25 of 287 pieces — so
**~89% of the defect comes from buildings IN CONTACT, not from inside buildings.**
Per-building processing means no tolerance operation ever spans two buildings, so
the 2.2 cm inter-building gap is never seen and never becomes a pinch.

## 12.2 Cost + storage (measured, not extrapolated from one domain)

Face distribution over **1,250 sealed pieces / 6 diverse domains** (Duxton, CBD,
Kent Ridge, Queenstown, Jurong, Bishan): p50=100, mean=314, p99=2,710, max=11,936.

Stratified timing, then POPULATION-WEIGHTED (the raw stratified number oversamples
big buildings and reads pessimistically at 62.2%):

| face bin | pop share | pass @ eps=0.15 | mean s |
|---|---|---|---|
| 12–82 | 39.4% | 100% | 0.51 |
| 82–128 | 20.5% | 93% | 0.74 |
| 128–242 | 15.0% | 86% | 0.94 |
| 242–550 | 10.0% | 71% | 2.17 |
| 550–1,104 | 7.0% | 79% | 5.33 |
| 1,104–1,538 | 4.0% | 21% | 8.08 |
| 1,538–2,710 | 3.0% | 21% | 12.36 |
| 2,710–11,936 | 1.0% | 25% | 17.55 |

**85.7% pass, 1.95 s/building.** 146,645 buildings -> **80 CPU-h, ~8 h on 10 cores**.
Retry queue ~21,000 (+20 CPU-h at 0.05, +46 at 0.015). Total **~12–13 h on 10 cores**.

**Storage: 3.32 KB/building compressed npz -> ~0.4 GB islandwide.** An earlier
"2.2 GB" quote was UNCOMPRESSED bytes. NOTE: `df` reports the WSL .vhdx lie —
real free space came from `powershell.exe Get-PSDrive C` and was **8.7 GB**.

## 12.3 fTetWild NON-DETERMINISM is a first-order effect, not a footnote

Duxton piece 216 returned EMPTY once, then **804 / 817 / 788 tets on three retries
at the SAME eps**. It is a healthy building: 20 verts, 36 faces, 612 m^3,
watertight, euler 2, single body, zero degenerate faces.

Consequences:
- A ladder that only ever retries with a SMALLER eps cannot fix a coin flip, and
  pushes flakes into expensive territory. `ATTEMPTS_PER_EPS = 3` added.
- **The 85.7% pass rate is a LOWER BOUND** — single-attempt-per-eps counts flakes
  as real failures.
- Caching is the right SHAPE for a non-deterministic tool: verify-then-store makes
  the cached solid clean by construction.

## 12.4 THE AUDIT WAS THE WRONG AUDIT (the important part)

DAGMC's own definition: *"A model is considered watertight if the faceting of all
topologically linked surfaces are coincident."* That is a **CELL COMPLEX** —
volumes that SHARE surfaces — not one fused manifold solid. `imprint`+`merge`
exists specifically to force touching volumes to share coincident surfaces.

| geometry | our 0/0/0/0 soup audit | DAGMC |
|---|---|---|
| 2.2 cm gap between buildings | fine | **fine** — that is air, a legitimate volume |
| shared party wall | non-manifold edge = DEFECT | **REQUIRED** |
| volumetric overlap | self-intersection = defect | **FATAL** (lost particles) |

A surface shared by two adjacent volumes MUST read as a non-manifold edge once
flattened into one soup. **Months of "0/0/0/0 or throw it away" was the fused-CFD-
solid requirement applied to OpenMC, which does not want a fused solid.**

`make_watertight` is INAPPLICABLE to us — it needs CAD curve topology
(GEOM_DIMENSION=1); on faceted volumes it dies with "file set not found".

Toolchain (persistent, in `~/tools/mamba/root/envs/`): `dagmc` (DAGMC 3.2.4 +
MOAB 5.6.0, CLI `overlap_check`/`make_watertight`/`check_watertight`/`mbconvert`)
and `moabpy` (pymoab 5.5.1 — needs **setuptools<81**, v81 removed `pkg_resources`).
`/usr/local/bin/openmc` 0.15.3 reports **DAGMC support: yes**.

Source coupling for later: `openmc.stats.MeshSpatial(mesh, strengths=...)` takes
per-element source strengths — the direct bridge from Fluent per-cell
concentrations. Source mesh and geometry are independent.

## 12.5 Gates on the cache (Duxton, 287 pieces)

- **Cache quality: 285/287 individually 0/0/0/0**, 30 cm walls intact, 31 s total.
- **GATE 2 (slice for an exact CDT terrain seam): PASSES 99.7%** (285/286), mean
  1.21 rings/building, 52 with courtyards. NOTE: a first run read 0/286 — a TEST
  BUG, `slice_plane` output is unwelded so shared edges read as boundary. Weld first.
- **GATE 1 (just concatenate them): FAILS.** 180 of 684 bbox-overlapping pairs
  genuinely intersect; median 22 vertices of one building STRICTLY INSIDE the
  other; only 3/80 are pure touching. Duxton shophouses really interpenetrate.

## 12.6 Overlap resolution by DIFFERENCE — REJECTED by DAGMC's own checker

`B := B - A` (imprint-and-merge expressed as CSG). Our metric said it worked:
overlap volume 17.8 -> 0.2 m^3, **98.8% removed**, 91/100 pairs under 0.01 m^3.

DAGMC disagreed completely:

| model | overlap locations | volumes involved |
|---|---|---|
| per-building (286 vols) | 149 | 196 |
| difference-resolved | **176** | **225** |

**0 of 149 flagged pairs fixed; 27 NEW ones created.** Mechanism: `overlap_check`
(default) tests whether a VERTEX of one volume lies inside another. After the
difference, B's new boundary IS the contact patch, whose vertices lie exactly ON
A's surface, and point-in-volume at exact coincidence is ambiguous. Removing shared
VOLUME does not remove shared SURFACE — it manufactures more of it.

Real blocker: our h5m gives every volume a PRIVATE surface with sense `[vol, 0]`.
DAGMC needs ONE surface entity referenced by two volumes with `GEOM_SENSE_2 =
[forward, reverse]`. Two coincident-but-distinct copies of a wall is exactly what
imprint/merge exists to eliminate.

Also: do NOT use `vertices_to_h5m` at scale — it passes the FULL shared vertex
array to `create_vertices()` once per volume, so cost/size are O(volumes x total
verts). Measured 346 KB @5 vols -> 13 MB @40 -> **622 MB @286**, for ~97k triangles.
Per-volume vertices makes it linear: **6.5 MB**. See `cache_to_dagmc.py`.

## 12.7 Overlap resolution by GROUP-MERGE — the promising one

All buildings get ONE material, so touching buildings have no reason to be separate
volumes. Merge VOLUMES (no shared-surface bookkeeping) by feeding a whole
overlap-connected group to fTetWild in ONE call.

Grouping: 286 buildings, 180 overlapping pairs -> **112 connected components**
(58 singletons, 21 pairs, 13 triples, 20 of 4+, **largest 18**).

| model | volumes | overlap locations | volumes involved |
|---|---|---|---|
| per-building | 286 | 149 | 196 |
| difference | 286 | 176 | 225 |
| **group-merged** | **91** | **9** | **17** |

**CAVEAT, and it is a big one:** only **91/112 groups meshed**. The 21 failures are
the BIG groups (sizes 18,17,15,12,11,8,6,6,5,5,5,5,4,4,3,3,3,2,2,2,2) holding
**138 buildings = 48.3% of the domain**. So "149 -> 9" is measured on a model
missing half the domain, specifically the dense half. NOT like-for-like.

## 12.8 WHY the groups fail — measured, 167 trials, 21 groups x 3 eps x 3 trials

    failure-mode totals:  DEFECT 159,  OK 8
    ZERO empty. ZERO timeout. ZERO exceptions.

fTetWild ALWAYS returns a mesh. Every defect is `(open=0, NM=N, selfX=0)` — **purely
non-manifold edges**, 1–37 of them (only 2/159 trials had any self-intersection).

**Non-manifold count FALLS as eps shrinks** (grp 9: 16–22 @0.15 -> 4–12 @0.015;
grp 87: 11–17 -> 2–3). That is §10.1 again, now INSIDE merge groups: eps closes
sub-eps gaps into tangential pinches; smaller eps closes fewer.

Span does NOT predict failure — grp 85 (3 bldgs / 23 m) never passes; grp 105
(2 bldgs / 27 m) passes first try. It is whether the group contains sub-eps gaps
anywhere, not chain length. (An earlier "transitive closure spans air gaps"
hypothesis was only half right.)

**8 of 21 groups hit OK on a RETRY** -> with `ATTEMPTS_PER_EPS=3` the merge goes
**91/112 -> 99/112 (88%)**, covering 174/286 buildings. 13 groups (112 buildings)
still fail, trending toward 0 NM as eps drops. Low-eps test (0.005 / 0.002) is the
open item.

## 12.9 Standing lesson: check what a number MEASURES before reporting it

Four times in one session a result looked good until the metric was examined:
- "difference removed 98.8% of overlap volume" -> DAGMC: fixed 0 of 149.
- "group-merge: 94% fewer overlaps" -> on a model missing 48% of the domain.
- "85.7% pass rate" -> conflated non-determinism flakes with real failures.
- "0/286 slices failed" -> unwelded `slice_plane` output, a test bug.
Each time the geometry was fine and the MEASUREMENT answered a different question
than the consumer asks. Verify the metric against the consumer, then report.

---

# 13. REPAIR post-fTetWild — the per-building problem is SOLVED (2026-08-27)

Session 2026-08-27. Scripts: `scripts/repair_ftw.py`, `scripts/union_manifold3d.py`.

User question that opened this: *"we never ever tried to repairing the post-fTetWild
meshes did we?"* Correct — we never did. Every prior section either shrank eps,
displaced geometry, or changed the fusion strategy. Generic mesh repair applied to
fTetWild's own output was never tested, in 12 sections of investigation.

**It works. 287/287 at 0/0/0/0.**

## 13.1 The measured ladder (Duxton 400 m, 287 pieces, eps=0.10, 3 trials each)

| outcome | count | share |
|---|---|---|
| clean on ALL 3 trials | 216 | 75.3% |
| clean on SOME trials — **a retry fixes it** | **65** | **22.6%** |
| clean on NO trial (persistent) | 6 | 2.1% |
| **after pymeshfix on those 6** | **6/6 CLEAN** | **287/287 = 100%** |

The 22.6% flake rate is Sec 9.9 non-determinism at full strength and it confirms
Sec 12.3's argument directly: **the 85.7% single-attempt pass rate was a pessimistic
lower bound**, and a cache MUST verify-then-store.

## 13.2 Which tool, and why the others fail

| tool | fixed | mechanism |
|---|---|---|
| **`pymeshfix.repair()`** | **6/6** | CUTS: `intersection_removal` deletes offending triangles, `fill_holes` re-patches |
| `repair(joincomp=True)` | 6/6 | byte-identical output here |
| pymeshfix stages by hand | 5/6 | left selfX=4 on p281 — **use the full `repair()`**, not the stages |
| meshlib `resolveMeshDegenerations` | 0/6 | driven by `tinyEdgeLength`; our bad edges are 2.77 m median (Sec 9.4). PREDICTED and confirmed |
| trimesh `fix_normals`+`fill_holes` | 0/6 | neither addresses a non-manifold edge |
| manifold3d `Mesh.merge()` | 0/6 | **correct behaviour, not failure** — see 13.4 |

**Why the standing hypothesis was WRONG.** Sec 9.8 #1 / Sec 10.1 argued displacement
repair is impossible because a pinch has zero local thickness, so any inward motion
exits the solid. True — but **pymeshfix does not displace, it cuts**. Remove-and-repatch
has no thickness requirement. The argument was also imported across scales: it was
measured on VOXEL/DMC output and whole-domain soup, never on per-building fTetWild.

## 13.3 The volume scare was OUR METRIC (6th instance of Sec 12.9)

First reading was "pymeshfix inflates volume +4400% median". False. A non-manifold
edge makes SIGNED volume partially cancel, so the *before* number is garbage and any
correct repair looks like massive inflation. Against the true sealed source:

| piece | source vol | our "before" | pymeshfix | ratio | surface deviation vs source |
|---|---|---|---|---|---|
| 144 | 2,050.0 m^3 | 5.6 | 2,072.7 | 1.01x | mean 0.06 cm, p99 0.88, max 5.40 |
| 185 | 2,009.9 | 76.0 | 2,017.2 | 1.00x | mean 0.10 cm, p99 3.25, max 3.52 |
| 281 | 62,296.1 | 10,470.2 | 62,158.3 | 1.00x | mean 0.10 cm, p99 4.04, max 5.51 |
| 2 | 3,289.4 | 30.6 | 3,289.0 | 1.00x | — |

Bounding boxes identical to the decimal. **Max deviation 5.5 cm ~= eps** — pymeshfix
stays inside the envelope fTetWild already guarantees, so it is not fabricating.
Cost is face count: 136 -> 470, 2,358 -> 4,258 (roughly 2-3x, it retriangulates).

**Rule: never measure volume on a mesh with non-manifold edges.** Validate against the
SEALED SOURCE, and prefer surface deviation (`trimesh.proximity.closest_point`) over
volume — it cannot be fooled by winding.

## 13.4 manifold3d is the ASSEMBLY half, and it composes

`manifold/README.md` (Emmett Lalish; trimesh, Blender, OpenSCAD, Godot all use it):

> *"you'll get an error status if the imported mesh isn't manifold... in general you
> may need one of the automated repair tools that exist mostly for 3D printing"*
> *"a guaranteed-manifold mesh Boolean algorithm, which I believe is the first of its kind"*

It is a GUARANTEE library, not a repair library — its refusal of our raw output is the
contract working. And its headline claim is exactly the assembly blocker of Sec 12.6-12.8.

```
raw fTetWild            -> Error.NotManifold   (6/6 refused)
+ pymeshfix             -> Error.NoError       (6/6 accepted, volume matches source)
union of 2 overlapping  -> NoError, trimesh watertight=True, bodies=1, selfX=0
batch_boolean of 6      -> NoError, genus 7, 6,108 tris
```

So the two halves line up, each with a guarantee rather than a heuristic:

1. **Per-building** — fTetWild (exact geometry, envelope-bounded) + up to 3 retries +
   `pymeshfix.repair()` on the residual -> guaranteed 0/0/0/0, cacheable, verify-then-store.
2. **Assembly** — manifold3d boolean union -> guaranteed manifold output, no
   imprint/merge bookkeeping, no shared-surface sense bookkeeping.

This is the first candidate that does not hit the `NM=0 xor selfX=0` wall of Sec 10.2,
because neither stage is a tolerance operation spanning two buildings.

**Gotcha:** piece 185 came back through manifold3d with volume **-2017** (inverted
orientation). Our tet-boundary extraction does not order faces outward; flip on
`trimesh.volume < 0` before ingest.

## 13.5 Cost, reweighted for free overnight CPU

The user's constraint changed: overnight CPU is free, so Sec 10.4's per-cutout
rejection does not apply to a one-time cache. With 75% first-pass / 23% retry /
2% repair and ~0.5-2 s per fTetWild call, the Sec 12.2 islandwide estimate
(~80 CPU-h, ~12-13 h on 10 cores, 0.4 GB compressed) stands and now lands at 100%
rather than 85.7%.

## 13.6 What this does NOT establish

- **Duxton only.** Dense shophouses — the domain where proximity grouping already
  worked (Sec 0's 8.2 pieces/group vs Kent Ridge's 1.5). Kent Ridge and Queenstown
  are untested for the repair ladder.
- The union evidence in 13.4 is 2 artificially-overlapped buildings plus a 6-way
  batch, **not** a real domain. Full-domain result: see 13.7.
- **Terrain is not integrated.** Sec 9.13 / 9.5 still open.
- eps=0.10 only; the ladder's lower rungs (0.05, 0.015) were not needed here and
  were not exercised.

## 13.7 Standing lessons added this session

- **Never A/B fTetWild on one run** (Sec 9.9, re-confirmed hard): piece 185 read
  NM=8 on one run and fully clean on the next. The first version of `repair_ftw.py`
  reported "33 defective/287" from single runs; with 3 trials the persistent set is 6.
- **Bowtie (non-manifold VERTEX) is a separate check** and was missing from the first
  audit. Euler=1 on a closed mesh with zero non-manifold EDGES is impossible for an
  orientable manifold — that discrepancy is what exposed it. 3 of the 6 persistent
  failures are bowtie-only (nm=0, bow=1).
- **`pymeshfix` 0.18.1 API**: attributes are `.points`/`.faces` (NOT `.v`/`.f`), and
  `repair()` signature is `(joincomp, remove_smallest_components)` with **no `verbose`
  kwarg**. Note `onemap-slicer/src/onemap_slicer/mesh_processor.py:170` calls
  `repair(verbose=False, ...)` inside a bare `except Exception: pass` — so pymeshfix
  **never runs at all** there; it silently falls through to the trimesh path. A useful
  reminder that the reference implementation's "watertight" output is unverified.

## 13.8 FULL-DOMAIN union — per-building 287/287, but CSG is the WRONG TOOL

`scripts/union_manifold3d.py`, Duxton 400 m, eps=0.10, 3 trials + pymeshfix.

### Per-building stage: 287/287 accepted by manifold3d

| route | count |
|---|---|
| clean first try | 251 |
| clean on retry 1 | 20 |
| clean on retry 2 | 1 |
| needed `pymeshfix` | 14 |
| still defective by OUR audit but manifold3d took it | 1 |
| **manifold3d ingest: accepted / refused** | **287 / 0** |

The ladder + repair genuinely delivers 100% manifold-3d-ingestible buildings.

### TWO bugs of mine, found on the way, both worth keeping

**(a) `is_clean()` never checked WINDING, and our tet-boundary extraction never
oriented faces.** Collecting tet faces used exactly once gives the right SET of
boundary faces but ARBITRARY per-face winding — measured **305-578 conflicts per
piece**. A single global flip does not fix per-face inconsistency. Consequences:
signed volume partially cancelled (135.3 vs a true 2412.0, i.e. the "raw fTetWild
volume" figures in 13.3 were garbage for this reason too), and manifold3d refused
**281/287** as NotManifold while our own audit called them clean.
Fix: `trimesh.repair.fix_winding` + `fix_normals` inside `ftw()`; winding 305-578
-> 0 on 7 of 8 sampled pieces, volume then matches source to 0.03%, manifold3d
accepts 8/8. **Added `m3d_ok()` as a separate gate** — the real consumer's opinion,
not ours.

**(b) float32 at absolute SVY21 coordinates.** `manifold3d.Mesh` takes float32
`vert_properties`; at ~29,000 m the spacing is **2.0e-3 m**, so `merge()` welds
genuinely distinct vertices and manufactures non-manifoldness. Union in a
domain-local frame and restore the offset at export. Same trap as meshlib's 1 mm
quantisation (Sec 9.11) — this is now the THIRD time float32-at-SVY21-coords has
bitten this project. **Any coincident-surface work must be done in a local frame,
and any STL we ship at absolute coordinates is silently 2 mm quantised.**

### The union itself, and the finding that matters

```
batch_boolean of 287 solids   0.0s   status=NoError genus=104 tris=187,942
  vol 825,777.4 m3   sum of parts 825,796.0   overlap removed  18.6 m3
csg check: NO interpenetrating pair among 1,770 tested
audit LOCAL frame: open=0 nm=793 bowtie=108 wind=1,586 selfX=6,712 comps=291
```

291 components out of 287 inputs, and 0.0s. **The union did essentially nothing,
because there is essentially nothing to union.**

### CORRECTION to Sec 12.5 GATE 1

Measured on the SEALED SOURCE (ground truth, no fTetWild), 688 bbox-overlapping pairs:

| | count |
|---|---|
| DEEP interpenetration (> 1 m^3) | **5** |
| any interpenetration (> 1e-6 m^3) | 85 (median 0.0000, p90 0.003 m^3) |
| **surfaces TOUCHING (within 5 cm)** | **266** |

overlap volume median **0.0000 m^3**, surface gap p10 **0.0000 m**.

Sec 12.5 concluded *"Duxton shophouses really interpenetrate — median 22 vertices of
one building STRICTLY INSIDE the other."* That is almost certainly an artifact of
**point-in-volume being ambiguous at exact coincidence** — the identical mechanism
Sec 12.6 already identified in DAGMC's `overlap_check` ("point-in-volume at exact
coincidence is ambiguous"). Vertices lying exactly ON a shared party wall test as
"inside". The buildings are FLUSH, not interpenetrating.

### Why this reframes the assembly problem

**CSG union cannot merge two flush surfaces.** There is no overlapping volume to
remove; a union of two touching solids legitimately returns both sheets, and the
coincident coplanar triangles then read as self-intersections (6,712) and the shared
walls as non-manifold edges (793) in any fused-soup audit. manifold3d is not failing
— `status=NoError` — it is correctly reporting that a 291-component solid is manifold.
Our audit and manifold3d are answering different questions, again (Sec 12.9).

This is Sec 9.7 seen from the other side ("97% opposed normals, median penetration
0.000 m") and Sec 12.4 seen from the CFD side (a shared party wall MUST read as
non-manifold once flattened; DAGMC *wants* that, a fused CFD solid does not).

### The implied fix, untested

Sec 10.1's design rule already names it: *"every gap below eps must be deliberately
closed by dilating > eps/2."* For a FLUSH contact the gap is 0, so a dilation of a
few cm converts coincidence into genuine volumetric overlap, which CSG **can** merge.
Sec 10.2 tried this at the fTetWild level (true dilate 10 cm: NM 798 -> 235, selfX 0)
and it helped but did not finish the job. It has NOT been tried with manifold3d, whose
union is intersection-free BY GUARANTEE rather than by luck.

Physics supports it (user, Sec 9.8): at ~3 m CFD cells two buildings 2 cm apart ARE
one obstacle, so fusing them is the physically correct simplification. A 2-5 cm
dilation is ~1% of a cell.

**Next experiment:** dilate each cached building by ~2-5 cm (manifold3d
`minkowski_sum` with a small sphere, or a per-vertex normal offset), then
`batch_boolean`. Success criterion is component count collapsing from 291 toward the
number of physically distinct structures, with selfX staying 0.

---

# 14. VERDICT (2026-08-27) — mesh work FROZEN, and why that is the right call

## 14.1 The CFD path was never blocked

This is the fact that reframes Sec 9-13. Both consumers actually in use accept the
existing output, proven by real runs, not docs:

| consumer | mechanism | our raw mesh (11,334 selfX) |
|---|---|---|
| snappyHexMesh (OpenFOAM 12) | ray-casts, never retriangulates | **ACCEPTED** — 1,030,259 cells, "Finished meshing without any errors" |
| Ansys fault-tolerant / wrap | tolerates non-manifold input | **ACCEPTED** — already the production path |
| Ansys **watertight** workflow | surface-REMESHES | REJECTED — "failed to remesh 148 areas" |

The whole 0/0/0/0 chase was for the third row: one option of three, and not the one
being used. Not wasted work — it produced Sec 10.1 and Sec 12.4 — but optional.

## 14.2 What was actually load-bearing: `seal_piece`

Un-welded double-sided OneMap soup -> no coherent SDF sign -> ANY isosurface
extraction at level 0 shatters (Blender 130 bodies / meshlib 207 — identical
failure, so Blender was never uniquely bad). That was a REAL bug destroying real
buildings in the deliverable, and the fix is why voxel remesh now returns volume
within 0.4-1.1% of ground truth as a single body.

**Everything after `seal_piece` — fTetWild, manifold3d, dilation — is refinement
past the point where it changes a dose number.**

## 14.3 Why the dose deliverable does not need any of it

- Deposition is a NEAR-GROUND phenomenon: 3.46% of plume residence sits within 2 m
  of grade, and the entire groundshine map comes from there.
- Roof detail is geometrically far from every receptor, shielded by the building
  beneath it, and sub-grid at ~3 m CFD cells. Roof fidelity is close to irrelevant
  to the headline number.
- The current DAGMC model (one fused solid + air) validated against an ANALYTIC
  solution to **0.6%**, with leakage **0.00000** inside the slab. Per-building
  volumes would only buy per-building materials or per-building deposition, neither
  of which is in scope.

## 14.4 fTetWild: parked honestly, not forced in

It has no role in this deliverable. The tempting argument — "DAGMC wants a cell
complex (Sec 12.4), so the 287/287 per-building cache is its natural input" — is
true in principle and false in practice here, for the reason in 14.3.

What it DID produce is more portable than a pipeline stage: **Sec 10.1 is a
tool-independent design rule**, derived from measurement, that transfers to any
mesher —

> Any envelope- or tolerance-based mesher fuses sub-tolerance gaps into tangential
> pinches, and a pinched solid has a non-manifold boundary. The tolerance must be
> BELOW the smallest gap you need to preserve, OR every gap below it must be
> deliberately closed by dilating > eps/2.

OneMap buildings sit 2.2 cm apart (median) with sub-10 cm internal gaps, so any such
tool needs a tolerance under ~1 cm on this data. That is a property of the SOURCE
GEOMETRY, not of fTetWild.

## 14.5 State of the parked work, and the exit path if it reopens

Everything is measured and reproducible; nothing is half-applied to production.

| piece | state |
|---|---|
| `seal_piece` | **IN PRODUCTION** (`extract.py`), load-bearing, keep |
| per-building fTetWild + retry + pymeshfix | 287/287 at 0/0/0/0, `scripts/repair_ftw.py`, NOT wired in |
| manifold3d union | works, `scripts/union_manifold3d.py`, NOT wired in |
| dilate -> union | comps 298->120 at 2 cm, silhouette +0.84%, `scripts/dilate_union.py`, NOT wired in |
| residual defect | 26,793 positional-weld NM, 75% of which is `minkowski_sum` faceting, not fusion |

**If it reopens** (PI asks for the Ansys watertight workflow, or a bigger domain
forces a rebuild), the next step is fully mapped in Sec 13.8: morphological CLOSING
— dilate -> union -> `minkowski_difference` erode back — which fuses permanently
while returning geometry to true size. Alternatives ranked there.

## 14.6 The transferable lessons, for the writeup

- **Sec 12.4** — the same geometry is simultaneously correct for DAGMC and defective
  for a CFD remesher, because one wants a cell complex and the other wants a fused
  solid. Most pipelines never articulate this.
- **Measurement discipline (Sec 12.9), now EIGHT instances.** Every one looked like a
  result until the metric was examined:
  1. "difference removed 98.8% of overlap volume" -> DAGMC fixed 0 of 149
  2. "group-merge: 94% fewer overlaps" -> on a model missing 48% of the domain
  3. "85.7% pass rate" -> conflated non-determinism flakes with real failures
  4. "0/286 slices failed" -> unwelded `slice_plane`, a test bug
  5. "33 pieces defective" -> 6; the rest was single-run non-determinism
  6. "pymeshfix inflates volume +4400%" -> signed volume cancelling on a
     non-manifold mesh; true ratio 1.00-1.01x vs the sealed source
  7. "281/287 refused by manifold3d" -> OUR extraction never oriented faces
     (305-578 winding conflicts/piece) and `is_clean()` never checked winding
  8. "dilation loses 24% of frontal area" -> `0.5*sum(a|n|)` double-counts internal
     shared walls that fusion removes; TRUE rasterised silhouette is **+0.84%**
- **float32 at absolute SVY21 coordinates has now bitten three times** (meshlib
  1 mm, manifold3d 2 mm ingest, STL export). Do coincident-surface work in a local
  frame; any STL shipped at absolute coords is silently ~2 mm quantised.

## 14.7 Where the remaining effort should go instead

Gaps that move the numbers being presented, unlike mesh topology:
- **near-surface sampling** — 554/1600 columns carry signal, median 122 steps,
  ~9% Poisson error on the deposition footprint. Fix is upstream (more DRW tries).
- **wet deposition** — often dominant in a real assessment, currently absent.
- **one wind direction / one release / one nuclide** — a sensitivity sweep is cheap.
- the deferred CFD buffer-domain and wind-direction features (`dose/NOTES.md`).
