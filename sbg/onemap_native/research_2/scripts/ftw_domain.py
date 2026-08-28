"""Option 2: whole domain through fTetWild in ONE call, max_its=0.

If this works there is no fusion step at all -- fTetWild resolves inter-building
intersections itself. Cost at domain scale is the unknown; tet count is driven by
domain VOLUME, not input face count, so this is the make-or-break.
"""
import sys, os; sys.path.insert(0, "/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np, trimesh, time, io, contextlib, resource
import meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
import wildmeshing as wm
from shapely.geometry import box
from pyproj import Transformer
from sbg.onemap_native.tiles import domain_leaf_tiles
from sbg.onemap_native.extract import extract_domain_buildings, seal_piece

DOMS = {"duxton": (28941, 28758, 29341, 29158),
        "kentridge": (21950, 30250, 22850, 31150)}
DOM = os.environ.get("DOM", "duxton")
B = DOMS[DOM]
EPS_ABS = float(os.environ.get("EPS", "0.05"))
ITS = int(os.environ.get("ITS", "0"))
SHRINK = float(os.environ.get("SHRINK", "0.0"))   # metres, per-piece XY inset
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


t0 = time.time()
tr = Transformer.from_crs("EPSG:3414", "EPSG:4326", always_xy=True)
lo, la = tr.transform([B[0], B[2]], [B[1], B[3]])
pieces = extract_domain_buildings(domain_leaf_tiles(min(lo), min(la), max(lo), max(la)),
                                  box(*B), store_dir="data/onemap_store")
V, F, n = [], [], 0
for p in pieces:
    try:
        sv, sf = seal_piece(p["verts"], p["faces"])
    except Exception:
        sv, sf = p["verts"], p["faces"]
    sv = np.asarray(sv, float); sf = np.asarray(sf)
    if len(sf) == 0:
        continue
    if SHRINK > 0:
        # Adjacent shophouses share a party wall, so two solids carry COINCIDENT
        # surfaces; a positional weld then fuses them into non-manifold edges.
        # Inset each piece in XY so no two walls can ever be co-located.
        c = 0.5 * (sv[:, :2].max(axis=0) + sv[:, :2].min(axis=0))
        half = 0.5 * (sv[:, :2].max(axis=0) - sv[:, :2].min(axis=0))
        fac = np.where(half > 2 * SHRINK, 1.0 - SHRINK / np.maximum(half, 1e-9), 1.0)
        sv = sv.copy()
        sv[:, :2] = c + (sv[:, :2] - c) * fac
    V.append(sv); F.append(sf + n); n += len(sv)
V = np.vstack(V); F = np.vstack(F).astype(np.int32)
V = V - V.mean(axis=0)
diag = float(np.linalg.norm(V.max(axis=0) - V.min(axis=0)))
eps_rel = EPS_ABS / diag
d = V.max(axis=0) - V.min(axis=0)
print(f"DOMAIN={DOM}  {len(pieces)} pieces  {len(F):,} sealed faces")
print(f"bbox {d[0]:.0f}x{d[1]:.0f}x{d[2]:.0f}m  diag {diag:.0f}m")
print(f"eps_abs={EPS_ABS}m -> eps_rel={eps_rel:.2e}   max_its={ITS}  shrink={SHRINK}m",
      flush=True)

t1 = time.time()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    t = wm.Tetrahedralizer(epsilon=eps_rel, edge_length_r=0.05, coarsen=True,
                           max_its=ITS, stop_quality=10)
    t.set_mesh(V, F)
    t.tetrahedralize()
    out = t.get_tet_mesh(floodfill=True, manifold_surface=True,
                         correct_surface_orientation=True)
tv, tt = np.asarray(out[0], float), np.asarray(out[1])
print(f"[ftetwild] {len(tv):,} verts  {len(tt):,} tets  "
      f"{time.time()-t1:.1f}s  peakRSS={rss():.0f}MB", flush=True)

bv, bf = boundary(tv, tt)
op, nm, wd = strict(bv, bf)
m = mlmesh(bv, bf)
sx = mr.findSelfCollidingTriangles(mr.MeshPart(m)).size()
ncomp = mr.MeshComponents.getAllComponents(m).size()
tm = trimesh.Trimesh(bv, bf, process=False)
cov = float((np.abs(trimesh.proximity.ProximityQuery(tm).signed_distance(V)) < 0.10).mean())
print(f"\nRESULT {DOM} WHOLE-DOMAIN fTetWild")
print(f"  faces={len(bf):,}  components={ncomp}")
print(f"  open={op}  nonManifold={nm}  winding={wd}  selfX={sx:,}")
print(f"  volume={tm.volume:,.0f}  coverage<10cm={cov:.1%}")
os.makedirs(OUT, exist_ok=True)
tag = f"{DOM}_ftetwild_domain" + (f"_s{SHRINK}" if SHRINK else "")
tm.export(f"{OUT}/{tag}.stl")
print(f"  -> {OUT}/{tag}.stl")
print(f"\n[total] {time.time()-t0:.0f}s  peakRSS={rss():.0f}MB")
