# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **Start with [`README.md`](README.md)** — it documents the current (v2) pipeline, the two
> output paths, the intended CFD workflow, and how to run/containerize it. This file is the
> working-notes companion: build commands, verification habits, and the v1 architecture.
>
> **v2 (`sbg/onemap_native/`) is the current pipeline** and the one to touch for STL work.
> The v1 CityJSON stack described below still works and is what you want for per-building
> semantics, but it is not the STL path any more. Container files: `Dockerfile`,
> `docker-compose.yml`, `.dockerignore`; deps are `requirements-v2.txt` (v2, pinned, what
> the image installs) and `requirements-sbg.txt` (everything, incl. v1).

## What this is

SBG (Singapore Building Geometry): a pipeline that builds a Singapore-wide CityJSON building dataset from OSM footprints + SLA OneMap 3D data, overlays terrain, cuts out CFD-domain-sized subsets, and exports watertight STLs via Blender for radionuclide plume-dispersion CFD meshing. `sbg/` is the Python pipeline/library; `sbg/ui/` + `webui/` is a local-first FastAPI+Vue3 web app wrapping it (run via `python -m sbg.ui`, opens a browser tab — no hosting, single local instance per user).

Full design history, every architecture decision and why, and every non-obvious bug found (with root causes) live in `/home/quentin/.claude/plans/we-are-in-deep-imperative-petal.md` — read it before assuming something is unexplored or before re-deriving an approach that was already tried and rejected.

> **v2 no longer needs Blender.** `build_domain_stl(fuse_backend=...)` defaults to
> `"meshlib"` — Blender's `REMESH(VOXEL)` and meshlib's `offsetMesh(offset=0)` both call
> OpenVDB, and they are equivalent end-to-end on three real domains (holes/open/
> non-manifold/zero-area all 0, strictly watertight, volume agreeing to 0.001–0.004%),
> with meshlib 4.25× faster on the fuse. `--fuse-backend blender` is an escape hatch and
> is the only thing that still needs `SBG_BLENDER_PATH`. **v1 (`sbg/blender/`) still
> requires Blender.** Related: the boolean clip now nudges the domain polygon in by
> `SBG_CLIP_NUDGE_M` (1 mm) so it never cuts through voxel-grid vertices — that fixed a
> latent zero-area-sliver bug present in *both* backends.
>
> **The mesh-quality workstream is CLOSED (2026-08-27). Do not reopen it unprompted.**
> The CFD path was never blocked: snappyHexMesh meshed the raw export (1,030,259 cells,
> no errors) and Ansys fault-tolerant meshing already uses it. The long "watertight,
> 0 non-manifold, 0 self-intersection" chase was for the Ansys *watertight* workflow,
> which is one option of three and not the one in use. The one load-bearing fix is
> `seal_piece` (in `sbg/onemap_native/extract.py`) — OneMap meshes are un-welded
> double-sided soup, which no isosurface extractor can sign; sealing is why buildings
> stop disintegrating. Solved-but-deliberately-unwired work (per-building fTetWild +
> `pymeshfix` at 287/287 clean, manifold3d union, dilate→union) and the exit path if it
> ever reopens are in `sbg/onemap_native/research_2/PROBLEM_BRIEF.md` §13–§14.
> **Active work is the dose/CFD side** — see `dose/NOTES.md`.

`dose/` is the radionuclide dose workstream: Fluent particle tracks → OpenMC/DAGMC photon
transport → cloudshine and groundshine maps. `dose/NOTES.md` is its run log, settled
decisions, and deferred features; `dose/PARTICLES_XML_FORMAT.md` documents the CFD-Post
`<ParticleTracks>` format. Nuclear data lives at `/home/quentin/nuclear_data`; the
MOAB/DAGMC toolchain is a separate conda env (`~/tools/mamba/root/envs/{dagmc,moabpy}`),
**not** the project `.venv`.

## Setup and commands

Python: existing venv at `.venv/` (Python 3.12). Install deps: `.venv/bin/pip install -r requirements-sbg.txt`. No `pyproject.toml`/`setup.py` — `sbg/` is used in-place, always invoke via `.venv/bin/python -m sbg.<module>` or `.venv/bin/python sbg/<script>.py` from the repo root.

Frontend: `cd webui && npm install && npm run dev` (Vite dev server) or `npm run build` (writes `webui/dist/`, which `sbg/ui/app.py` serves directly in production). `npm install` runs `patch-package` via `postinstall` — patches in `webui/patches/` fix real bugs in the pinned `cityjson-threejs-loader` dependency (a `Uint16Array`→`Float32Array` object-id capacity bug; see the plan doc). Don't bump `cityjson-threejs-loader` or `three` casually — `three` is pinned to `^0.165.0` specifically because the loader's peer dependency requires it (upgrading broke `sRGBEncoding` exports once already).

Running the app: `.venv/bin/python -m sbg.ui` (serves the built `webui/dist/`) or, for frontend dev, `.venv/bin/python -m sbg.ui --dev --port 8010` (skips serving the built bundle, enables CORS) alongside `npm run dev` in `webui/` (Vite proxies `/api` to port 8010, see `webui/vite.config.js`). Useful flags: `--dataset <path>` to load a small test CityJSON instead of the full `data/sbg.city.json` for fast iteration, `--no-browser`.

No automated test suite (no pytest, no vitest) — verification in this project is direct: `cjio <file> info`/`validate` on any CityJSON output, `trimesh.load(path).is_watertight` for STL output, numeric seam/manifold checks (see `conforming_mesh.py`'s exposed-boundary-vertex pattern), and real Playwright browser sessions for frontend changes (throwaway scripts, not committed). Always test pipeline scripts against a small fixture (`data/small_test_town.city.json` / `--dataset` flag) before running against the full 118k-building `data/sbg.city.json`.

Key CLI entrypoints (all under `sbg/`, each has its own `argparse` — run with `--help`):
- `python -m sbg.build_sbg` — Deliverable 1, builds `data/sbg.city.json` from `sg_buildings_v5.geojson`.
- `python -m sbg.onemap.crawl_tiles` — bulk OneMap batch-table crawl (already run once; output cached at `data/onemap_buildings.jsonl`).
- `python sbg/cutout.py --bbox ... -o out.city.json` — Deliverable 3, domain cutout.
- `python sbg/topo/dtm.py --bbox ...` — build a local DTM GeoTIFF from cached contour points.
- `python sbg/topo/conforming_mesh.py --dtm ... --sbg ... -o ...` — Deliverable 2 for a real domain (terrain + buildings, constrained triangulation).
- `python sbg/edit.py --demo` — add/remove smoke test.
- Blender step (not run directly with Python): `<blender> --background --python sbg/blender/export_stl.py -- <args>`, then `.venv/bin/python sbg/blender/repair_stl.py input.stl -o output.stl`. Blender install path is hardcoded in `sbg/config.py::BLENDER_PATH` (a standalone tarball install, not `pip install bpy` — PyPI's wheel is pinned to an incompatible Python version).

`data/` is gitignored (large generated/downloaded files) except a strict allowlist inside it — see below.

## Architecture

### The CityJSON data model

Everything downstream operates on one CityJSON file (`data/sbg.city.json`, ~185MB, ~118,782 `Building` CityObjects) with a real `transform`/quantization (`scale=[0.001,...]`) and EPSG:3414 (SVY21, meters) as the working CRS throughout — WGS84 inputs get reprojected once at ingest and never carried further. `sbg/io_cityjson.py` is the shared foundation nearly every other module builds on: `VertexPool`/`AppendOnlyVertexPool` (dedup + quantize), `footprint_rings`/`building_footprint_polygon` (reconstructs a shapely polygon from a CityObject's geometry — handles `Solid`/`CompositeSolid` exactly, falls back to a convex hull for `MultiSurface`/mesh-embedded buildings), `walk_vertex_indices`/`remap_vertex_indices`/`subset_cityjson` (generic boundary-index traversal reused by cutout, add/remove, and the UI's bbox-scoped dataset endpoint alike).

Buildings are LoD1 prismatic extrusions (`sbg/extrude.py`) from flat footprint + a single scalar height — the only exceptions are a handful of OneMap-mesh-embedded landmarks (`sbg/onemap/embed.py`, tagged `height_source="onemap_mesh"`, geometry type `MultiSurface`) where a box is a genuinely bad approximation (domes, curved roofs). Height resolution is a coalesce chain tracked via `attributes.height_source`: real OSM height → `levels*3.2` → OneMap-backfilled real measured height → flat `3.2m` default (`estimated_default`) — always check `height_source` before trusting a building's height, `estimated_default`/`osm` are known-unreliable.

### The OneMap 3D pipeline (`sbg/onemap/`)

SLA's public OneMap 3D Tiles API (`sg_noterrain_tiles`, no key, but needs a browser `User-Agent`/`Referer` or it 403s — see `client.py::HEADERS`) is the source of real government-surveyed heights and, for a few landmarks, real meshes. Two very different fetch modes: `client.py::fetch_batch_table()` (an HTTP Range request pulling just the ~KB-scale attribute table, used for the bulk height-backfill crawl) vs. full-tile fetch + Draco decode (`mesh.py`, for real geometry). A tile's mesh is almost always a *combined* structure batching many buildings together — per-building isolation must use the `_BATCHID` Draco attribute (`mesh.py::extract_building_mesh`), never `mesh.name` string-matching (that only ever finds one "hero" building per tile, a real bug this project shipped once already).

`backfill.py::match_buildings()` is a real production geometry-height matcher (not naive nearest-neighbor) — per-building-size match radius, greedy edge-sort matching (not exact bipartite assignment, which blows up combinatorially on dense HDB estates), with a fallback pass so legitimate duplicate OSM footprints still share a height. It also *regenerates geometry*, not just the `height` attribute — `_regenerate_height()` moves vertices in place when safe (exclusively referenced by one building) or appends a private copy when a vertex is shared with a neighboring CityObject, using a global one-pass vertex-refcount to decide which.

### The terrain/domain pipeline (`sbg/topo/`)

Two distinct code paths for two distinct needs, do not conflate them: `overlay_cityjson.py` (whole-island, coarse, centroid-drape — cheap, approximate) vs. `conforming_mesh.py` (any real CFD domain — constrained Delaunay triangulation via the `triangle` library, building footprints inserted as PSLG holes/constraints so terrain and building-base vertices share exact `VertexPool` indices at the seam, not just numerically-close coordinates). `conforming_mesh.py` also carries the "podium slab" mechanism (`_podium_faces`/`PODIUM_PLUNGE_M`) that guarantees solid ground-contact backing under preserved-mesh (non-box) buildings regardless of real voids in the source mesh — this becomes relevant for *every* building, not just the rare preserved-mesh case, if/when the pipeline moves toward using real OneMap meshes more broadly (see the plan doc's OneMap tile-native investigation).

### The STL/Blender pipeline (`sbg/blender/`)

Runs inside Blender's own bundled Python (`export_stl.py`, invoked via `--background --python ... --`), separate from `.venv`. Key non-obvious design point: boolean union does **not** work at this data scale/messiness (tested and rejected — see the plan doc's Phase 5 post-mortem) — the working approach is solidify the terrain into a real slab, join every building (deep-plunged boxes, or `remesh_sharp()`'d preserved meshes) into one mesh soup with all the overlaps left in, then **Voxel Remesh** to resample inside/outside on a grid, which doesn't care that the input is a mess. Decimation (`fast_simplification`, not Blender's own single-threaded `Decimate`/`COLLAPSE`) and `pymeshfix` repair happen afterward in `.venv`-side `repair_stl.py`, not inside Blender.

### The web UI (`sbg/ui/` + `webui/`)

FastAPI routers (`sbg/ui/routers/`) are thin wrappers over the `sbg/` library — no pipeline logic duplicated in the UI layer. `sbg/ui/app.py`'s `lifespan` loads the whole dataset once into `app.state.cm` and builds `SpatialIndex` (an `STRtree` over every footprint, `sbg/ui/spatial_index.py`) once at startup — every mutating endpoint (`buildings.py`, `attributes.py`) operates in-memory against that shared state, invalidating cheap caches (`app.state.full_island_buildings_body`) rather than recomputing per-request. `sbg/ui/session.py` is an in-memory undo/mutation log (not persisted) with a boundary at "Save Version" — going further back than the current session is `versioning.py`'s job (a dedicated `git` repo scoped to `data/`, allowlist-only `.gitignore`, restore via single-path `git checkout <hash> -- sbg.city.json`, never a branch/reset).

Frontend: a single full-viewport 2D/3D mode toggle (not a split pane — this was tried and rejected), not two independently-designed views. 2D (`OrthoWebGLView.vue`) is WebGL/Three.js (not Canvas2D — CPU-bound redraw of 118k footprints was measured too slow), one merged `BufferGeometry` + per-vertex color attribute for the whole island, one draw call. 3D (`ThreeJsViewer.vue`, a port of the reference `ninja/` viewer's component) always eager-loads the full island now (a deliberate simplification after a scoped/full dual-mode was found to be unneeded complexity). Both views are real, independent `WebGLRenderer` instances — kept deliberately separate rather than unified, to keep the hard-won-stable 3D pipeline isolated from 2D risk.

Two categories of past bugs worth knowing before touching this code:
1. **Vue reactivity + huge data**: `citymodel` is always a `shallowRef`, reassigned wholesale, never deep-reactive or mutated in place — a `deep: true` watcher on it, or letting a `CityJSONWorkerParser` instance land in Options API `data()`, are both real, previously-hit bugs (structuredClone failures, multi-minute reactivity-traversal stalls at full-island scale). Small per-session edit-tracking maps (`attributeOverrides`) are fine as normal deep-reactive `ref`s — the distinction is dataset size, not a blanket rule.
2. **TypedArray silent truncation**: writing past a pre-sized `Float32Array`/`Uint16Array`'s length is silently dropped, not an exception — this project has shipped two real bugs from exactly this (a 2D geometry pre-allocation miscalculation that quietly deleted half of Singapore's buildings from view, and a `cityjson-threejs-loader` object-id `Uint16Array` capacity overflow at >65,535 buildings, fixed via `patch-package`). Any future pre-sized-buffer optimization needs either a provably-correct bound or a loud failure-mode guard, never a silent one.

## Vendored reference repos (not part of this project's own code)

`ninja/`, `onemap-slicer/`, `buildings.city/` are each independently git-cloned (own `.git`, excluded from this repo's tracking via `.gitignore`) reference implementations — `ninja` (official CityJSON web viewer) is what `ThreeJsViewer.vue` was ported from and is worth diffing against when debugging a three.js/`cityjson-threejs-loader` issue that "should just work like the reference does." Don't edit inside them expecting changes to be tracked by this repo.
