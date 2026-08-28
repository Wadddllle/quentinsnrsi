"""Bisect the REAL input: how do the 798 NM edges scale with piece count?
linear   -> per-piece / per-contact effect, go find the responsible pieces
sudden   -> a threshold, and the jump point says where to look
"""
import sys, os; sys.path.insert(0, "/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np, trimesh, io, contextlib, time
import meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
import wildmeshing as wm
from shapely.geometry import box
from pyproj import Transformer
from sbg.onemap_native.tiles import domain_leaf_tiles
from sbg.onemap_native.extract import extract_domain_buildings, seal_piece

B = (28941, 28758, 29341, 29158)
EPS = float(os.environ.get("EPS", "0.10"))
NS = [int(x) for x in os.environ.get("NS", "10,25,50,100,200,287").split(",")]


def strict(v, f):
    v = np.asarray(v, np.float32)
    uq, inv = np.unique(v.view([('', np.float32)] * 3).ravel(), return_inverse=True)
    i = inv.astype(np.int64)[np.asarray(f).astype(np.int64)]
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
    u = np.unique(bf); rm = np.full(len(tv), -1, np.int64); rm[u] = np.arange(len(u))
    return tv[u], rm[bf]


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
print(f"{len(S)} sealed pieces, eps={EPS}\n", flush=True)
print(f"{'N':>5} {'inFaces':>8} {'outFaces':>9} {'comp':>5} {'open':>5} {'NM':>5} "
      f"{'wind':>6} {'selfX':>6} {'NM/piece':>9} {'t':>7}", flush=True)

for N in NS:
    sub = S[:N]
    V, F, off = [], [], 0
    for v, f in sub:
        V.append(v); F.append(f + off); off += len(v)
    V = np.vstack(V); F = np.vstack(F).astype(np.int32); V = V - V.mean(axis=0)
    diag = float(np.linalg.norm(V.max(axis=0) - V.min(axis=0)))
    t0 = time.time()
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            t = wm.Tetrahedralizer(epsilon=EPS / diag, edge_length_r=0.05,
                                   coarsen=True, max_its=0, stop_quality=10)
            t.set_mesh(V, F)
            t.tetrahedralize()
            o = t.get_tet_mesh(floodfill=True, manifold_surface=True,
                               correct_surface_orientation=True)
        bv, bf = boundary(np.asarray(o[0], float), np.asarray(o[1]))
        op, nm, wd = strict(bv, bf)
        ml = mn.meshFromFacesVerts(np.asarray(bf, np.int32), np.asarray(bv, float))
        sx = mr.findSelfCollidingTriangles(mr.MeshPart(ml)).size()
        nc = mr.MeshComponents.getAllComponents(ml).size()
        print(f"{N:5} {len(F):8,} {len(bf):9,} {nc:5} {op:5} {nm:5} {wd:6} {sx:6} "
              f"{nm/N:9.2f} {time.time()-t0:6.1f}s", flush=True)
    except Exception as e:
        print(f"{N:5}  FAILED {type(e).__name__}: {str(e)[:40]}", flush=True)
