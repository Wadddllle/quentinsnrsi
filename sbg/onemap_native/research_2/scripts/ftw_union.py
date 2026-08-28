"""Option 1: per-building fTetWild (parallel) -> careful exact boolean union.

No voxelization anywhere, so the thin walls fTetWild preserves stay preserved.

The union follows the hard-won rules from the earlier scaling work:
  1. BINARY MERGE TREE, never a sequential fold (that is O(n^2) and blew up
     23x at 9x buildings).
  2. Only call mr.boolean() on pairs that ACTUALLY intersect; disjoint solids are
     concatenated, which is free. Exact CSG on non-touching meshes is pure waste.
  3. Nothing domain-spanning (terrain) goes in early -- it wrecks BVH culling.
     Buildings only here.
  4. mr.boolean() can return valid()==True with ZERO faces. Must check
     numValidFaces() > 0 or empty geometry silently propagates.
No interleaved decimation: fTetWild output is already ~1.6k faces/building, and
decimating would undo the whole point.
"""
import sys, os; sys.path.insert(0, "/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np, trimesh, time, io, contextlib, resource
import meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
import scipy.sparse as sp
from scipy.sparse.csgraph import connected_components
from shapely.geometry import box
from shapely.strtree import STRtree
from pyproj import Transformer
from multiprocessing import Pool
from sbg.onemap_native.tiles import domain_leaf_tiles
from sbg.onemap_native.extract import extract_domain_buildings, seal_piece

DOMS = {"duxton": (28941, 28758, 29341, 29158),
        "kentridge": (21950, 30250, 22850, 31150)}
DOM = os.environ.get("DOM", "duxton")
B = DOMS[DOM]
EPS_ABS = float(os.environ.get("EPS", "0.05"))
NPROC = int(os.environ.get("NPROC", "10"))
OUT = "data/wt_raw_test"


def rss():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def mlmesh(v, f):
    return mn.meshFromFacesVerts(np.asarray(f, np.int32), np.asarray(v, float))


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
    keep = idx[cnt == 1]
    bf = q[keep]
    a_, b_, c_ = tv[bf[:, 0]], tv[bf[:, 1]], tv[bf[:, 2]]
    flip = np.einsum('ij,ij->i', np.cross(b_ - a_, c_ - a_), tv[opp[keep]] - a_) > 0
    bf[flip] = bf[flip][:, [0, 2, 1]]
    used = np.unique(bf)
    remap = np.full(len(tv), -1, np.int64); remap[used] = np.arange(len(used))
    return tv[used], remap[bf]


def ftw_one(arg):
    """one building -> clean solid. max_threads=1: TBB would otherwise
    oversubscribe badly with NPROC processes each spawning 12 threads."""
    i, v, f = arg
    import wildmeshing as wm
    v = np.asarray(v, float); f = np.asarray(f, np.int32)
    diag = float(np.linalg.norm(v.max(axis=0) - v.min(axis=0)))
    if diag <= 0 or len(f) < 4:
        return i, None, None
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            t = wm.Tetrahedralizer(epsilon=EPS_ABS / diag, edge_length_r=0.05,
                                   coarsen=True, max_its=0, stop_quality=10,
                                   max_threads=1)
            t.set_mesh(v, f)
            t.tetrahedralize()
            o = t.get_tet_mesh(floodfill=True, manifold_surface=True,
                               correct_surface_orientation=True)
        tv, tt = np.asarray(o[0], float), np.asarray(o[1])
        if len(tt) == 0:
            return i, None, None
        bv, bf = boundary(tv, tt)
        return i, bv, bf
    except Exception:
        return i, None, None


def safe_bool(a, b):
    """rule 4: valid() alone is NOT enough -- it can be True with zero faces."""
    try:
        r = mr.boolean(a, b, mr.BooleanOperation.Union)
    except Exception:
        return None
    if not r.valid():
        return None
    if r.mesh.topology.numValidFaces() <= 0:
        return None
    return r.mesh


def merge_tree(meshes):
    """rule 1: binary tree, halving each round -- never a sequential fold."""
    cur = list(meshes)
    while len(cur) > 1:
        nxt = []
        for k in range(0, len(cur) - 1, 2):
            m = safe_bool(cur[k], cur[k + 1])
            if m is None:                       # fall back to concatenation
                nxt.append(cur[k]); nxt.append(cur[k + 1])
            else:
                nxt.append(m)
        if len(cur) % 2:
            nxt.append(cur[-1])
        if len(nxt) >= len(cur):                # no progress -> stop, keep parts
            return cur
        cur = nxt
    return cur


t0 = time.time()
tr = Transformer.from_crs("EPSG:3414", "EPSG:4326", always_xy=True)
lo, la = tr.transform([B[0], B[2]], [B[1], B[3]])
pieces = extract_domain_buildings(domain_leaf_tiles(min(lo), min(la), max(lo), max(la)),
                                  box(*B), store_dir="data/onemap_store")
jobs = []
for i, p in enumerate(pieces):
    try:
        sv, sf = seal_piece(p["verts"], p["faces"])
    except Exception:
        continue
    sv = np.asarray(sv, float); sf = np.asarray(sf)
    if len(sf) >= 4:
        jobs.append((i, sv, sf))
print(f"DOMAIN={DOM}  {len(jobs)} sealed pieces  ({time.time()-t0:.1f}s)", flush=True)

t1 = time.time()
with Pool(NPROC) as pool:
    res = pool.map(ftw_one, jobs)
solids = [(v, f) for _, v, f in res if v is not None]
nfail = sum(1 for _, v, _ in res if v is None)
print(f"[ftetwild] {len(solids)} solids, {nfail} failed, "
      f"{sum(len(f) for _, f in solids):,} faces total  "
      f"{time.time()-t1:.1f}s on {NPROC} procs", flush=True)

# ---- rule 2: only boolean pairs that ACTUALLY intersect
t2 = time.time()
ml = [mlmesh(v, f) for v, f in solids]
boxes = [box(v[:, 0].min(), v[:, 1].min(), v[:, 0].max(), v[:, 1].max())
         for v, _ in solids]
tree = STRtree(boxes)
pairs = []
for i, bx in enumerate(boxes):
    for j in tree.query(bx):
        j = int(j)
        if j <= i:
            continue
        zi = (solids[i][0][:, 2].min(), solids[i][0][:, 2].max())
        zj = (solids[j][0][:, 2].min(), solids[j][0][:, 2].max())
        if zi[1] < zj[0] or zj[1] < zi[0]:      # 3D bbox reject
            continue
        if mr.findCollidingTriangles(mr.MeshPart(ml[i]), mr.MeshPart(ml[j])).size() > 0:
            pairs.append((i, j))
print(f"[pairs] {len(pairs)} genuinely intersecting pairs  {time.time()-t2:.1f}s", flush=True)

n = len(solids)
if pairs:
    g = sp.coo_matrix((np.ones(len(pairs)), tuple(zip(*pairs))), shape=(n, n))
    ncomp, lab = connected_components(g, directed=False)
else:
    ncomp, lab = n, np.arange(n)
print(f"[groups] {ncomp} groups (max members "
      f"{np.bincount(lab).max() if n else 0})", flush=True)

t3 = time.time()
final = []
for c in range(ncomp):
    mem = [ml[k] for k in np.where(lab == c)[0]]
    final.extend(merge_tree(mem) if len(mem) > 1 else mem)
print(f"[union] {len(final)} solids after union  {time.time()-t3:.1f}s  "
      f"peakRSS={rss():.0f}MB", flush=True)

# ---- rule: disjoint solids are just concatenated, no CSG
V, F, off = [], [], 0
for m in final:
    v = mn.getNumpyVerts(m); f = mn.getNumpyFaces(m.topology)
    V.append(v); F.append(np.asarray(f) + off); off += len(v)
V = np.vstack(V); F = np.vstack(F)

op, nm, wd = strict(V, F)
mm = mlmesh(V, F)
sx = mr.findSelfCollidingTriangles(mr.MeshPart(mm)).size()
tm = trimesh.Trimesh(V, F, process=False)
rawV = np.vstack([j[1] for j in jobs])
cov = float((np.abs(trimesh.proximity.ProximityQuery(tm).signed_distance(rawV)) < 0.10).mean())
print(f"\nRESULT {DOM} fTetWild-per-building + exact union")
print(f"  faces={len(F):,}  components={mr.MeshComponents.getAllComponents(mm).size()}")
print(f"  open={op}  nonManifold={nm}  winding={wd}  selfX={sx:,}")
print(f"  volume={tm.volume:,.0f}  coverage<10cm={cov:.1%}")
os.makedirs(OUT, exist_ok=True)
tm.export(f"{OUT}/{DOM}_ftw_union.stl")
print(f"  -> {OUT}/{DOM}_ftw_union.stl")
print(f"\n[total] {time.time()-t0:.0f}s  peakRSS={rss():.0f}MB")
