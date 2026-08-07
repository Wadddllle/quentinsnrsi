# MD1 disintegration research (reference only — nothing here is wired into production)

Investigation into why some tile-native buildings (NUS MD1 being the reproducible test case,
SVY21 ~22564.8,30648.98) disintegrate into a floating skeleton after `fuse_stl.py`'s Blender
voxel remesh, while visually similar buildings survive. Full narrative, every dead end and every
finding, is in the project plan doc (`we-are-in-deep-imperative-petal.md`, section "MD1
disintegration root-cause investigation + fix research") — this directory is just the scripts
and renders that back it up, so the numbers can be re-run/re-checked later instead of trusted
from memory.

**Root cause** (confirmed, not guessed): MD1 has a real elevated bridge touching the ground at
one degenerate point. `extract.py`'s footprint clustering correctly discards that as noise, so
the bridge gets zero skirt/ground-backing anywhere along its span. Blender's OpenVDB voxel remesh
can't resolve a genuinely thin, unsupported structural member with nothing nearby to help the
sign classification — it erodes. Confirmed by reproducing the exact disintegration on MD1 in
total isolation, and confirming a "similar-looking" neighbour building erodes identically in
isolation too (the difference in the real pipeline is backing geometry, not mesh quality).

## Files, roughly in the order the investigation happened

- `00_tile9_0_batch0.obj` / `_dedup_oriented.obj`, `nb00_17_0_b4.obj` — the actual raw MD1 and
  neighbour-building meshes used throughout (extracted once, reused by every later script).
- `investigate_seams.py`, `investigate_skipped_prims.py` — ruled out two early wrong hypotheses
  (mixed-batchid seam faces, skipped non-Draco primitives). Both found zero occurrences.
- `find_roof_holes2.py` — the (corrected) boundary-loop check. First attempt failed to account
  for these meshes being double-sided soup; this version welds properly first. Confirms MD1 has
  zero real holes away from its base.
- `check_winding.py` — found the double-sided-mesh structure (every edge used 2x or 4x, never
  1x/3x) that motivated (and then, via `dedup_and_orient.py` + `blender_ab_test.py`, disproved)
  the winding-duplication hypothesis.
- `dedup_and_orient.py` — welds + deduplicates the double-sided mesh to one triangle per real
  face + fixes orientation via BFS across shared edges.
- `blender_ab_test.py` / `render_ab.py` — A/B test: raw vs. weld-only vs. dedup+oriented, voxel
  remeshed in isolation. All three gave byte-identical results — kills the winding hypothesis.
- `blender_solidify_confirm.py` — confirms Solidify (thickness 3m) recovers the correct shape.
- `synthetic_struts.py` / `blender_strut_test.py` / `blender_struts_solidify.py` — the
  per-cell-ground-strut idea: fixes vertical thin members, does nothing for the horizontal bridge
  deck on its own, needs combining with solidify.
- `dump_md1_area.py` / `render_md1_area.py` — pulls every real building piece near the target
  point and renders them, so the right building could be identified by eye. `piece00_oblique.png`
  is the ground-truth reference used for every later visual comparison.
- `pipeline_trace.py` / `fuse_debug.py` / `render_stages.py` — runs the REAL production pipeline
  (extract → conforming terrain/skirts → Blender fuse) for a domain covering both MD1 and the
  neighbour, snapshotting every stage, to see what backing geometry each building actually gets.
  This is what confirmed the "backing geometry, not mesh quality" explanation.
- `morph_closing.py` / `morph_closing_scene.py` / `morph_closing_o3d_full.py` / `morph_edt.py` /
  `morph_aniso.py` — the morphological-closing (dilate→fill→erode) research thread, first via
  trimesh's slow voxelizer, then via open3d's much faster one. Conceptually the "right" fix
  (self-limiting, unlike Solidify) but the voxelization step doesn't scale to production domains
  as implemented.
- `check_included_test.py` / `o3d_voxel_speed.py` / `o3d_extract_scaling.py` /
  `vectorized_voxelize.py` — open3d voxelizer speed investigation, including two dead-end
  alternatives (`check_if_included` bulk query — slower; hand-rolled point-sampling vectorization
  — 3x slower AND only ~40% correct).
- `poisson_test.py` / `poisson_trim.py` — Poisson surface reconstruction, tested properly with
  and without density trimming. Fails badly (wrong size, not watertight, real geometry missing).
- `vdb_test.py` / `vdb_test_scene.py` — real OpenVDB (via a scratch conda-forge env, not
  installed here) tested directly: `halfWidth` does NOT bridge gaps, refuting that hypothesis
  cleanly (identical results at halfWidth 3/5/8/12, both isolated and in the real closed scene).
- `meshlib_doubleoffset_test.py` — meshlib's own native `doubleOffsetVdb` (already an installed
  production dependency). Fast, correct, watertight — but confirmed algorithmically super-linear
  via real memory-vs-time profiling (see the plan doc for the `/usr/bin/time -v` numbers).
- `meshlib_boolean_test.py` / `meshlib_boolean_scene.py` — the user's original literal ask:
  solidify each building (meshlib `offsetMesh`) + real exact CSG `boolean()` union with terrain.
  Fastest result of the whole investigation (0.18-0.33s for 1-2 buildings).
- `meshlib_scale_single.py` / `meshlib_scaling_test.py` / `meshlib_boolean_scaling.py` /
  `meshlib_boolean_hierarchical.py` — scaling investigation for the boolean-union approach.
  Naive sequential-fold union is a real O(n^2) accumulation bug (23.5x time at 9x buildings);
  fixed with a binary-tree hierarchical union (12.4x at 9x buildings — real improvement, not a
  full fix, some residual super-linear cost in the CSG solver itself remains unexplained).

## renders/

Visual comparisons at every stage — `piece00_oblique.png`/`piece00_topdown.png` are the ground
truth (real full-detail MD1 mesh, no processing). `CMP_*.png` are same-camera-angle comparisons
of each candidate fix against ground truth. `O3D_*`/`render_*_stage3_voxel_remeshed.png` are the
real-pipeline-context renders (both buildings, with terrain/skirts).

## If this gets picked up again

See the plan doc's "Concrete next steps" for this section — the short version: the
meshlib-solidify+hierarchical-boolean-union approach is the strongest candidate, but its
residual per-operation super-linear cost (separate from the already-fixed accumulation-order bug)
needs isolating before trusting it at real multi-km² production domain scale. Nothing in this
directory is imported by any production code — it's all standalone scratch scripts kept for
reference, most with hardcoded `/tmp/...` scratchpad paths from the original session that will
need updating to actually re-run.
