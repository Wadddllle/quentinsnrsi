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
from sbg.blender.repair_stl import verify_and_repair
from sbg.onemap_native.extract import extract_domain_buildings
from sbg.onemap_native.terrain import (
    SOLIDIFY_M, DtmSampler, build_domain_dtm, flatten_pads,
    place_on_terrain, terrain_surface_mesh,
)
from sbg.onemap_native.tiles import domain_leaf_tiles

_svy_to_wgs = Transformer.from_crs("EPSG:3414", "EPSG:4326", always_xy=True)
_FUSE_SCRIPT = Path(__file__).parent / "blender" / "fuse_stl.py"


def _write_obj(path, terr_v, terr_f, bld_v, bld_f):
    with open(path, "w") as f:
        f.write("o terrain\n")
        np.savetxt(f, terr_v, fmt="v %.3f %.3f %.3f")
        np.savetxt(f, terr_f + 1, fmt="f %d %d %d")
        f.write("o buildings\n")
        np.savetxt(f, bld_v, fmt="v %.3f %.3f %.3f")
        np.savetxt(f, bld_f + 1 + len(terr_v), fmt="f %d %d %d")


def build_domain_stl(domain_polygon, out_stl, step=5.0, voxel_size=2.0,
                     target_reduction=0.85, workers=6, workdir=None, log=None):
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

    log(f"[extract] whole-mesh extraction ({workers} workers, correct transform, clip)...")
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

    pieces = extract_domain_buildings(leaves, domain_polygon, workers=workers, progress=_prog)
    log(f"[extract] {len(pieces)} building pieces")

    log(f"[terrain] building DTM + placing buildings on graded pads...")
    grid_z, affine = build_domain_dtm((xmin, ymin, xmax, ymax), step=step)
    dtm = DtmSampler(grid_z, affine)
    bld_v, bld_f, footprints = place_on_terrain(pieces, dtm)
    grid_pad = flatten_pads(grid_z, affine, footprints)
    terr_v, terr_f = terrain_surface_mesh(grid_pad, affine)
    log(f"[terrain] DTM {grid_z.shape} elev {np.nanmin(grid_z):.0f}-{np.nanmax(grid_z):.0f}m, "
        f"{len(footprints)} buildings placed")

    obj_path = workdir / "scene.obj"
    _write_obj(obj_path, terr_v, terr_f, bld_v, bld_f)
    log(f"[obj] wrote {obj_path} ({obj_path.stat().st_size / 1e6:.0f} MB)")

    raw_stl = workdir / "fused.stl"
    log(f"[blender] fusing (solidify + join + voxel remesh {voxel_size}m)...")
    subprocess.run(
        [str(BLENDER_PATH), "--background", "--python", str(_FUSE_SCRIPT), "--",
         "--input", str(obj_path), "--output", str(raw_stl),
         "--solidify", str(SOLIDIFY_M), "--voxel-size", str(voxel_size)],
        check=True,
    )

    # Clip to the EXACT domain box with clean vertical walls -- kills the ragged
    # "teeth" where sloped terrain met the oversized (margin) grid edge, and
    # gives the CFD domain planar inlet/outlet/lateral faces. cap=True seals each
    # cut; pymeshfix (next) tidies any residual non-manifold edges at the caps.
    clipped_stl = workdir / "clipped.stl"
    log(f"[clip] trimming to exact domain box with vertical walls...")
    import trimesh
    m = trimesh.load(str(raw_stl))
    for origin, normal in [((xmin, 0, 0), (1, 0, 0)), ((xmax, 0, 0), (-1, 0, 0)),
                           ((0, ymin, 0), (0, 1, 0)), ((0, ymax, 0), (0, -1, 0))]:
        m = m.slice_plane(plane_origin=origin, plane_normal=normal, cap=True)
    m.export(str(clipped_stl))

    log(f"[repair] decimate (fast_simplification) + pymeshfix...")
    verify_and_repair(str(clipped_stl), str(out_stl), target_reduction=target_reduction, log_fn=log)
    log(f"[done] {out_stl}")
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
    ap.add_argument("--target-reduction", type=float, default=0.85,
                    help="fraction of faces removed in decimation (0.85 default; "
                         "0.9-0.925 is safe for CFD -- flow sees frontal area, not roof detail)")
    ap.add_argument("--workers", type=int, default=6, help="parallel tile-decode workers")
    ap.add_argument("--workdir", default=None)
    args = ap.parse_args()

    domain = load_domain_polygon(bbox=args.bbox, domain_geojson=args.domain_geojson,
                                 domain_crs=args.domain_crs)
    build_domain_stl(domain, args.output, step=args.step, voxel_size=args.voxel_size,
                     target_reduction=args.target_reduction, workers=args.workers,
                     workdir=args.workdir)


if __name__ == "__main__":
    main()
