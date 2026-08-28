"""Sweep epsilon UP on one building. Bigger eps = coarser envelope = much faster.
Find where it stops being 0/0/0/0 and where the thin walls actually die.
Exports an STL per eps for eyeballing.
"""
import sys, os; sys.path.insert(0, "/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np, trimesh, time, io, contextlib
import meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
import wildmeshing as wm
from shapely.geometry import box
from pyproj import Transformer
from sbg.onemap_native.tiles import domain_leaf_tiles
from sbg.onemap_native.extract import extract_domain_buildings, seal_piece

PIECE = int(os.environ.get("PIECE", "26"))
B = (28941, 28758, 29341, 29158)
OUT = "data/wt_raw_test"
EPSS = [float(x) for x in os.environ.get(
    "EPSS", "0.05,0.1,0.25,0.5,1.0,2.0").split(",")]


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
    keep = idx[cnt == 1]; bf = q[keep]
    a_, b_, c_ = tv[bf[:, 0]], tv[bf[:, 1]], tv[bf[:, 2]]
    flip = np.einsum('ij,ij->i', np.cross(b_ - a_, c_ - a_), tv[opp[keep]] - a_) > 0
    bf[flip] = bf[flip][:, [0, 2, 1]]
    used = np.unique(bf)
    rm = np.full(len(tv), -1, np.int64); rm[used] = np.arange(len(used))
    return tv[used], rm[bf]


tr = Transformer.from_crs("EPSG:3414", "EPSG:4326", always_xy=True)
lo, la = tr.transform([B[0], B[2]], [B[1], B[3]])
pieces = extract_domain_buildings(domain_leaf_tiles(min(lo), min(la), max(lo), max(la)),
                                  box(*B), store_dir="data/onemap_store")
v, f = seal_piece(pieces[PIECE]["verts"], pieces[PIECE]["faces"])
v = np.asarray(v, float) ; f = np.asarray(f, np.int32)
v = v - v.mean(axis=0)
diag = float(np.linalg.norm(v.max(axis=0) - v.min(axis=0)))
raw = trimesh.Trimesh(v, f, process=False)
os.makedirs(OUT, exist_ok=True)
raw.export(f"{OUT}/E_P{PIECE}_RAW.stl")
print(f"piece {PIECE}: {len(f):,} faces, diag {diag:.1f}m, volume {raw.volume:,.0f}")
print(f"RAW -> {OUT}/E_P{PIECE}_RAW.stl\n")

print(f"{'eps(m)':>7} {'faces':>8} {'open':>5} {'NM':>4} {'wind':>5} {'selfX':>6} "
      f"{'volume':>11} {'dVol':>7} {'cov<10cm':>9} {'cov<50cm':>9} {'time':>7}")
for e in EPSS:
    t0 = time.time()
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            t = wm.Tetrahedralizer(epsilon=e / diag, edge_length_r=0.05, coarsen=True,
                                   max_its=0, stop_quality=10)
            t.set_mesh(v, f)
            t.tetrahedralize()
            o = t.get_tet_mesh(floodfill=True, manifold_surface=True,
                               correct_surface_orientation=True)
        bv, bf = boundary(np.asarray(o[0], float), np.asarray(o[1]))
        dt = time.time() - t0
        op, nm, wd = strict(bv, bf)
        ml = mn.meshFromFacesVerts(np.asarray(bf, np.int32), np.asarray(bv, float))
        sx = mr.findSelfCollidingTriangles(mr.MeshPart(ml)).size()
        tm = trimesh.Trimesh(bv, bf, process=False)
        d = np.abs(trimesh.proximity.ProximityQuery(tm).signed_distance(v))
        tm.export(f"{OUT}/E_P{PIECE}_eps{e}.stl")
        print(f"{e:7.2f} {len(bf):8,} {op:5} {nm:4} {wd:5} {sx:6,} {tm.volume:11,.0f} "
              f"{(tm.volume/raw.volume-1)*100:6.1f}% {float((d<0.10).mean()):9.1%} "
              f"{float((d<0.50).mean()):9.1%} {dt:6.1f}s", flush=True)
    except Exception as ex:
        print(f"{e:7.2f}  FAILED {type(ex).__name__}: {str(ex)[:50]}", flush=True)
print(f"\nSTLs: {OUT}/E_P{PIECE}_eps*.stl  (+ E_P{PIECE}_RAW.stl)")
