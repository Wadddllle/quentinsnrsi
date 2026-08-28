"""fTetWild speed tuning: can we stop right after triangle insertion?

Paper pipeline: (1) simplify, (2) BSP subdivide + insert input triangles,
(3) mesh IMPROVEMENT/optimization. Stage 3 was 36 of 38s on piece 26, and it
exists to make good-quality TETS -- which we do not need, because we throw the
interior away and keep only the boundary surface. The envelope guarantee is
maintained from insertion onward, so halting early should keep fidelity.

Knobs: max_its (optimization passes), stop_quality (stop when max tet energy is
below this -- set huge to stop immediately), skip_simplify.
Judged on: does the surface still cover the RAW input, and is it still 0/0/0/0.
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
EPS_ABS = float(os.environ.get("EPS", "0.05"))
B = (28941, 28758, 29341, 29158)


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


tr = Transformer.from_crs("EPSG:3414", "EPSG:4326", always_xy=True)
lo, la = tr.transform([B[0], B[2]], [B[1], B[3]])
pieces = extract_domain_buildings(domain_leaf_tiles(min(lo), min(la), max(lo), max(la)),
                                  box(*B), store_dir="data/onemap_store")
v, f = seal_piece(pieces[PIECE]["verts"], pieces[PIECE]["faces"])
v = np.asarray(v, float); f = np.asarray(f, np.int32)
v = v - v.mean(axis=0)
diag = float(np.linalg.norm(v.max(axis=0) - v.min(axis=0)))
eps_rel = EPS_ABS / diag
print(f"piece {PIECE}: {len(f):,} faces, diag {diag:.1f}m, eps_rel={eps_rel:.2e}\n")

CONFIGS = [
    ("baseline (its=80,q=10)",   dict(max_its=80, stop_quality=10)),
    ("its=0",                    dict(max_its=0,  stop_quality=10)),
    ("its=1",                    dict(max_its=1,  stop_quality=10)),
    ("its=2",                    dict(max_its=2,  stop_quality=10)),
    ("q=1e5 (stop at once)",     dict(max_its=80, stop_quality=1e5)),
    ("its=0 + skip_simplify",    dict(max_its=0,  stop_quality=10, skip_simplify=True)),
]

print(f"{'config':28s} {'faces':>9} {'tets':>9} {'open':>6} {'NM':>5} {'wind':>6} "
      f"{'selfX':>7} {'vol':>11} {'cov<10cm':>9} {'t':>8}")
for name, kw in CONFIGS:
    t0 = time.time()
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):          # fTetWild is very chatty
            t = wm.Tetrahedralizer(epsilon=eps_rel, edge_length_r=0.05,
                                   coarsen=True, **kw)
            t.set_mesh(v, f)
            t.tetrahedralize()
            out = t.get_tet_mesh(floodfill=True, manifold_surface=True,
                                 correct_surface_orientation=True)
        tv, tt = np.asarray(out[0], float), np.asarray(out[1])
        bv, bf = boundary(tv, tt)
        dt = time.time() - t0
        op, nm, wd = strict(bv, bf)
        sx = mr.findSelfCollidingTriangles(mr.MeshPart(mlmesh(bv, bf))).size()
        tm = trimesh.Trimesh(bv, bf, process=False)
        cov = float((np.abs(trimesh.proximity.ProximityQuery(tm)
                            .signed_distance(v)) < 0.10).mean())
        print(f"{name:28s} {len(bf):9,} {len(tt):9,} {op:6} {nm:5} {wd:6} {sx:7,} "
              f"{tm.volume:11,.0f} {cov:9.1%} {dt:7.1f}s", flush=True)
    except Exception as e:
        print(f"{name:28s} FAILED: {type(e).__name__}: {str(e)[:60]}", flush=True)
