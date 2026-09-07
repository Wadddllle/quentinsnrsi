# SBG — Singapore Building Geometry → CFD STL

Turns any Singapore map region into a 3D surface mesh (STL) of the real buildings and
terrain in it, ready to be volume-meshed by a CFD solver. Built to feed radionuclide
plume-dispersion modelling at town scale.

Buildings are **real SLA LiDAR capture** (OneMap 3D Tiles), not extruded boxes. Terrain
comes from SLA contour data. You give it a bounding box or a polygon; it gives you an STL.

```bash
.venv/bin/python -m sbg.onemap_native.build --bbox 29041,28858,29241,29058 -o duxton.stl --voxel-size 0
```

or draw the domain on a map in the browser:

```bash
.venv/bin/python -m sbg.onemap_native.ui
```

---

## 1. What comes out: two paths, pick by your mesher

The same pipeline ends two different ways, and **which one you want depends entirely on
what your CFD mesher does with the surface.**

| | **Raw** (`--voxel-size 0`) | **Watertight** (`--voxel-size 2.0`, default) |
|---|---|---|
| Geometry | exact LiDAR capture, full detail | resampled on a voxel grid |
| Watertight | no — triangle soup, many separate solids | yes, single closed manifold |
| Self-intersections | yes (~thousands per domain) | none |
| Faces (400 m domain) | ~16 k | ~340 k for a 2 km domain; small domains far less |
| Time (400 m, from store) | **~3 s** | ~40–90 s |
| **Ansys Fluent — fault-tolerant / wrap** | ✅ **this is the intended target** | ✅ |
| **Ansys Fluent — watertight geometry** | ❌ rejects it (see below) | ✅ |
| **OpenFOAM snappyHexMesh** | ✅ | ✅ |

### Why raw is the default choice here

Ansys' **fault-tolerant meshing (FTM)** wraps the surface — it shrink-wraps a shell around
your geometry and never has to resolve the input's own topology. It does not care that the
raw mesh is open, multi-solid, or self-intersecting. So it accepts full-detail LiDAR
directly, and you skip the voxel remesh entirely (~30× faster, no detail loss).

The cost is memory: FTM's wrap is memory-hungry, and a ~2 km domain of raw geometry needs a
large workstation. If that's a problem, the watertight path trades detail for a mesh a
lighter workflow can chew.

### Why raw is rejected by Ansys' *watertight* workflow (and not by snappyHexMesh)

Tested, not assumed:

- **snappyHexMesh ray-casts** against the surface. It never retriangulates it, so
  self-intersections are simply invisible to it. It meshed our raw output fine
  (`Finished meshing without any errors`, ~1 M cells).
- **Ansys' watertight workflow surface-remeshes.** A remesher has no defined answer for two
  triangles passing through each other, so it fails ("Failed to remesh 148 area(s)").

So "Ansys watertight is stricter than snappy" is true, but in one specific way:
self-intersection, not closure. The raw output *is* closed per-solid; it self-intersects
where building meshes interpenetrate the terrain.

---

## 2. How it works

```
 bbox / polygon (EPSG:3414)
        │
        ▼
 ┌─ tiles.py ────────── walk OneMap's 3D-Tiles tileset, find the finest leaf
 │                      tiles covering the domain                    (~0.3 s)
 ▼
 ┌─ extract.py ──────── fetch each tile, Draco-decode, apply the 3D-Tiles
 │                      transform, clip to domain, split into per-building
 │                      pieces, seal each one                     (0.2 s–80 s)
 ▼
 ┌─ terrain.py ──────── DTM for the domain, sit each building on a graded flat
 │                      pad, constrained-Delaunay terrain that conforms exactly
 │                      to every footprint, extrude down to a flat base   (~1 s)
 ▼
 ├──── raw ──────────── write terrain + buildings straight to STL. Done.
 │
 └──── watertight ───── join everything into one soup → voxel remesh → decimate,
                        boolean-clip to the exact domain, drop debris, polish
                        to strict watertightness
```

**Working CRS is EPSG:3414 (SVY21, metres) throughout.** WGS84 only appears at the
tileset-query boundary. All bboxes, polygons and coordinates you pass in are SVY21.

### The parts worth knowing about

**`transform.py` — the 3D-Tiles transform.** `RTC_CENTER + ZUP·(R·p + T)`, then
ECEF→WGS84→SVY21. Getting this wrong scatters tile ground planes by −130 to +266 m while
each tile still looks internally consistent, so it is invisible in any top-down view. Every
building base lands at exactly z=0 when it's right (OneMap models all bases on one datum;
real ground comes from the DTM separately).

**`extract.py::seal_piece` — the single most important fix in the project.** OneMap meshes
arrive as *double-sided, unwelded triangle soup*: every real face stored twice with opposite
winding, no shared vertices. A signed distance field over that has no coherent sign, so
**any** voxel remesh (Blender's and meshlib's alike) shatters the building into hundreds of
fragments. Dedup + weld + fix winding + fill remaining holes, and the mesh is essentially
already closed. This is why buildings survive the remesh now.

**`terrain.py` — conforming triangulation.** Building footprints are inserted as constraints
into the terrain triangulation itself, so terrain vertices land exactly on footprint edges.
The seam closes by construction rather than by being numerically close. `--placement`
controls how a structure spanning real relief gets levelled — see
[`sbg/onemap_native/TERRAIN.md`](sbg/onemap_native/TERRAIN.md), which is the honest account
of what the terrain can and cannot do (short version: the source contours are 20 m interval
at 1:250,000, so absolute ground is uncertain to roughly ±10 m and no algorithm fixes that).

---

## 3. Running it

The whole goal, in order — clone, install, get the data, run:

```bash
git clone https://github.com/Wadddllle/quentinsnrsi && cd snrsi
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-v2.txt
```

Then get the data. Two files are required, one is a big optional speed-up:

**Fastest — download the pre-built bundle:** ask quentin he has the ssd. Unzip it so
`sg_buildings_v5.geojson` lands at the **repo root** (next to this README) and `dtm.tif` +
`onemap_store/` land under `data/`.

**From scratch instead** (you'll need this eventually anyway, e.g. to refresh with newer
OneMap data — same commands either way):
- `sg_buildings_v5.geojson` — an external dataset (NUS City Syntax Lab's buildings.sg), not something
  this repo generates. Download it from [there](https://github.com/City-Syntax/buildings.sg/blob/main/download/sg_buildings_v5.zip) and place it at the repo root. 
- `data/dtm.tif` — built from `NationalMapLine.geojson` (634 MB, an SLA/data.gov.sg
  basemap; download it from [here](https://data.gov.sg/datasets/d_10480c0b59e65663dfae1028ff4aa8bb/view), place it at the **repo root**, then `.venv/bin/python -m sbg.topo.contours`
  extracts `data/contour_points.npz`, and `.venv/bin/python -m sbg.topo.dtm` builds `dtm.tif`
  from that). Only needed if you're not using the bundle — `dtm.tif` itself is small (30 MB)
  and rarely needs rebuilding, unlike the OneMap-derived files above.
- `data/onemap_store/` — `.venv/bin/python -m sbg.onemap_native.precompute --out data/onemap_store`
  (crawls every OneMap tile, so it's slow — run `--help` on it for tuning flags). **Optional**:
  without it the app fetches tiles live per domain instead (~80 s vs ~0.2 s per build), which
  is fine for occasional use.
- `data/onemap_buildings.jsonl` — **optional**, only powers the wind/buffer sizing feature.
  `.venv/bin/python -m sbg.onemap.crawl_tiles`.

Then: §4 for the web UI, §5 for the CLI. To run this in Podman/Docker or host it on Google
Cloud Run instead, see [`DEPLOY.md`](DEPLOY.md).

---

## 4. Web UI

```bash
cd webui-v2 && npm install && npm run build && cd ..
.venv/bin/python -m sbg.onemap_native.ui
```

Add `--store data/onemap_store` if you downloaded it, `--port 8000` to change the port.

Draw a domain (rectangle / polygon / point+buffer) on a 2D map of the whole island, see
which buildings are kept vs. crossing the boundary, generate the STL as a background job
with live progress, view it in 3D, download it. Advanced settings expose the build flags
from §5.

Frontend dev loop: `.venv/bin/python -m sbg.onemap_native.ui --dev --port 8011` alongside
`cd webui-v2 && npm run dev`.

No npm/node yet:

```bash
sudo apt-get install curl
curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/master/install.sh | bash
source ~/.bashrc
nvm install --lts
```

---

## 5. CLI reference

```bash
.venv/bin/python -m sbg.onemap_native.build --bbox xmin,ymin,xmax,ymax -o out.stl
.venv/bin/python -m sbg.onemap_native.build --domain-geojson domain.geojson -o out.stl
```

Both accept the flags below, plus anything else `--help` lists (`[...]` in a usage line
always means "optional," never something to type literally — same convention argparse's own
`--help` uses).

| flag | default | what it does |
|---|---|---|
| `--voxel-size` | `2.0` | `0` = raw path. `>0` = voxel remesh at that resolution. |
| `--decimate-error` | `2.5` | **The quality knob.** Max geometric error (m) decimation may introduce. Tie it to your CFD cell size — at a ~3 m target, 2.5 keeps every vertex inside one cell. |
| `--target-reduction` | `0.97` | Face-count cap. Deliberately high so `--decimate-error` is the real governor. |
| `--step` | `5.0` | DTM grid step (m). |
| `--placement` | `drape` | `group` / `drape` / `laplacian` — how connected structures spanning relief are levelled. |
| `--coupling-lambda` | `10` | `laplacian` only. Higher = flatter, lower = more terracing. |
| `--no-base` | off | Exclude the ground plane from the export (buildings only), for solvers that build their own enclosure. Terrain still backs the remesh internally. |
| `--fuse-backend` | `meshlib` | `meshlib` (no Blender needed) or `blender` (needs `SBG_BLENDER_PATH`) for the voxel-remesh fuse step. Equivalent output; meshlib is ~4× faster. |
| `--store` | live fetch | Use a precomputed piece store. |
| `--workers` | `6` | Parallel tile-decode workers (live fetch only). |

**On `--decimate-error`:** a frontal-area (blockage) sweep showed the error is essentially
flat at ~0.3 % across the whole useful range — that ~0.3 % is the voxel-remesh noise floor,
not decimation damage. So you can decimate hard for free; the limit is that `maxError` is a
real geometric displacement, and once it exceeds your cell size, corners move more than the
solver can see anyway.

---

## 6. Known limits — read before trusting output

- **Terrain elevation is coarse.** 20 m contour interval at 1:250,000 scale is the only
  elevation source that exists for this. Absolute ground is uncertain to roughly ±10 m in
  the worst spots. Fine for town-scale dispersion; not a site survey.
- **Domain-edge buildings are dropped, not clipped.** A building must be fully inside the
  domain to be kept. A building sliced flat at the boundary is both unphysical and a meshing
  hazard. Practical consequence: **your CFD domain should sit inside a larger cutout**, with
  the boundary in open flow (urban-CFD guidance: inlet ≥5H, sides/top ≥5H, outlet ≥15H).
- **A rigid captured mesh cannot bend to a hillside.** One connected structure spanning real
  relief sits at one level, so part of it may bury or float slightly. `--placement` chooses
  *how* that error is distributed, not whether it exists.
- **Some buildings are genuinely missing from OneMap.** Underground stations, a small tail of
  structures with no usable capture. Measured coverage on a real domain: ~98 % of recorded
  buildings placed.
- **No per-building identity in the output.** The STL is one fused mesh (or a set of solids);
  attributes do not survive. `extract.py` still carries `_BATCHID` per piece if you need to
  reintroduce it.
- **Raw output self-intersects** where buildings meet terrain. By design — see §1.

---

## 7. Repo layout

```
sbg/
  onemap_native/     ← v2, the current pipeline. Everything above lives here.
    build.py           end-to-end CLI
    tiles.py           tileset walk, tile fetch
    extract.py         Draco decode, transform, clip, split, seal
    transform.py       the 3D-Tiles coordinate transform
    terrain.py         DTM, pads, conforming triangulation, base solid
    precompute.py      build the whole-island piece store
    blender/           voxel-remesh fuse (watertight path only)
    ui/                FastAPI backend for the web app
    TERRAIN.md         deep-dive on terrain: what works, what can't be fixed
    research/          disposable investigation scripts, kept for provenance
  onemap/            OneMap client, batch-table crawl, per-building mesh extract
  topo/              DTM construction from SLA contours
  blender/, ui/, *.py  ← v1 CityJSON pipeline (LoD1 boxes + semantic editing)
webui-v2/            Vue 3 + Three.js frontend for the v2 UI
webui/               v1 frontend (CityJSON viewer/editor)
doc/                 LaTeX write-ups
```

**v1 vs v2.** v1 builds a semantic CityJSON dataset of all 118,782 buildings as LoD1
extruded boxes with OneMap-backfilled heights, with a full editing web UI. It still works
and is what you want if you need *per-building semantics*. v2 goes straight from LiDAR tiles
to a CFD mesh and is what you want for an STL. They share `sbg/config.py`, `sbg/cutout.py`,
and the OneMap client; nothing else.

Design history, every rejected approach and why, is in `CLAUDE.md` and the plan document it
points to. Worth reading before re-deriving something — a lot of the negative results were
expensive.
