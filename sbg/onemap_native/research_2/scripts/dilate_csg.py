"""Dilate each piece FIRST, then union -- both via soup+floodfill and via CSG.

The pinches come from tangential contact. If each piece is dilated so neighbours
genuinely OVERLAP, there is no tangency for the union to resolve. CSG may benefit
more than soup here because it tracks the individual solids instead of inferring
membership from one merged winding number.

Dilation is a TRUE per-vertex normal offset (not a bbox scale -- see 9.6).
ms=False throughout, since manifold_surface doubles the defect for our path.
"""
import sys, os; sys.path.insert(0, "/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np, trimesh, io, contextlib, time, json
import meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
import wildmeshing as wm
from shapely.geometry import box
from pyproj import Transformer
from sbg.onemap_native.tiles import domain_leaf_tiles
from sbg.onemap_native.extract import extract_domain_buildings, seal_piece

B = (28941, 28758, 29341, 29158)
EPS = float(os.environ.get("EPS", "0.10"))
NP = int(os.environ.get("NP", "10"))
DILS = [float(x) for x in os.environ.get("DILS", "0,0.02,0.05,0.10").split(",")]


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


def dilate(v, f, d):
    if d <= 0:
        return v
    fn = np.cross(v[f[:, 1]] - v[f[:, 0]], v[f[:, 2]] - v[f[:, 0]])
    acc = np.zeros_like(v)
    for k in range(3):
        for c in range(3):
            acc[:, c] += np.bincount(f[:, k], weights=fn[:, c], minlength=len(v))
    ln = np.linalg.norm(acc, axis=1, keepdims=True); ln[ln == 0] = 1.0
    return v + (acc / ln) * d


def union_tree(ids):
    if len(ids) == 1:
        return ids[0]
    m = len(ids) // 2
    return {"operation": "union", "left": union_tree(ids[:m]), "right": union_tree(ids[m:])}


def report(tag, tv, tt, t):
    bv, bf = boundary(np.asarray(tv, float), np.asarray(tt))
    op, nm, wd = strict(bv, bf)
    ml = mn.meshFromFacesVerts(np.asarray(bf, np.int32), np.asarray(bv, float))
    sx = mr.findSelfCollidingTriangles(mr.MeshPart(ml)).size()
    nc = mr.MeshComponents.getAllComponents(ml).size()
    vol = trimesh.Trimesh(bv, bf, process=False).volume
    flag = "  <<< 0/0/0/0" if (op == 0 and nm == 0 and wd == 0 and sx == 0) else ""
    print(f"{tag:30s} comp={nc:4} open={op:4} NM={nm:5} wind={wd:6} selfX={sx:5} "
          f"vol={vol:10,.0f} {t:5.1f}s{flag}", flush=True)


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
print(f"{len(S)} pieces, eps={EPS}, ms=False throughout\n")

for D in DILS:
    Sd = [(np.ascontiguousarray(dilate(v, f, D)),
           np.ascontiguousarray(f.astype(np.int32))) for v, f in S]
    V, F, off = [], [], 0
    for v, f in Sd:
        V.append(v); F.append(f + off); off += len(v)
    V = np.vstack(V); F = np.vstack(F).astype(np.int32)
    diag = float(np.linalg.norm(V.max(axis=0) - V.min(axis=0)))
    # soup
    t0 = time.time()
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            t = wm.Tetrahedralizer(epsilon=EPS / diag, edge_length_r=0.05,
                                   coarsen=True, max_its=0, stop_quality=10)
            t.set_mesh(V, F); t.tetrahedralize()
            o = t.get_tet_mesh(floodfill=True, manifold_surface=False,
                               correct_surface_orientation=True)
        report(f"dil {D*100:4.0f}cm  SOUP", o[0], o[1], time.time() - t0)
    except Exception as e:
        print(f"dil {D*100:4.0f}cm  SOUP  FAILED {type(e).__name__}", flush=True)
    # csg
    t0 = time.time()
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            t2 = wm.Tetrahedralizer(epsilon=EPS / diag, edge_length_r=0.05,
                                    coarsen=True, max_its=0, stop_quality=10)
            t2.set_meshes([v for v, _ in Sd], [f for _, f in Sd])
            t2.tetrahedralize()
            o2 = t2.get_tet_mesh_from_csg(json.dumps(union_tree(list(range(len(Sd))))),
                                          manifold_surface=False,
                                          correct_surface_orientation=True)
        report(f"dil {D*100:4.0f}cm  CSG ", o2[0], o2[1], time.time() - t0)
    except Exception as e:
        print(f"dil {D*100:4.0f}cm  CSG   FAILED {type(e).__name__}", flush=True)
    print()
