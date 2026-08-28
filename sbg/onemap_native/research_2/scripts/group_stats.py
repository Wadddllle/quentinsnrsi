"""Grouping stats ONLY -- no meshing. Answers whether per-group fTetWild is viable
or whether one giant group swallows the domain and defeats the point.

Groups buildings whose surfaces come within T. Contacts must land INSIDE a group
(so fTetWild sees both sides); groups must be disjoint (so they can simply be
concatenated, no boolean union).
"""
import sys, os; sys.path.insert(0, "/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np, trimesh, time
import scipy.sparse as sp
from scipy.sparse.csgraph import connected_components
from shapely.geometry import box
from shapely.strtree import STRtree
from pyproj import Transformer
from sbg.onemap_native.tiles import domain_leaf_tiles
from sbg.onemap_native.extract import extract_domain_buildings, seal_piece

DOMS = {"duxton": (28941, 28758, 29341, 29158),
        "kentridge": (21950, 30250, 22850, 31150)}
DOM = os.environ.get("DOM", "duxton")
B = DOMS[DOM]
NPROC = 10

tr = Transformer.from_crs("EPSG:3414", "EPSG:4326", always_xy=True)
lo, la = tr.transform([B[0], B[2]], [B[1], B[3]])
t0 = time.time()
pieces = extract_domain_buildings(domain_leaf_tiles(min(lo), min(la), max(lo), max(la)),
                                  box(*B), store_dir="data/onemap_store")
S = []
for p in pieces:
    try:
        sv, sf = seal_piece(p["verts"], p["faces"])
    except Exception:
        continue
    sv = np.asarray(sv, float); sf = np.asarray(sf)
    if len(sf) >= 4:
        S.append((sv, sf))
nf = np.array([len(f) for _, f in S])
print(f"DOMAIN={DOM}  {len(S)} pieces, {nf.sum():,} faces  ({time.time()-t0:.1f}s)\n")

boxes = [box(v[:, 0].min(), v[:, 1].min(), v[:, 0].max(), v[:, 1].max()) for v, _ in S]
tree = STRtree(boxes)
prox = {}
t1 = time.time()
for i, bx in enumerate(boxes):
    for j in tree.query(bx.buffer(1.0)):
        j = int(j)
        if j <= i:
            continue
        zi = (S[i][0][:, 2].min(), S[i][0][:, 2].max())
        zj = (S[j][0][:, 2].min(), S[j][0][:, 2].max())
        if zi[1] + 1 < zj[0] or zj[1] + 1 < zi[0]:
            continue
        tb = trimesh.Trimesh(S[j][0], S[j][1], process=False)
        d = trimesh.proximity.ProximityQuery(tb).signed_distance(S[i][0])
        prox[(i, j)] = float(-d.max()) if d.size else np.inf   # <0 = overlapping
print(f"{len(prox)} candidate pairs evaluated ({time.time()-t1:.0f}s)\n")

# per-group fTetWild cost model, anchored on two real measurements:
#   502 faces -> 7.0 s (piece 26, its=0)   39,232 faces -> 239 s (whole domain)
# => t ~ a * faces^b
b = np.log(239 / 7.0) / np.log(39232 / 502)
a = 7.0 / 502 ** b
print(f"cost model from 2 real points:  t = {a:.4f} * faces^{b:.3f}   "
      f"(check: 502f->{a*502**b:.1f}s  39232f->{a*39232**b:.0f}s)\n")

print(f"{'T(m)':>6} {'groups':>7} {'max sz':>7} {'>10 mem':>8} {'max faces':>10} "
      f"{'serial':>8} {'on 10 cores':>12} {'critical path':>14}")
for T in (0.05, 0.10, 0.30, 1.00, 3.00):
    pairs = [(i, j) for (i, j), g in prox.items() if g < T]
    n = len(S)
    if pairs:
        g = sp.coo_matrix((np.ones(len(pairs)), tuple(zip(*pairs))), shape=(n, n))
        ncomp, lab = connected_components(g, directed=False)
    else:
        ncomp, lab = n, np.arange(n)
    sizes = np.bincount(lab, minlength=ncomp)
    gfaces = np.bincount(lab, weights=nf, minlength=ncomp)
    times = a * np.maximum(gfaces, 1) ** b
    serial = times.sum()
    # greedy longest-first bin packing onto NPROC cores
    load = np.zeros(NPROC)
    for t in sorted(times, reverse=True):
        load[np.argmin(load)] += t
    print(f"{T:6.2f} {ncomp:7} {sizes.max():7} {int((sizes>10).sum()):8} "
          f"{int(gfaces.max()):10,} {serial:7.0f}s {load.max():11.0f}s "
          f"{times.max():13.0f}s")

print("\n  serial        = sum of all group times (1 core)")
print("  on 10 cores   = greedy longest-first packing")
print("  critical path = the single biggest group; no parallelism beats this")
print(f"\n  for reference: whole-domain fTetWild measured 239s, "
      f"Duxton is {B[2]-B[0]:.0f}m")
