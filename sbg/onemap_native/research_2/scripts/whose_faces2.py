"""Proper attribution: nearest SURFACE, not nearest vertex.

OneMap triangles are 7-12 m per edge, so nearest-vertex attribution is useless
(median error 3.3 m). Point-to-triangle distance against each candidate piece is
the right query. Sampled, because it is much more expensive.
"""
import sys, os; sys.path.insert(0, "/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np, trimesh
from shapely.geometry import box
from shapely.strtree import STRtree
from pyproj import Transformer
from sbg.onemap_native.tiles import domain_leaf_tiles
from sbg.onemap_native.extract import extract_domain_buildings, seal_piece

B = (28941, 28758, 29341, 29158)
STL = os.environ.get("STL", "data/wt_raw_test/duxton_ftetwild_domain.stl")
NS = int(os.environ.get("NS", "120"))

tr = Transformer.from_crs("EPSG:3414", "EPSG:4326", always_xy=True)
lo, la = tr.transform([B[0], B[2]], [B[1], B[3]])
pieces = extract_domain_buildings(domain_leaf_tiles(min(lo), min(la), max(lo), max(la)),
                                  box(*B), store_dir="data/onemap_store")
P, allv = [], []
for i, p in enumerate(pieces):
    try:
        sv, sf = seal_piece(p["verts"], p["faces"])
    except Exception:
        sv, sf = p["verts"], p["faces"]
    sv = np.asarray(sv, float); sf = np.asarray(sf)
    if len(sf) == 0:
        continue
    P.append((i, sv, sf)); allv.append(sv)
ctr = np.vstack(allv).mean(axis=0)
P = [(i, sv - ctr, sf) for i, sv, sf in P]              # match ftw_domain recentring
tm_of = {i: trimesh.Trimesh(sv, sf, process=False) for i, sv, sf in P}
boxes = [box(sv[:, 0].min(), sv[:, 1].min(), sv[:, 0].max(), sv[:, 1].max())
         for _, sv, _ in P]
ids = [i for i, _, _ in P]
tree = STRtree(boxes)

m = trimesh.load(STL, process=False)
v = np.asarray(m.vertices, np.float32); f = np.asarray(m.faces).astype(np.int64)
uq, inv = np.unique(v.view([('', np.float32)] * 3).ravel(), return_inverse=True)
n = np.int64(len(uq)); idx = inv.astype(np.int64)[f]
de = np.vstack([idx[:, [0, 1]], idx[:, [1, 2]], idx[:, [2, 0]]])
fid = np.tile(np.arange(len(f)), 3)
a, b = de[:, 0], de[:, 1]
key = np.minimum(a, b) * n + np.maximum(a, b)
o = np.argsort(key); key_s, fid_s = key[o], fid[o]
uk, start, cnt = np.unique(key_s, return_index=True, return_counts=True)
bad = np.where(cnt > 2)[0]
rng = np.random.default_rng(0)
sample = rng.choice(bad, min(NS, len(bad)), replace=False)
cent = m.triangles_center
print(f"{len(bad)} non-manifold edges; attributing {len(sample)} by nearest SURFACE\n")

nsame = ndiff = nfar = 0
errs = []
for e in sample:
    fs = fid_s[start[e]:start[e] + cnt[e]]
    owners, dists = [], []
    for c in cent[fs]:
        cand = [ids[int(k)] for k in tree.query(box(c[0] - 1, c[1] - 1, c[0] + 1, c[1] + 1))]
        if not cand:
            owners.append(-1); dists.append(np.inf); continue
        best, bd = -1, np.inf
        for pid in cand:
            d = abs(float(trimesh.proximity.ProximityQuery(tm_of[pid])
                          .signed_distance([c])[0]))
            if d < bd:
                bd, best = d, pid
        owners.append(best); dists.append(bd)
    errs.append(max(dists))
    if not np.isfinite(max(dists)) or max(dists) > 0.5:
        nfar += 1
    elif len(set(owners)) == 1:
        nsame += 1
    else:
        ndiff += 1

tot = nsame + ndiff
print(f"of {len(sample)} sampled edges ({nfar} unattributable, >0.5m from any piece):")
if tot:
    print(f"  all 4 faces on ONE building        : {nsame:4d} ({nsame/tot:5.1%})")
    print(f"  faces span TWO OR MORE buildings   : {ndiff:4d} ({ndiff/tot:5.1%})")
er = np.array([e for e in errs if np.isfinite(e)])
print(f"\nattribution error (face -> nearest piece SURFACE): "
      f"median={np.median(er):.3f}m p90={np.percentile(er,90):.3f}m")
