"""For every real building piece: weld by position, then for every internally
shared edge (used by exactly 2 faces) check whether the two faces traverse it
in OPPOSITE order (consistent orientation, as a real manifold surface should
have) or the SAME order (a winding flip -- one triangle's normal points the
wrong way relative to its neighbor). Report the fraction of same-direction
("flipped") shared edges per piece."""
import sys
sys.path.insert(0, "/home/quentin/snrsi")
import numpy as np
from collections import defaultdict
from pyproj import Transformer

from sbg.onemap_native.tiles import domain_leaf_tiles, fetch_tile, feature_table, load_gltf
from sbg.onemap_native.extract import _draco_prims, FLAT_PLATE_Z
from sbg.onemap_native.transform import local_to_svy21

svy_to_wgs = Transformer.from_crs("EPSG:3414", "EPSG:4326", always_xy=True)
xmin, ymin, xmax, ymax = 22000, 30550, 22280, 30820
lon_min, lat_min = svy_to_wgs.transform(xmin, ymin)
lon_max, lat_max = svy_to_wgs.transform(xmax, ymax)
leaves = domain_leaf_tiles(lon_min, lat_min, lon_max, lat_max)

WELD = 3

def weld(v, f):
    key = np.round(v, WELD)
    _, inv = np.unique(key, axis=0, return_inverse=True)
    inv = inv.reshape(-1)
    v_w = np.zeros((inv.max() + 1, 3))
    v_w[inv] = v
    f_w = inv[f]
    keep = (f_w[:,0] != f_w[:,1]) & (f_w[:,1] != f_w[:,2]) & (f_w[:,0] != f_w[:,2])
    return v_w, f_w[keep]

def winding_stats(f):
    directed = defaultdict(int)   # directed edge -> count
    for tri in f:
        for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
            directed[(a, b)] += 1
    shared_consistent = 0
    shared_flipped = 0
    for (a, b), cnt in directed.items():
        if a > b:
            continue  # count each undirected pair once
        fwd = directed.get((a, b), 0)
        rev = directed.get((b, a), 0)
        if fwd >= 1 and rev >= 1:
            shared_consistent += min(fwd, rev)
        if fwd >= 2 or rev >= 2:
            # same-direction duplicate uses of one directed edge = flipped pair(s)
            shared_flipped += (fwd - 1 if fwd >= 2 else 0) + (rev - 1 if rev >= 2 else 0)
    return shared_consistent, shared_flipped

results = []
for uri in leaves:
    try:
        data = fetch_tile(uri)
        ft = feature_table(data)
        rtc = ft.get("RTC_CENTER")
        if rtc is None:
            continue
        rtc = np.array(rtc)
        gltf, blob = load_gltf(data)
    except Exception as e:
        continue
    for pts, faces, bid, T, R in _draco_prims(gltf, blob):
        svy = local_to_svy21(pts, T, R, rtc)
        for c in np.unique(bid):
            fmask = np.all(bid[faces] == c, axis=1)
            if not np.any(fmask):
                continue
            sub_faces = faces[fmask]
            used = np.unique(sub_faces)
            v = svy[used]
            if np.ptp(v[:, 2]) < FLAT_PLATE_Z:
                continue
            remap = np.zeros(len(svy), dtype=np.int64)
            remap[used] = np.arange(len(used))
            f_local = remap[sub_faces]
            v_w, f_w = weld(v, f_local)
            consistent, flipped = winding_stats(f_w)
            total = consistent + flipped
            frac = flipped / total if total else 0.0
            results.append((uri.split("/")[-1], c, len(f_w), consistent, flipped, frac,
                             v_w[:,2].min(), v_w[:,2].max()))

results.sort(key=lambda r: -r[5])
print(f"{len(results)} building pieces checked in this domain\n")
print("Top 20 by fraction of flipped-winding shared edges (most likely to erode):")
for uri, c, nf, cons, flip, frac, zmin, zmax in results[:20]:
    print(f"  {frac*100:5.1f}% flipped  ({flip:5d}/{cons+flip:5d} shared edges)  "
          f"faces={nf:6d}  z=[{zmin:.1f},{zmax:.1f}]  tile={uri} batch={c}")

import numpy as _np
fracs = _np.array([r[5] for r in results])
print(f"\nOverall: mean flipped-fraction={fracs.mean()*100:.2f}%, median={_np.median(fracs)*100:.2f}%, "
      f"buildings with >5% flipped edges: {(fracs>0.05).sum()}/{len(fracs)}, "
      f">20%: {(fracs>0.20).sum()}/{len(fracs)}")
