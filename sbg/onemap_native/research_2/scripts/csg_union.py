"""fTetWild's NATIVE CSG union instead of soup + floodfill.

Soup+floodfill infers inside/outside from a winding number over one merged
triangle set. The CSG path instead TRACKS which input surface bounds each tet and
evaluates a boolean expression per tet -- so the union is computed, not inferred.
That may resolve building-to-building contacts (the source of the pinches)
properly rather than producing a pinched solid.

set_meshes(V_list, F_list) + get_tet_mesh_from_csg(tree). No source build needed.
"""
import sys, os; sys.path.insert(0, "/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np, io, contextlib, time, json
import meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
import wildmeshing as wm
from shapely.geometry import box
from pyproj import Transformer
from sbg.onemap_native.tiles import domain_leaf_tiles
from sbg.onemap_native.extract import extract_domain_buildings, seal_piece

B = (28941, 28758, 29341, 29158)
EPS = float(os.environ.get("EPS", "0.10"))
NP = int(os.environ.get("NP", "10"))


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


def union_tree(ids):
    """balanced binary union tree over mesh indices"""
    if len(ids) == 1:
        return ids[0]
    m = len(ids) // 2
    return {"operation": "union", "left": union_tree(ids[:m]),
            "right": union_tree(ids[m:])}


def report(tag, tv, tt, t):
    bv, bf = boundary(np.asarray(tv, float), np.asarray(tt))
    op, nm, wd = strict(bv, bf)
    ml = mn.meshFromFacesVerts(np.asarray(bf, np.int32), np.asarray(bv, float))
    sx = mr.findSelfCollidingTriangles(mr.MeshPart(ml)).size()
    nc = mr.MeshComponents.getAllComponents(ml).size()
    import trimesh
    vol = trimesh.Trimesh(bv, bf, process=False).volume
    print(f"{tag:34s} tets={len(tt):7,} faces={len(bf):7,} comp={nc:4} "
          f"open={op:4} NM={nm:5} wind={wd:6} selfX={sx:5} vol={vol:10,.0f} {t:6.1f}s",
          flush=True)


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
S = [(np.ascontiguousarray(v - ctr), np.ascontiguousarray(f.astype(np.int32)))
     for v, f in S]
print(f"{len(S)} pieces, eps={EPS}\n")

# ---- baseline: soup + floodfill (what we have been doing)
V, F, off = [], [], 0
for v, f in S:
    V.append(v); F.append(f + off); off += len(v)
V = np.vstack(V); F = np.vstack(F).astype(np.int32)
diag = float(np.linalg.norm(V.max(axis=0) - V.min(axis=0)))
t0 = time.time()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    t = wm.Tetrahedralizer(epsilon=EPS / diag, edge_length_r=0.05, coarsen=True,
                           max_its=0, stop_quality=10)
    t.set_mesh(V, F)
    t.tetrahedralize()
    o = t.get_tet_mesh(floodfill=True, manifold_surface=False,
                       correct_surface_orientation=True)
report("SOUP+floodfill (ms=False)", o[0], o[1], time.time() - t0)

# ---- CSG union
tree = union_tree(list(range(len(S))))
for ms in (False, True):
    t0 = time.time()
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            t2 = wm.Tetrahedralizer(epsilon=EPS / diag, edge_length_r=0.05,
                                    coarsen=True, max_its=0, stop_quality=10)
            t2.set_meshes([v for v, _ in S], [f for _, f in S])
            t2.tetrahedralize()
            o2 = t2.get_tet_mesh_from_csg(json.dumps(tree), manifold_surface=ms,
                                          correct_surface_orientation=True)
        report(f"CSG union (ms={ms})", o2[0], o2[1], time.time() - t0)
    except Exception as e:
        print(f"CSG union (ms={ms}) FAILED: {type(e).__name__}: {str(e)[:120]}",
              flush=True)
