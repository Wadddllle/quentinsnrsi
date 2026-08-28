"""Causal test: are the pinches INTRA-piece or INTER-piece?

Run the worst pinch-owning pieces through fTetWild ALONE. Neighbours absent.
  pinches still appear -> intra-piece (one _BATCHID holds several abutting
                          shophouses, so their shared walls are inside one mesh
                          and no per-piece inset can separate them)
  pinches vanish       -> inter-piece contact
Also reports edge ORIENTATION: a vertical bad edge is two walls meeting along a
corner; a horizontal one is a wall meeting a slab.
"""
import sys, os; sys.path.insert(0, "/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np, trimesh, io, contextlib, time
from scipy.spatial import cKDTree
from shapely.geometry import box
from pyproj import Transformer
import wildmeshing as wm
from sbg.onemap_native.tiles import domain_leaf_tiles
from sbg.onemap_native.extract import extract_domain_buildings, seal_piece

B = (28941, 28758, 29341, 29158)
STL = "data/wt_raw_test/duxton_ftetwild_domain.stl"
EPS_ABS = 0.05


def boundary(tv, tt):
    q = np.vstack([tt[:, [0, 1, 2]], tt[:, [0, 1, 3]], tt[:, [0, 2, 3]], tt[:, [1, 2, 3]]])
    opp = np.concatenate([tt[:, 3], tt[:, 2], tt[:, 1], tt[:, 0]])
    _, idx, cnt = np.unique(np.sort(q, axis=1), axis=0, return_index=True, return_counts=True)
    keep = idx[cnt == 1]; bf = q[keep]
    a_, b_, c_ = tv[bf[:, 0]], tv[bf[:, 1]], tv[bf[:, 2]]
    flip = np.einsum('ij,ij->i', np.cross(b_ - a_, c_ - a_), tv[opp[keep]] - a_) > 0
    bf[flip] = bf[flip][:, [0, 2, 1]]
    used = np.unique(bf)
    rm = np.full(len(tv), -1, np.int64); rm[used] = np.arange(len(used))
    return tv[used], rm[bf]


def bad_edges(v, f):
    v = np.asarray(v, np.float32)
    uq, inv = np.unique(v.view([('', np.float32)] * 3).ravel(), return_inverse=True)
    va = uq.view(np.float32).reshape(-1, 3)
    n = np.int64(len(uq)); i = inv.astype(np.int64)[np.asarray(f).astype(np.int64)]
    de = np.vstack([i[:, [0, 1]], i[:, [1, 2]], i[:, [2, 0]]])
    a, b = de[:, 0], de[:, 1]
    key = np.minimum(a, b) * n + np.maximum(a, b)
    u, c = np.unique(key, return_counts=True)
    bad = u[c > 2]
    return bad, va, n


def ftw(v, f):
    v = np.asarray(v, float); f = np.asarray(f, np.int32)
    diag = float(np.linalg.norm(v.max(axis=0) - v.min(axis=0)))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        t = wm.Tetrahedralizer(epsilon=EPS_ABS / diag, edge_length_r=0.05,
                               coarsen=True, max_its=0, stop_quality=10)
        t.set_mesh(v, f)
        t.tetrahedralize()
        o = t.get_tet_mesh(floodfill=True, manifold_surface=True,
                           correct_surface_orientation=True)
    return boundary(np.asarray(o[0], float), np.asarray(o[1]))


# ---------- edge orientation on the shipped domain output
m = trimesh.load(STL, process=False)
bad, va, n = bad_edges(m.vertices, m.faces)
p0 = va[(bad // n).astype(np.int64)]; p1 = va[(bad % n).astype(np.int64)]
d = p1 - p0
L = np.linalg.norm(d, axis=1)
vert_frac = np.abs(d[:, 2]) / np.maximum(L, 1e-9)
print(f"{len(bad)} bad edges — orientation (1.0 = vertical, 0.0 = horizontal)")
print(f"   median={np.median(vert_frac):.3f}  "
      f"vertical(>0.9): {float((vert_frac>0.9).mean()):.1%}   "
      f"horizontal(<0.1): {float((vert_frac<0.1).mean()):.1%}\n")

# ---------- attribute pinches to pieces
tr = Transformer.from_crs("EPSG:3414", "EPSG:4326", always_xy=True)
lo, la = tr.transform([B[0], B[2]], [B[1], B[3]])
pieces = extract_domain_buildings(domain_leaf_tiles(min(lo), min(la), max(lo), max(la)),
                                  box(*B), store_dir="data/onemap_store")
SV, lab, keepidx = [], [], []
for i, p in enumerate(pieces):
    try:
        sv, sf = seal_piece(p["verts"], p["faces"])
    except Exception:
        sv, sf = p["verts"], p["faces"]
    sv = np.asarray(sv, float); sf = np.asarray(sf)
    if len(sf) == 0:
        continue
    SV.append((sv, sf)); lab.append(np.full(len(sv), len(keepidx))); keepidx.append(i)
allv = np.vstack([s[0] for s in SV]); lab = np.concatenate(lab)
ctr = allv.mean(axis=0)
tree = cKDTree(allv - ctr)
mid = 0.5 * (p0 + p1)
_, near = tree.query(mid, k=1)
cnt = np.bincount(lab[near], minlength=len(SV))
top = np.argsort(-cnt)[:6]

print(f"{'piece':>6} {'faces':>7} {'pinches in':>11} | ISOLATED fTetWild: "
      f"{'faces':>7} {'open':>5} {'NM':>5} {'wind':>6} {'selfX':>6}  {'t':>6}")
for k in top:
    sv, sf = SV[k]
    t0 = time.time()
    try:
        bv, bf = ftw(sv, sf)
        b2, _, _ = bad_edges(bv, bf)
        v2 = np.asarray(bv, np.float32)
        uq2, inv2 = np.unique(v2.view([('', np.float32)] * 3).ravel(), return_inverse=True)
        n2 = np.int64(len(uq2)); i2 = inv2.astype(np.int64)[np.asarray(bf).astype(np.int64)]
        de2 = np.vstack([i2[:, [0, 1]], i2[:, [1, 2]], i2[:, [2, 0]]])
        a2, b2_ = de2[:, 0], de2[:, 1]
        _, c2 = np.unique(np.minimum(a2, b2_) * n2 + np.maximum(a2, b2_), return_counts=True)
        _, d2 = np.unique(a2 * n2 + b2_, return_counts=True)
        import meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
        ml = mn.meshFromFacesVerts(np.asarray(bf, np.int32), np.asarray(bv, float))
        sx = mr.findSelfCollidingTriangles(mr.MeshPart(ml)).size()
        print(f"{keepidx[k]:6} {len(sf):7,} {cnt[k]:11} | "
              f"{len(bf):24,} {int((c2==1).sum()):5} {int((c2>2).sum()):5} "
              f"{int((d2-1)[d2>1].sum()):6} {sx:6,}  {time.time()-t0:5.1f}s", flush=True)
    except Exception as e:
        print(f"{keepidx[k]:6} {len(sf):7,} {cnt[k]:11} | FAILED {type(e).__name__}", flush=True)
