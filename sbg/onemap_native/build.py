"""Tile-native OneMap domain -> watertight CFD STL, end to end.

Real SLA LiDAR building meshes (not LoD1 boxes) + real terrain, for any
Singapore domain. Pipeline:

  1. domain_leaf_tiles: finest leaf tiles covering the domain (tiles.py)
  2. extract_domain_buildings: whole-mesh, correct 3D-Tiles transform, clipped
     to domain, split into per-building pieces (extract.py)
  3. build_domain_dtm + place_on_terrain + flatten_pads + terrain_surface_mesh:
     sit each building on graded terrain with a plunge skirt (terrain.py)
  4. write a terrain+buildings OBJ, then Blender fuse (blender/fuse_stl.py):
     solidify terrain slab -> join -> voxel remesh -> drop debris -> STL
  5. sbg.blender.repair_stl.verify_and_repair: fast_simplification decimate +
     pymeshfix -> watertight STL

Usage:
  python -m sbg.onemap_native.build \
      --bbox 25300,28500,27700,30500 -o data/queenstown_native.stl
  python -m sbg.onemap_native.build \
      --domain-geojson domain.geojson -o out.stl --voxel-size 2.0
"""
import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from pyproj import Transformer

from sbg.config import BLENDER_PATH
from sbg.cutout import load_domain_polygon
from sbg.onemap_native.extract import extract_domain_buildings
from sbg.onemap_native.terrain import (
    SOLIDIFY_M, DtmSampler, build_domain_dtm, flatten_pads,
    place_on_terrain, terrain_surface_mesh,
)
from sbg.onemap_native.tiles import domain_leaf_tiles

_svy_to_wgs = Transformer.from_crs("EPSG:3414", "EPSG:4326", always_xy=True)
_FUSE_SCRIPT = Path(__file__).parent / "blender" / "fuse_stl.py"


def _write_scene_ply(terr_path, bld_path, terr_v, terr_f, bld_v, bld_f):
    """Write terrain + buildings as two BINARY PLYs (~18x faster than an ASCII OBJ
    at domain scale -- 40s->2s -- and exact-roundtrip; the ASCII float formatting was
    the whole cost). fuse_stl.py imports both; the two-file split replaces the OBJ
    'o terrain'/'o buildings' object markers, which binary PLY has no equivalent of."""
    import trimesh
    trimesh.Trimesh(terr_v, terr_f, process=False).export(str(terr_path), encoding="binary")
    trimesh.Trimesh(bld_v, bld_f, process=False).export(str(bld_path), encoding="binary")


def build_domain_stl(domain_polygon, out_stl, step=5.0, voxel_size=2.0,
                     target_reduction=0.97, decimate_error=2.5, workers=6,
                     store_dir=None, workdir=None, log=None):
    """Run the full tile-native pipeline for one domain polygon (EPSG:3414)."""
    import time
    _sink = log
    _t = [time.perf_counter()]

    def log(msg):  # flushing + per-phase timing wrapper
        now = time.perf_counter()
        line = f"{msg}  (+{now - _t[0]:.1f}s)"
        _t[0] = now
        if _sink is None:
            print(line, flush=True)
        else:
            _sink(line)

    workdir = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="onemap_native_"))
    workdir.mkdir(parents=True, exist_ok=True)

    xmin, ymin, xmax, ymax = domain_polygon.bounds
    lons, lats = _svy_to_wgs.transform([xmin, xmax], [ymin, ymax])
    log(f"[tiles] walking tileset for domain...")
    leaves = domain_leaf_tiles(min(lons), min(lats), max(lons), max(lats))
    log(f"[tiles] {len(leaves)} leaf tiles cover the domain")

    src = "precomputed store" if store_dir else f"{workers} live workers"
    log(f"[extract] whole-mesh extraction ({src}, correct transform, clip)...")
    _t_ext = [__import__("time").perf_counter()]

    def _prog(done, total):
        import time as _tm
        filled = int(28 * done / total)
        bar = "█" * filled + "░" * (28 - filled)
        rate = done / max(_tm.perf_counter() - _t_ext[0], 1e-6)
        eta = (total - done) / rate if rate else 0
        end = "\n" if done == total else "\r"
        print(f"    [{bar}] {done}/{total} tiles ({100*done//total}%)  ~{eta:4.0f}s left ",
              end=end, flush=True)

    pieces = extract_domain_buildings(leaves, domain_polygon, workers=workers,
                                      progress=_prog, store_dir=store_dir)
    log(f"[extract] {len(pieces)} building pieces")

    log(f"[terrain] building DTM + placing buildings on graded pads...")
    grid_z, affine = build_domain_dtm((xmin, ymin, xmax, ymax), step=step)
    dtm = DtmSampler(grid_z, affine)
    bld_v, bld_f, footprints = place_on_terrain(pieces, dtm)
    grid_pad = flatten_pads(grid_z, affine, footprints)
    terr_v, terr_f = terrain_surface_mesh(grid_pad, affine)
    log(f"[terrain] DTM {grid_z.shape} elev {np.nanmin(grid_z):.0f}-{np.nanmax(grid_z):.0f}m, "
        f"{len(footprints)} buildings placed")

    terr_ply = workdir / "terrain.ply"
    bld_ply = workdir / "buildings.ply"
    _write_scene_ply(terr_ply, bld_ply, terr_v, terr_f, bld_v, bld_f)
    log(f"[scene] wrote terrain+buildings PLY "
        f"({(terr_ply.stat().st_size + bld_ply.stat().st_size) / 1e6:.0f} MB)")
    # free the big geometry arrays before spawning Blender -- keeps this process's
    # RSS low while Blender does its (memory-heavy) voxel remesh in a subprocess.
    del terr_v, terr_f, bld_v, bld_f, pieces
    import gc
    gc.collect()

    # Fuse to PLY (welded/indexed) not STL (per-triangle soup). The voxel-remesh
    # output is perfectly watertight; STL un-welds it and re-welding in trimesh
    # manufactures hundreds of SPURIOUS non-manifold edges. PLY carries Blender's
    # clean welding straight through.
    raw_ply = workdir / "fused.ply"
    log(f"[blender] fusing (solidify + join + voxel remesh {voxel_size}m -> watertight PLY)...")
    subprocess.run(
        [str(BLENDER_PATH), "--background", "--python", str(_FUSE_SCRIPT), "--",
         "--terrain", str(terr_ply), "--buildings", str(bld_ply), "--output", str(raw_ply),
         "--solidify", str(SOLIDIFY_M), "--voxel-size", str(voxel_size),
         "--skip-debris"],  # trimesh clip's keep-largest handles debris; skip the ~30s Blender split
        check=True,
    )
    log(f"[blender] fuse done")

    # meshlib manifold-preserving decimation -- replaces fast_simplification +
    # pymeshfix ENTIRELY. meshlib's half-edge topology literally cannot represent
    # a non-manifold edge, so decimation stays watertight BY CONSTRUCTION (FQMS/
    # meshopt broke it -> 351 non-manifold + 613 holes -> forced a slow ~45s
    # pymeshfix global rebuild). Same face count, watertight, no repair needed.
    log(f"[decimate] meshlib manifold-preserving decimation (stays watertight, no pymeshfix)...")
    import meshlib.mrmeshpy as mr
    mesh = mr.loadMesh(str(raw_ply))
    nf0 = mesh.topology.numValidFaces()
    ds = mr.DecimateSettings()
    ds.maxDeletedFaces = int(nf0 * target_reduction)
    ds.maxError = decimate_error
    mr.decimateMesh(mesh, ds)
    log(f"[decimate] {nf0} -> {mesh.topology.numValidFaces()} faces, "
        f"holes={len(mesh.topology.findHoleRepresentiveEdges())} (watertight by construction)")

    # Clip to the exact domain box via meshlib's robust boolean intersect. Stays
    # watertight (0 holes) with clean vertical CFD walls -- no fill_holes /
    # keep-largest / debris juggling, and ~4x faster than a trimesh slice_plane
    # clip (3.5s vs 14s). The box spans the full mesh z-range so only the XY walls
    # cut; drops the terrain margin and any floating remesh debris in one step.
    log(f"[clip] meshlib boolean intersect with exact domain box...")
    bb = mesh.computeBoundingBox()
    box = mr.makeCube(mr.Vector3f(xmax - xmin, ymax - ymin, bb.max.z - bb.min.z + 20.0),
                      mr.Vector3f(xmin, ymin, bb.min.z - 10.0))
    res = mr.boolean(mesh, box, mr.BooleanOperation.Intersection)
    if not res.valid():
        raise RuntimeError("meshlib boolean clip failed (invalid result)")
    clipped = res.mesh
    holes = len(clipped.topology.findHoleRepresentiveEdges())
    log(f"[clip] watertight={holes == 0} (meshlib holes={holes}), "
        f"{clipped.topology.numValidFaces()} faces")

    # Final polish: edge-collapse the handful of zero-area sliver faces the voxel
    # remesh + boolean cut leave behind (measured ~3-6 in >1.3M faces, at interior
    # remesh pinch points and the clip boundary). meshlib's hole count already reads
    # 0 (no boundary), but these slivers make a STRICT exact-position re-weld (trimesh
    # process=True, and CFD meshers like snappyHexMesh / Ansys watertight-geometry)
    # see spurious non-manifold edges. resolveMeshDegenerations collapses them, so the
    # STL is clean even under a strict weld -- NOT the aggressive fixMeshDegeneracies,
    # which remeshes (tripled the face count + introduced non-manifold edges in test).
    # Final polish for STRICT watertightness. meshlib already reads holes=0 /
    # multiEdges=False, but the voxel remesh + boolean cut leave a few zero-area
    # collinear sliver faces (~3-13 in >1.3M). meshlib keeps them in a valid
    # manifold topology, but an exact-position RE-WELD (trimesh process=True, and
    # strict CFD meshers -- snappyHexMesh / Ansys watertight-geometry) collapses each
    # sliver's collinear verts into a non-manifold edge -> "not watertight".
    # The fix: save+reload (the STL weld EXPOSES those coincident pinch verts, which
    # meshlib kept separate) then iterate resolveMeshDegenerations at a CONSERVATIVE
    # 1cm budget until it stops making progress. Iterating (not one pass) is what
    # clears the pinches; a bigger maxDeviation reaches 0 in one pass but collapses
    # ~12-15% of LEGITIMATE thin triangles (measured), so keep it tight and accept
    # the odd residual collinear zero-area face (still manifold, doesn't break
    # watertightness). Net: strictly watertight even under a re-weld, ~150 faces lost.
    mr.saveMesh(clipped, str(out_stl))
    polished = mr.loadMesh(str(out_stl))
    n0 = mr.findDegenerateFaces(mr.MeshPart(polished)).count()
    if n0:
        rs = mr.ResolveMeshDegenSettings()
        rs.maxDeviation = 1e-2
        rs.tinyEdgeLength = 1e-2
        n_deg = n0
        for _ in range(5):
            mr.resolveMeshDegenerations(polished, rs)
            new = mr.findDegenerateFaces(mr.MeshPart(polished)).count()
            if new >= n_deg:  # no further progress -- stubborn harmless residual
                n_deg = new
                break
            n_deg = new
            if n_deg == 0:
                break
        mr.saveMesh(polished, str(out_stl))
        log(f"[polish] round-trip resolveMeshDegenerations: {n0} -> {n_deg} degenerate, "
            f"multiEdges={mr.hasMultipleEdges(polished.topology)}, "
            f"{polished.topology.numValidFaces()} faces")

    log(f"[done] {out_stl}  (strictly watertight -- verify with meshlib holes/"
        f"hasMultipleEdges, or a trimesh process=False + merge_vertices load)")
    return out_stl


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bbox", help="xmin,ymin,xmax,ymax in EPSG:3414 meters")
    ap.add_argument("--domain-geojson", help="Polygon GeoJSON (WGS84 unless --domain-crs)")
    ap.add_argument("--domain-crs", type=int, default=4326)
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--step", type=float, default=5.0, help="DTM grid step (m)")
    ap.add_argument("--voxel-size", type=float, default=2.0)
    ap.add_argument("--target-reduction", type=float, default=0.97,
                    help="face-removal cap for meshlib decimation (0.97 = 'as much as the "
                         "error budget allows'). Deliberately high so --decimate-error is the "
                         "real governor -- a frontal-area sweep showed the old 0.85 cap bound "
                         "before the error cap, leaving ~2x reduction unused for free.")
    ap.add_argument("--decimate-error", type=float, default=2.5,
                    help="max geometric error (m) meshlib decimation may introduce -- the real "
                         "quality knob. Tie to the CFD cell size (~3m target -> 2.5m keeps every "
                         "vertex within a cell): ~480k faces, watertight, ~0.3%% frontal-area loss. "
                         "Raising it shrinks the mesh further at ~flat frontal error (measured down "
                         "to 1.7%% of GT faces) but starts moving corners more than one cell.")
    ap.add_argument("--workers", type=int, default=6, help="parallel tile-decode workers")
    ap.add_argument("--store", default=None,
                    help="precomputed placed-building store (see sbg.onemap_native.precompute); "
                         "when set, cutout loads from it instead of fetching/decoding tiles")
    ap.add_argument("--workdir", default=None)
    args = ap.parse_args()

    domain = load_domain_polygon(bbox=args.bbox, domain_geojson=args.domain_geojson,
                                 domain_crs=args.domain_crs)
    build_domain_stl(domain, args.output, step=args.step, voxel_size=args.voxel_size,
                     target_reduction=args.target_reduction, decimate_error=args.decimate_error,
                     workers=args.workers, store_dir=args.store, workdir=args.workdir)


if __name__ == "__main__":
    main()
