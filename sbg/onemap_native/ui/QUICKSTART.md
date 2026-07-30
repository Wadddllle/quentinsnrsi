# SBG v2 — CFD Domain → STL tool (quickstart)

A lean local web app: draw a CFD domain boundary on a map, generate a watertight
STL from real OneMap LiDAR building meshes + terrain, view and download it.

## Run it (once set up)

```bash
python -m sbg.onemap_native.ui                 # opens http://127.0.0.1:8000 in a browser
python -m sbg.onemap_native.ui --store data/onemap_store   # fast: use the precomputed piece store
python -m sbg.onemap_native.ui --blender /path/to/blender  # point at your Blender executable
```

Draw a border (Polygon / Rectangle / Point+buffer), watch the kept/crossing
buildings update, click **Generate watertight STL**, then **Download** or **View in 3D**.

## Setting up on a fresh machine

Only two things aren't `pip install`-able and must be provided:

1. **Python deps** — `pip install -r requirements-sbg.txt` into a Python 3.12 venv.
2. **Blender** (for the voxel-remesh fuse step) — download the standalone tarball
   (Blender 4.5 LTS) and pass its path via `--blender /path/to/blender`
   (or set `SBG_BLENDER_PATH`). Not `pip install bpy` (wrong Python pin).

**Data to copy over (small):**
- `sg_buildings_v5.geojson` (~135 MB) — footprints + `building_archetype` for the 2D map.
- `data/dtm.tif` (~30 MB) — whole-island terrain cache.

**Tiles:** by default the tool **fetches OneMap tiles live** per domain — no 2.6 GB
store or 113 GB archive needed. For faster repeat cutouts, copy `data/onemap_store/`
(~2.6 GB) and pass `--store data/onemap_store`.

**Frontend:** the built UI is served from `webui-v2/dist/`. Rebuild it if you change
the frontend: `cd webui-v2 && npm install && npm run build`. (Already built = nothing to do.)

## First run

The first launch reprojects the 118k footprints (~22 s) and caches them to
`data/footprint_index_cache.pkl`; subsequent launches start in ~6 s.

## Dev mode (frontend iteration)

```bash
python -m sbg.onemap_native.ui --dev --port 8011      # backend only, CORS on
cd webui-v2 && npm run dev                              # Vite dev server, proxies /api -> 8011
```
