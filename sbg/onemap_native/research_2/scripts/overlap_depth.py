"""How deeply do adjacent OneMap buildings INTERPENETRATE?

Sets the floor for any per-piece inset (must exceed the penetration to separate
them) and the floor for any dilation (must exceed the GAP to fuse them). Both
knobs are sized from this, not guessed.

Depth = for each intersecting pair, how far A's vertices lie inside B.
"""
import sys, os; sys.path.insert(0, "/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np, trimesh, time
import meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
from shapely.geometry import box
from shapely.strtree import STRtree
from pyproj import Transformer
from sbg.onemap_native.tiles import domain_leaf_tiles
from sbg.onemap_native.extract import extract_domain_buildings, seal_piece

B = (28941, 28758, 29341, 29158)

tr = Transformer.from_crs("EPSG:3414", "EPSG:4326", always_xy=True)
lo, la = tr.transform([B[0], B[2]], [B[1], B[3]])
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
print(f"{len(S)} sealed pieces")

ml = [mn.meshFromFacesVerts(np.asarray(f, np.int32), np.asarray(v, float)) for v, f in S]
boxes = [box(v[:, 0].min(), v[:, 1].min(), v[:, 0].max(), v[:, 1].max()) for v, _ in S]
tree = STRtree(boxes)

t0 = time.time()
pairs, depths, gaps = [], [], []
for i, bx in enumerate(boxes):
    for j in tree.query(bx):
        j = int(j)
        if j <= i:
            continue
        zi = (S[i][0][:, 2].min(), S[i][0][:, 2].max())
        zj = (S[j][0][:, 2].min(), S[j][0][:, 2].max())
        if zi[1] < zj[0] or zj[1] < zi[0]:
            continue
        hit = mr.findCollidingTriangles(mr.MeshPart(ml[i]), mr.MeshPart(ml[j])).size() > 0
        # signed distance of A's verts wrt B: negative = inside B
        tb = trimesh.Trimesh(S[j][0], S[j][1], process=False)
        d = trimesh.proximity.ProximityQuery(tb).signed_distance(S[i][0])
        if hit:
            pen = float(d.max()) if d.size else 0.0   # deepest penetration into B
            pairs.append((i, j)); depths.append(max(pen, 0.0))
        else:
            g = float(-d.max()) if d.size else np.inf  # closest approach
            if np.isfinite(g) and g < 2.0:
                gaps.append(g)

depths = np.array(depths); gaps = np.array(gaps)
print(f"[{time.time()-t0:.0f}s] {len(depths)} INTERSECTING pairs, "
      f"{len(gaps)} near-miss pairs (<2m apart)\n")
if len(depths):
    print("PENETRATION depth of intersecting pairs (metres) "
          "-- an inset must EXCEED this to separate them:")
    for q in (50, 75, 90, 95, 99, 100):
        print(f"   p{q:<3} = {np.percentile(depths, q):7.3f}")
    print(f"   mean = {depths.mean():7.3f}")
    for thr in (0.03, 0.05, 0.15, 0.5, 1.0):
        print(f"   pairs penetrating deeper than {thr:4.2f}m: "
              f"{int((depths>thr).sum()):4d} / {len(depths)}  "
              f"({(depths>thr).mean():5.1%})")
if len(gaps):
    print("\nGAP of non-intersecting close pairs (metres) "
          "-- a dilation must EXCEED this to fuse them:")
    for q in (10, 25, 50, 75, 90):
        print(f"   p{q:<3} = {np.percentile(gaps, q):7.3f}")
    for thr in (0.05, 0.1, 0.3):
        print(f"   pairs closer than {thr:4.2f}m: {int((gaps<thr).sum()):4d} / {len(gaps)}")
