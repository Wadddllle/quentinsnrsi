# SBG v2 pipeline — file-by-file, end to end

This documents the **current, production tile-native pipeline** (`sbg/onemap_native/`),
from scraping OneMap's 3D-Tiles `.b3dm` archive through to the final mesh/GeoTIFF result.
UI (`sbg/ui/`, `sbg/onemap_native/ui/`, `webui/`, `webui-v2/`) is deliberately excluded —
everything below is reachable purely through `python -m sbg.onemap_native.build`.

The older v1 CityJSON stack (`sbg/build_sbg.py`, `sbg/extrude.py`, `sbg/topo/conforming_mesh.py`,
`sbg/blender/`) and everything under `sbg/onemap_native/research/` and `research_2/` are
**not** part of this chain — they're prior investigation/scratch work, kept for the record
but not imported by anything below. Confirmed by reading the actual `import` statements in
each file, not by re-deriving it from memory.

## The chain, in data-flow order

```
b3dm tiles (OneMap 3D-Tiles API)
    -> tile discovery + fetch          (sbg/onemap_native/tiles.py, sbg/onemap/client.py, sbg/onemap/b3dm.py)
    -> Draco decode + coordinate xform (sbg/onemap_native/transform.py)
    -> per-building extraction + seal  (sbg/onemap_native/extract.py)
    -> terrain (DTM crop + CDT + pads) (sbg/onemap_native/terrain.py, sbg/topo/dtm.py)
    -> [optional] wind-domain rotation (sbg/onemap_native/wind.py, heights.py)
    -> fuse (join + voxel remesh)      (sbg/onemap_native/blender/fuse_stl.py  OR  meshlib, inline in build.py)
    -> decimate + boolean clip + polish  (meshlib, inline in sbg/onemap_native/build.py)
    -> STL / GeoTIFF DEM export        (sbg/onemap_native/build.py, sbg/onemap_native/dem.py)
```

`sbg/onemap_native/build.py` is the **orchestrator** — it's the one file that imports
every other stage and runs them in order. Its CLI is the front door:

```
python -m sbg.onemap_native.build --bbox xmin,ymin,xmax,ymax -o out.stl [--store data/onemap_store]
```

## Stage 1 — tile discovery + fetch (scraping the b3dm)

- **`sbg/onemap/client.py`** — `requests.Session` wrapper for SLA OneMap's public 3D-Tiles
  API. Sets the browser `User-Agent`/`Referer` headers the live API requires (it 403s
  without them), walks the tileset tree (`load_tileset`), and does the range-request
  batch-table-only fetch used by the height-backfill crawl.
- **`sbg/onemap/b3dm.py`** — minimal, stdlib-only B3DM container parser (vendored, not the
  `onemap-slicer` clone's version). Pulls the feature/batch table JSON and the glTF/GLB
  payload out of a raw `.b3dm` byte stream.
- **`sbg/onemap_native/tiles.py`** — the real per-build tile layer: `domain_leaf_tiles()`
  walks the tileset and returns the finest-LOD leaf tiles intersecting a domain bbox;
  `fetch_tile()` reads from a local precomputed archive/store first, falls back to a live
  fetch; `feature_table()` / `load_gltf()` pull out the per-tile metadata and decoded glTF.
- **`sbg/onemap_native/precompute.py`** — offline bulk step (`precompute --local`) that
  pre-extracts every leaf tile's building pieces once into `data/onemap_store/`, so a real
  build reads pre-extracted pieces (~0.2s) instead of live-fetching+decoding per cutout
  (~79s). Not required — `build.py` works without `--store`, just slower.

## Stage 2 — Draco decode + coordinate transform

- **`sbg/onemap_native/transform.py`** — `local_to_svy21()`, the corrected 3D-Tiles
  transform: `RTC_CENTER + ZUP @ (R @ p_local + T)` (node rotation quaternion → matrix,
  Y-up→Z-up axis correction, then ECEF→WGS84→SVY21). This is the fix for the "half the
  tiles' ground scattered ±100m" bug — every building base now lands at exactly z=0.

## Stage 3 — per-building extraction + repair

- **`sbg/onemap_native/extract.py`** — `extract_domain_buildings()`: for every leaf tile in
  a domain, Draco-decodes the mesh (`DracoPy`), isolates each building by its `_BATCHID`
  vertex attribute, applies the Stage-2 transform, clips to the domain, and computes a
  per-piece footprint (proximity-clustered convex hulls, `_footprint_rings`). Also holds
  **`seal_piece()`** — the single most load-bearing fix in the whole pipeline: OneMap
  building meshes are un-welded, double-sided triangle soup with no coherent inside/outside,
  so any isosurface extractor (Blender's voxel remesh, meshlib's) shatters them into hundreds
  of fragments unless they're deduped, welded, wound consistently, and hole-filled first.
  `split_piece_components()` splits a sealed piece back into its real disjoint solids.

## Stage 4 — terrain

- **`sbg/topo/dtm.py`** — builds/reads the whole-island cached DTM raster (`data/dtm.tif`,
  20m native resolution from SLA's 1:250,000 contour data). `build_domain_dtm()` in
  `terrain.py` crops+resamples this cache per domain instead of re-interpolating from raw
  contour points (42× faster).
- **`sbg/onemap_native/terrain.py`** — the terrain half of the pipeline:
  `build_domain_dtm()` (crop the island DTM), `conforming_terrain()` (constrained Delaunay
  triangulation via the `triangle` library — building footprints/pads inserted as PSLG
  constraints so terrain and building-base vertices share exact coordinates, closing the
  seam by construction), `place_on_terrain_conforming()` (per-piece/per-connected-group pad
  elevation + skirt), `terrain_flat_base_solid()` (extrudes the terrain surface down to a
  flat closed solid). Depends on `extract.py` (`seal_piece`, `split_piece_components`).

## Stage 5 — wind-aligned CFD domains (optional)

- **`sbg/onemap_native/wind.py`** — pure geometry: given an area-of-interest polygon and a
  set of wind bearings, computes the COST-732-style buffer (5H upwind / 15H downwind / 5H
  lateral), the per-direction wind-aligned rectangle, and the build-once convex-hull
  envelope covering every requested direction. No I/O — this is the single source of truth
  for the rotation convention, reused identically by the CLI and the API.
- **`sbg/onemap_native/heights.py`** — cached OneMap building-height index
  (`data/onemap_buildings.jsonl` → cKDTree), used to estimate the tallest building H in an
  area before sizing the buffer.

## Stage 6 — fuse (join + voxel remesh)

- **`sbg/onemap_native/blender/fuse_stl.py`** — runs inside Blender's bundled Python
  (`blender --background --python fuse_stl.py --`). Imports the terrain + building PLYs,
  joins them into one mesh soup, and voxel-remeshes (OpenVDB) to resample a clean surface
  regardless of input mess. This is the **optional, non-default** fuse backend
  (`--fuse-backend blender`).
- **meshlib**, called inline from `sbg/onemap_native/build.py` (no separate module) — the
  **default** fuse backend: `mr.offsetMesh(offset=0)` calls the same OpenVDB routine
  Blender's voxel remesh does, ~4× faster, with no Blender/subprocess dependency.

## Stage 7 — decimate + boolean clip + polish

All inline in **`sbg/onemap_native/build.py`**, via `meshlib` (`mr.decimateMesh`,
`mr.boolean(..., Intersection)` against the domain box, `mr.resolveMeshDegenerations`,
component/debris dropping). No separate files — this is the tail of `build_domain_stl()`.

## Stage 8 — output

- **`sbg/onemap_native/build.py`** — writes the final watertight STL (and, with
  `--also-raw`, an unfused/undecimated raw-detail STL of the exact captured geometry, for
  the Ansys fault-tolerant meshing workflow rather than a strict-watertight one).
- **`sbg/onemap_native/dem.py`** — `build_dem()` / CLI: rasterizes the same raw terrain +
  building geometry (before the fuse) into a north-up GeoTIFF heightfield (`domain.dem.tif`)
  for consumers that want a DEM (e.g. the JAEA dose code) instead of a mesh. Can run
  standalone against an existing `.raw.stl`, or inline as part of a normal build
  (`--dem`).

## Not part of this chain (kept for context, not imported by the above)

- `sbg/onemap/crawl_tiles.py` + `sbg/onemap/backfill.py` — the historical bulk
  batch-table-only crawl that built `data/onemap_buildings.jsonl` (used only by
  `heights.py` for buffer sizing, not by extraction itself) and the v1 CityJSON
  height-backfill matcher.
- `sbg/onemap/mesh.py`, `sbg/onemap/embed.py`, `sbg/onemap/candidates.py` — the
  per-building landmark-replacement / OneMap-review-gate machinery, used by the v1
  CityJSON UI workflow, not by the v2 STL build.
- `sbg/blender/export_stl.py`, `sbg/blender/repair_stl.py`, `sbg/topo/conforming_mesh.py`,
  `sbg/extrude.py`, `sbg/build_sbg.py` — the whole v1 LoD1-box CityJSON→STL pipeline.
  Superseded by v2 for the STL deliverable.
- `sbg/onemap_native/research/`, `sbg/onemap_native/research_2/` — investigation scripts
  (MD1 disintegration root-causing, fTetWild/manifold3d/dilate-union experiments, Open3D
  morphological-closing prototypes, etc.). None of this is imported by `build.py`; it
  documents *why* the current design (sealing + meshlib) was chosen, not code in the chain.

## Running it

```bash
.venv/bin/python -m sbg.onemap_native.build \
    --bbox 21950,30250,22850,31150 \
    -o out.stl \
    --store data/onemap_store        # optional: pre-extracted pieces, ~0.2s vs ~79s live fetch
```

Verification, per this project's established convention: `meshlib` reports
`holes == 0` / `findMultipleEdges == 0` on the reloaded output, plus an independent strict
`trimesh` re-weld (`process=False`, `merge_vertices(digits_vertex=6)`) reporting
`is_watertight == True` and `body_count == 1` — trust these over a bare `trimesh
process=True` load, which has repeatedly given false negatives on this project's STLs
(see `sbg/onemap_native/TERRAIN.md` and the main plan doc for the forensics on why).
