"""ONE global level set for every building in the domain. No grouping at all.

Premise (now measured): meshToLevelSet is sparse -- cost ~ surface area, not bbox
volume -- so chunking into proximity groups was bounding a cost that doesn't exist.
If this holds, grouping disappears and with it every between-group self-X.

Reports the four spec properties on the raw extraction (no decimation, no slice,
no terrain) so the fusion step is isolated from everything downstream.
"""
import sys, os; sys.path.insert(0, "/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np, time, resource, gc
import meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
import scipy.sparse as sp
from scipy.sparse.csgraph import connected_components
from shapely.geometry import box
from pyproj import Transformer
from sbg.onemap_native.tiles import domain_leaf_tiles
from sbg.onemap_native.extract import extract_domain_buildings, seal_piece

VS = float(os.environ.get("VS", "0.5"))
DOMS = {"duxton": (28941, 28758, 29341, 29158),
        "kentridge": (21950, 30250, 22850, 31150)}
DOM = os.environ.get("DOM", "duxton")
B = DOMS[DOM]


def rss():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def mlmesh(v, f):
    return mn.meshFromFacesVerts(np.asarray(f, np.int32), np.asarray(v, float))


def audit(v, f, ml=None):
    """closed / edge-manifold / oriented / intersection-free.

    NO WELDING: gridToMesh already returns an indexed mesh, so edges are counted
    straight off the integer indices with a 1-D int64 key. The previous version
    lexsorted ~5M float rows via np.unique(axis=0) and took longer than the whole
    pipeline it was checking.
    """
    v = np.asarray(v, float); f = np.asarray(f).astype(np.int64)
    f = f[(f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 0] != f[:, 2])]
    nv = np.int64(len(v))
    de = np.vstack([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])   # directed
    a, b = de[:, 0], de[:, 1]
    lo, hi = np.minimum(a, b), np.maximum(a, b)
    _, cnt = np.unique(lo * nv + hi, return_counts=True)          # undirected
    _, dcnt = np.unique(a * nv + b, return_counts=True)           # directed
    wind = int((dcnt - 1)[dcnt > 1].sum())
    g = sp.coo_matrix((np.ones(len(lo), np.int8), (lo, hi)), shape=(nv, nv))
    ncomp = connected_components(g, directed=False)[0]
    if ml is None:
        ml = mlmesh(v, f)
    sx = mr.findSelfCollidingTriangles(mr.MeshPart(ml)).size()
    return dict(faces=len(f), open=int((cnt == 1).sum()), nm=int((cnt > 2).sum()),
                wind=wind, selfX=int(sx), comps=ncomp)


t0 = time.time()
tr = Transformer.from_crs("EPSG:3414", "EPSG:4326", always_xy=True)
lo, la = tr.transform([B[0], B[2]], [B[1], B[3]])
pieces = extract_domain_buildings(domain_leaf_tiles(min(lo), min(la), max(lo), max(la)),
                                  box(*B), store_dir="data/onemap_store")
print(f"DOMAIN={DOM}  VS={VS}  {len(pieces)} pieces  ({time.time()-t0:.1f}s)")

# ---- seal every piece, concatenate into ONE mesh
t1 = time.time()
V, F, noff = [], [], 0
for p in pieces:
    try:
        sv, sf = seal_piece(p["verts"], p["faces"])
    except Exception:
        sv, sf = p["verts"], p["faces"]
    sv = np.asarray(sv, float); sf = np.asarray(sf)
    if len(sf) == 0:
        continue
    V.append(sv); F.append(sf + noff); noff += len(sv)
V = np.vstack(V); F = np.vstack(F)
print(f"[seal+concat] {len(V):,} verts  {len(F):,} faces  {time.time()-t1:.1f}s")

d = V.max(axis=0) - V.min(axis=0)
print(f"[bbox] {d[0]:.0f} x {d[1]:.0f} x {d[2]:.0f} m  "
      f"-> dense equivalent {np.prod(d/VS):,.0f} voxels")

# ---- ONE level set, ONE extraction
gc.collect()
t2 = time.time()
grid = mr.meshToLevelSet(mr.MeshPart(mlmesh(V, F)), mr.AffineXf3f(),
                         mr.Vector3f(VS, VS, VS), 3.0)
t_ls = time.time() - t2
print(f"[levelset] ONE global grid  {t_ls:.1f}s  peakRSS={rss():.0f}MB", flush=True)

t3 = time.time()
gs = mr.GridToMeshSettings()
gs.voxelSize = mr.Vector3f(VS, VS, VS)
gs.isoValue = 0.0
gs.adaptivity = 0.0
out = mr.gridToMesh(grid, gs)
t_gm = time.time() - t3
ov = mn.getNumpyVerts(out); of_ = mn.getNumpyFaces(out.topology)
print(f"[gridToMesh] {len(of_):,} faces  {t_gm:.1f}s  peakRSS={rss():.0f}MB", flush=True)

print(f"[closed?] meshlib isClosed={out.topology.isClosed()} "
      f"numHoles={out.topology.findNumHoles()}", flush=True)
t4 = time.time()
a = audit(ov, of_, ml=out)
print(f"[audit] {time.time()-t4:.1f}s")
print(f"\nRESULT {DOM} GLOBAL-LEVELSET (raw, no decimate/slice/terrain)")
print(f"  faces={a['faces']:,}  components={a['comps']}")
print(f"  open={a['open']}  nonManifold={a['nm']}  winding={a['wind']}  selfX={a['selfX']}")
# --- STRICT check: re-weld by EXACT position, which is what a downstream mesher
# does when it reads the STL (unwelded soup). Exact, NOT rounded -- rounding to
# 1e-4 is finer than float32 spacing at 29km coords and manufactures fake defects.
t5 = time.time()
vv = np.asarray(ov, np.float32)
uq, inv = np.unique(vv.view([('', np.float32)] * 3).ravel(), return_inverse=True)
idx = inv.astype(np.int64)[np.asarray(of_).astype(np.int64)]
idx = idx[(idx[:, 0] != idx[:, 1]) & (idx[:, 1] != idx[:, 2]) & (idx[:, 0] != idx[:, 2])]
n2 = np.int64(len(uq))
de = np.vstack([idx[:, [0, 1]], idx[:, [1, 2]], idx[:, [2, 0]]])
a2, b2 = de[:, 0], de[:, 1]
_, c2 = np.unique(np.minimum(a2, b2) * n2 + np.maximum(a2, b2), return_counts=True)
_, d2 = np.unique(a2 * n2 + b2, return_counts=True)
print(f"\nSTRICT exact-position weld ({len(ov):,} -> {len(uq):,} verts, "
      f"{time.time()-t5:.1f}s)")
print(f"  open={int((c2==1).sum())}  nonManifold={int((c2>2).sum())}  "
      f"winding={int((d2-1)[d2>1].sum())}")

# --- FIX: the mesh is manifold in its OWN topology; it only reads non-manifold
# when a consumer welds by position. So separate the coincident positions.
# Moving a vertex cannot change connectivity, so closure is preserved by
# construction. Only self-intersection needs re-checking.
NUDGE = float(os.environ.get("NUDGE", "0.001"))       # 1mm vs 0.5m voxel
if NUDGE > 0 and int((c2 > 2).sum()) > 0:
    t6 = time.time()
    _, first, ncopy = np.unique(inv, return_index=True, return_counts=True)
    dup_pos = np.where(ncopy > 1)[0]                  # welded ids with >1 original
    dupmask = np.isin(inv, dup_pos)
    affected = np.where(dupmask)[0]                   # original vertex ids
    fo = np.asarray(of_).astype(np.int64)
    touch = np.isin(fo, affected).any(axis=1)
    ft = fo[touch]
    p = np.asarray(ov, float)
    fn = np.cross(p[ft[:, 1]] - p[ft[:, 0]], p[ft[:, 2]] - p[ft[:, 0]])
    acc = np.zeros((len(p), 3))
    for k in range(3):
        for c in range(3):
            acc[:, c] += np.bincount(ft[:, k], weights=fn[:, c], minlength=len(p))
    nrm = np.linalg.norm(acc, axis=1, keepdims=True)
    nrm[nrm == 0] = 1.0
    p2 = p.copy()
    p2[affected] -= (acc[affected] / nrm[affected]) * NUDGE   # inward
    # re-verify under a strict exact-position weld
    v3 = np.asarray(p2, np.float32)
    uq3, inv3 = np.unique(v3.view([('', np.float32)] * 3).ravel(), return_inverse=True)
    i3 = inv3.astype(np.int64)[fo]
    i3 = i3[(i3[:, 0] != i3[:, 1]) & (i3[:, 1] != i3[:, 2]) & (i3[:, 0] != i3[:, 2])]
    n3 = np.int64(len(uq3))
    d3 = np.vstack([i3[:, [0, 1]], i3[:, [1, 2]], i3[:, [2, 0]]])
    a3, b3 = d3[:, 0], d3[:, 1]
    _, cc3 = np.unique(np.minimum(a3, b3) * n3 + np.maximum(a3, b3), return_counts=True)
    _, dd3 = np.unique(a3 * n3 + b3, return_counts=True)
    sx3 = mr.findSelfCollidingTriangles(mr.MeshPart(mlmesh(p2, fo))).size()
    print(f"\nAFTER NUDGE ({NUDGE*1000:.1f}mm, {len(affected)} verts moved, "
          f"{time.time()-t6:.1f}s)")
    print(f"  open={int((cc3==1).sum())}  nonManifold={int((cc3>2).sum())}  "
          f"winding={int((dd3-1)[dd3>1].sum())}  selfX={int(sx3)}")

print(f"\n[total] {time.time()-t0:.0f}s   peakRSS={rss():.0f}MB")
