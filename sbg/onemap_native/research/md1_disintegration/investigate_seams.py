"""Investigate whether per-_BATCHID face splitting drops real geometry at seams
between adjacent buildings/roof pieces -- the hypothesis for the "hole visible
in our mesh, not visible on OneMap's own site" report.

For every leaf tile in the PGP test domain: decode every Draco/_BATCHID
primitive, and for every face check whether all 3 vertices share one batch id
(kept by our per-piece split) vs a mixed/seam face (dropped by EVERY piece,
since extract.py's fmask = np.all(bid[faces]==c, axis=1) requires unanimous
vertex batchid). Report the seam-face fraction, and separately look for the
biggest local cluster of seam faces (candidate real "hole" location).
"""
import sys
sys.path.insert(0, "/home/quentin/snrsi")
import numpy as np
from pyproj import Transformer

from sbg.onemap_native.tiles import domain_leaf_tiles, fetch_tile, feature_table, load_gltf
from sbg.onemap_native.extract import _draco_prims

svy_to_wgs = Transformer.from_crs("EPSG:3414", "EPSG:4326", always_xy=True)

# PGP-ish bbox from prior session context (SVY21)
xmin, ymin, xmax, ymax = 22000, 30550, 22280, 30820
lon_min, lat_min = svy_to_wgs.transform(xmin, ymin)
lon_max, lat_max = svy_to_wgs.transform(xmax, ymax)

leaves = domain_leaf_tiles(lon_min, lat_min, lon_max, lat_max)
print(f"{len(leaves)} leaf tiles in domain")

total_faces = 0
total_seam_faces = 0
per_tile_stats = []

for uri in leaves:
    try:
        data = fetch_tile(uri)
        ft = feature_table(data)
        rtc = ft.get("RTC_CENTER")
        if rtc is None:
            continue
        gltf, blob = load_gltf(data)
    except Exception as e:
        print(f"  skip {uri}: {e}")
        continue
    tile_faces = 0
    tile_seam = 0
    for pts, faces, bid, T, R in _draco_prims(gltf, blob):
        fb = bid[faces]  # (F,3)
        same = (fb[:, 0] == fb[:, 1]) & (fb[:, 1] == fb[:, 2])
        tile_faces += len(faces)
        tile_seam += int((~same).sum())
    total_faces += tile_faces
    total_seam_faces += tile_seam
    if tile_faces:
        per_tile_stats.append((uri, tile_faces, tile_seam, tile_seam / tile_faces))

print(f"\nTOTAL faces: {total_faces}, seam (mixed-batchid, dropped by every piece): {total_seam_faces} "
      f"({100*total_seam_faces/max(total_faces,1):.2f}%)")

per_tile_stats.sort(key=lambda t: -t[3])
print("\nTop 10 tiles by seam-face fraction:")
for uri, tf, ts, frac in per_tile_stats[:10]:
    print(f"  {frac*100:5.2f}%  ({ts:5d}/{tf:6d})  {uri}")
