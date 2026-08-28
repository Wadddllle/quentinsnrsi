"""REAL buildings, CONTROLLED placement.

Boxes were too clean to reproduce the defect. So take two real sealed OneMap
pieces and slide one along x through a known sweep: far apart -> touching ->
overlapping. Real contact geometry, but placement I control exactly.

Control: each piece ALONE (expect ~0 NM).
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
A_ID = int(os.environ.get("A", "208"))
B_ID = int(os.environ.get("BI", "173"))
OUT = "data/wt_raw_test"


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


def run(V, F, tag, export=None):
    diag = float(np.linalg.norm(V.max(axis=0) - V.min(axis=0)))
    t0 = time.time()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        t = wm.Tetrahedralizer(epsilon=EPS / diag, edge_length_r=0.05, coarsen=True,
                               max_its=0, stop_quality=10)
        t.set_mesh(V, F.astype(np.int32))
        t.tetrahedralize()
        o = t.get_tet_mesh(floodfill=True, manifold_surface=True,
                           correct_surface_orientation=True)
    bv, bf = boundary(np.asarray(o[0], float), np.asarray(o[1]))
    op, nm, wd = strict(bv, bf)
    ml = mn.meshFromFacesVerts(np.asarray(bf, np.int32), np.asarray(bv, float))
    sx = mr.findSelfCollidingTriangles(mr.MeshPart(ml)).size()
    nc = mr.MeshComponents.getAllComponents(ml).size()
    tm = trimesh.Trimesh(bv, bf, process=False)
    if export:
        tm.export(f"{OUT}/{export}")
    print(f"{tag:26s} f={len(bf):7,} comp={nc:3} open={op:4} NM={nm:5} wind={wd:6} "
          f"selfX={sx:5} vol={tm.volume:10,.0f} {time.time()-t0:6.1f}s", flush=True)
    return nm


tr = Transformer.from_crs("EPSG:3414", "EPSG:4326", always_xy=True)
lo, la = tr.transform([B[0], B[2]], [B[1], B[3]])
P = extract_domain_buildings(domain_leaf_tiles(min(lo), min(la), max(lo), max(la)),
                             box(*B), store_dir="data/onemap_store")
av, af = seal_piece(P[A_ID]["verts"], P[A_ID]["faces"])
bv0, bf0 = (seal_piece(P[A_ID]["verts"], P[A_ID]["faces"]) if os.environ.get("SELF") else seal_piece(P[B_ID]["verts"], P[B_ID]["faces"]))
av = np.asarray(av, float); af = np.asarray(af)
bv0 = np.asarray(bv0, float); bf0 = np.asarray(bf0)
av = av - av.mean(axis=0)
bv0 = bv0 - bv0.mean(axis=0)
# put B to the +x side of A, then slide
axmax = av[:, 0].max(); bxmin = bv0[:, 0].min()
print(f"A = piece {A_ID} ({len(af):,} faces)   B = piece {B_ID} ({len(bf0):,} faces)")
print(f"eps = {EPS} m\n")

run(av, af, f"A alone (control)")
run(bv0, bf0, f"B alone (control)")
print()

for gap in (5.0, 1.0, 0.30, 0.10, 0.05, 0.02, 0.0, -0.10, -0.50, -2.0):
    shift = axmax - bxmin + gap
    bv = bv0.copy(); bv[:, 0] += shift
    V = np.vstack([av, bv])
    F = np.vstack([af, bf0 + len(av)])
    ex = f"SR_gap{gap}.stl" if gap in (0.02, 1.0) else None
    run(V, F, f"gap {gap:+.2f} m", export=ex)
