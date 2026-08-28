"""SPLIT + NUDGE.

manifold_surface=True splits the pinch vertex into one copy per lobe -- exactly
what is needed -- but leaves the copies at IDENTICAL coordinates, so a positional
weld (which STL forces) merges them straight back into a 4-face edge.

So: keep the split, then move each copy a microscopic distance INTO ITS OWN LOBE
along that copy's own inward normal. The copies are then distinct positions and
the weld cannot merge them.

Why this may work where the earlier nudge failed: that test was on VOXEL output,
where a pinch is a knife edge with no thickness to push into (selfX 0->51). Here a
lobe is a whole building -- metres of solid.

fTetWild is NON-DETERMINISTIC, so every config is run TRIALS times and reported as
a distribution, not a single number.
"""
import sys, os; sys.path.insert(0, "/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np, io, contextlib, time
import meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
import wildmeshing as wm
from shapely.geometry import box
from pyproj import Transformer
from sbg.onemap_native.tiles import domain_leaf_tiles
from sbg.onemap_native.extract import extract_domain_buildings, seal_piece

B = (28941, 28758, 29341, 29158)
EPS = float(os.environ.get("EPS", "0.10"))
NP = int(os.environ.get("NP", "10"))
TRIALS = int(os.environ.get("TRIALS", "5"))
NUDGE = float(os.environ.get("NUDGE", "0.001"))      # 1 mm


def strict(v, f):
    v = np.ascontiguousarray(np.asarray(v, np.float32))
    uq, inv = np.unique(v.view([('', np.float32)] * 3).ravel(), return_inverse=True)
    i = inv.astype(np.int64)[np.ascontiguousarray(f).astype(np.int64)]
    i = i[(i[:, 0] != i[:, 1]) & (i[:, 1] != i[:, 2]) & (i[:, 0] != i[:, 2])]
    n = np.int64(len(uq))
    d = np.vstack([i[:, [0, 1]], i[:, [1, 2]], i[:, [2, 0]]])
    a, b = d[:, 0], d[:, 1]
    _, c = np.unique(np.minimum(a, b) * n + np.maximum(a, b), return_counts=True)
    _, dd = np.unique(a * n + b, return_counts=True)
    return int((c == 1).sum()), int((c > 2).sum()), int((dd - 1)[dd > 1].sum())


def boundary(tv, tt):
    q = np.vstack([tt[:, [0, 1, 2]], tt[:, [0, 1, 3]], tt[:, [0, 2, 3]], tt[:, [1, 2, 3]]])
    opp = np.concatenate([tt[:, 3], tt[:, 2], tt[:, 1], tt[:, 0]])
    _, idx, cnt = np.unique(np.sort(q, axis=1), axis=0, return_index=True, return_counts=True)
    k = idx[cnt == 1]; bf = q[k]
    a_, b_, c_ = tv[bf[:, 0]], tv[bf[:, 1]], tv[bf[:, 2]]
    fl = np.einsum('ij,ij->i', np.cross(b_ - a_, c_ - a_), tv[opp[k]] - a_) > 0
    bf[fl] = bf[fl][:, [0, 2, 1]]
    used = np.unique(bf)
    rm = np.full(len(tv), -1, np.int64); rm[used] = np.arange(len(used))
    return tv[used], rm[bf]


def nudge_split_verts(v, f, delta):
    """Move each copy of a coincident vertex INTO ITS OWN LOBE, along that copy's
    own inward normal (-outward). Only coincident vertices move."""
    v = np.asarray(v, float).copy()
    f = np.asarray(f)
    key = np.ascontiguousarray(v.astype(np.float32)).view([('', np.float32)] * 3).ravel()
    uq, inv, cts = np.unique(key, return_inverse=True, return_counts=True)
    dup = np.where(cts > 1)[0]
    if len(dup) == 0:
        return v, 0
    moving = np.isin(inv, dup)
    fn = np.cross(v[f[:, 1]] - v[f[:, 0]], v[f[:, 2]] - v[f[:, 0]])
    acc = np.zeros_like(v)
    for k in range(3):
        for c in range(3):
            acc[:, c] += np.bincount(f[:, k], weights=fn[:, c], minlength=len(v))
    ln = np.linalg.norm(acc, axis=1, keepdims=True); ln[ln == 0] = 1.0
    v[moving] -= (acc[moving] / ln[moving]) * delta          # inward
    return v, int(moving.sum())


tr = Transformer.from_crs("EPSG:3414", "EPSG:4326", always_xy=True)
lo, la = tr.transform([B[0], B[2]], [B[1], B[3]])
P = extract_domain_buildings(domain_leaf_tiles(min(lo), min(la), max(lo), max(la)),
                             box(*B), store_dir="data/onemap_store")
S = []
for p in P:
    try:
        sv, sf = seal_piece(p["verts"], p["faces"])
    except Exception:
        continue
    sv = np.asarray(sv, float); sf = np.asarray(sf)
    if len(sf) >= 4:
        S.append((sv, sf))
S = S[:NP]
ctr = np.vstack([v for v, _ in S]).mean(axis=0)
S = [(v - ctr, f) for v, f in S]
V, F, off = [], [], 0
for v, f in S:
    V.append(v); F.append(f + off); off += len(v)
V = np.vstack(V); F = np.vstack(F).astype(np.int32)
diag = float(np.linalg.norm(V.max(axis=0) - V.min(axis=0)))
print(f"{NP} real pieces, {len(F):,} faces, eps={EPS}, nudge={NUDGE*1000:.1f}mm, "
      f"{TRIALS} trials/config\n")

res = {k: [] for k in ("ms=True", "ms=False", "ms=True+nudge")}
sx_ = {k: [] for k in res}
for _ in range(TRIALS):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        t = wm.Tetrahedralizer(epsilon=EPS / diag, edge_length_r=0.05, coarsen=True,
                               max_its=0, stop_quality=10)
        t.set_mesh(V, F)
        t.tetrahedralize()
        oT = t.get_tet_mesh(floodfill=True, manifold_surface=True,
                            correct_surface_orientation=True)
        oF = t.get_tet_mesh(floodfill=True, manifold_surface=False,
                            correct_surface_orientation=True)
    for tag, o in (("ms=True", oT), ("ms=False", oF)):
        bv, bf = boundary(np.asarray(o[0], float), np.asarray(o[1]))
        op, nm, wd = strict(bv, bf)
        ml = mn.meshFromFacesVerts(np.asarray(bf, np.int32), np.asarray(bv, float))
        res[tag].append((op, nm, wd))
        sx_[tag].append(mr.findSelfCollidingTriangles(mr.MeshPart(ml)).size())
    # split + nudge, on the manifold_surface=True output
    bv, bf = boundary(np.asarray(oT[0], float), np.asarray(oT[1]))
    nv, nmoved = nudge_split_verts(bv, bf, NUDGE)
    op, nm, wd = strict(nv, bf)
    ml = mn.meshFromFacesVerts(np.asarray(bf, np.int32), np.asarray(nv, float))
    res["ms=True+nudge"].append((op, nm, wd))
    sx_["ms=True+nudge"].append(mr.findSelfCollidingTriangles(mr.MeshPart(ml)).size())

print(f"{'config':16s} {'open':>18} {'NM':>18} {'winding':>18} {'selfX':>16}")
for k in ("ms=True", "ms=False", "ms=True+nudge"):
    a = np.array(res[k]); s = np.array(sx_[k])
    print(f"{k:16s} {str(list(a[:,0])):>18} {str(list(a[:,1])):>18} "
          f"{str(list(a[:,2])):>18} {str(list(s)):>16}")
print(f"\n  means: " + "   ".join(
    f"{k} NM={np.mean([x[1] for x in res[k]]):.1f} selfX={np.mean(sx_[k]):.1f}"
    for k in res))
print(f"  ({nmoved} coincident vertices nudged in the last trial)")
