"""Run the REAL production pipeline (extract -> conforming terrain/skirts ->
PLY scene) for a domain covering both MD1 and the neighbor building, then hand
off to a debug Blender script that snapshots every stage (import, terrain
solidify, join, voxel remesh) instead of just the final result -- so we can
see exactly where in the real pipeline (not an isolated single-building test)
each building starts falling apart, or gets rescued."""
import sys
sys.path.insert(0, "/home/quentin/snrsi")
import numpy as np
from shapely.geometry import box

from sbg.onemap_native.extract import extract_domain_buildings
from sbg.onemap_native.terrain import build_domain_dtm, DtmSampler, place_on_terrain_conforming
from sbg.onemap_native.tiles import domain_leaf_tiles
from sbg.onemap_native.build import _write_scene_ply
from pyproj import Transformer

MD1 = np.array([22564.79267465497, 30648.979156176567])
NEIGHBOR = np.array([22448.07138926402, 30549.242487220705])

xmin, ymin, xmax, ymax = 22350, 30480, 22650, 30750
domain_polygon = box(xmin, ymin, xmax, ymax)

svy_to_wgs = Transformer.from_crs("EPSG:3414", "EPSG:4326", always_xy=True)
lon_min, lat_min = svy_to_wgs.transform(xmin, ymin)
lon_max, lat_max = svy_to_wgs.transform(xmax, ymax)
leaves = domain_leaf_tiles(lon_min, lat_min, lon_max, lat_max)
print(f"{len(leaves)} leaf tiles")

pieces = extract_domain_buildings(leaves, domain_polygon, workers=6)
print(f"{len(pieces)} pieces extracted")

grid_z, affine = build_domain_dtm((xmin, ymin, xmax, ymax), step=5.0)
dtm = DtmSampler(grid_z, affine)
bld_v, bld_f, terr_v, terr_f = place_on_terrain_conforming(pieces, dtm, domain_polygon)
print(f"bld_v={len(bld_v)} bld_f={len(bld_f)} terr_v={len(terr_v)} terr_f={len(terr_f)}")

out_dir = "/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583/scratchpad/trace"
import os
os.makedirs(out_dir, exist_ok=True)
_write_scene_ply(f"{out_dir}/terrain.ply", f"{out_dir}/buildings.ply", terr_v, terr_f, bld_v, bld_f)
print("wrote terrain.ply, buildings.ply")

# Which piece(s) correspond to MD1 and neighbor, by centroid distance?
for pc in pieces:
    v = pc["verts"]
    cen = v[:, :2].mean(axis=0)
    for name, target in [("MD1", MD1), ("NEIGHBOR", NEIGHBOR)]:
        d = np.linalg.norm(cen - target)
        if d < 20:
            print(f"  piece near {name}: dist={d:.1f} base_z={pc['base_z']:.1f} nfaces={len(pc['faces'])}")
