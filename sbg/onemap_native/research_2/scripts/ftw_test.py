"""THE decisive fTetWild test: do sub-voxel-thickness walls SURVIVE?

Voxel/DMC samples on a grid, so a 30cm wall in a 0.5m grid aliases into
'pillar gap pillar' or vanishes. fTetWild inserts the real triangles and keeps
the output within an envelope eps of the input, so a thin wall cannot be missed
between samples.

Metric that actually detects the artifact: for every RAW vertex, distance to the
output surface. A wall that vanished leaves its vertices stranded far away.
Face count and the 4 spec properties reported alongside.
"""
import sys, os; sys.path.insert(0, "/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np, trimesh, time
import meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
import wildmeshing as wm
from shapely.geometry import box
from pyproj import Transformer
from sbg.onemap_native.tiles import domain_leaf_tiles
from sbg.onemap_native.extract import extract_domain_buildings, seal_piece

PIECE = int(os.environ.get("PIECE", "26"))
EPS_ABS = float(os.environ.get("EPS", "0.05"))
DOM = os.environ.get("DOM", "duxton")
DOMS = {"duxton": (28941, 28758, 29341, 29158),
        "kentridge": (21950, 30250, 22850, 31150)}
B = DOMS[DOM]


def mlmesh(v, f):
    return mn.meshFromFacesVerts(np.asarray(f, np.int32), np.asarray(v, float))


def strict(v, f):
    """weld by EXACT float32 position -- what a consumer's STL import does"""
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


OUTDIR = "data/wt_raw_test"


def report(tag, v, f, rawv, t, out=None):
    op, nm, wd = strict(v, f)
    m = mlmesh(v, f)
    sx = mr.findSelfCollidingTriangles(mr.MeshPart(m)).size()
    tm = trimesh.Trimesh(np.asarray(v, float), np.asarray(f), process=False)
    d = trimesh.proximity.ProximityQuery(tm).signed_distance(rawv)
    d = np.abs(d)
    cov10 = float((d < 0.10).mean()); cov25 = float((d < 0.25).mean())
    print(f"{tag:22s} f={len(f):>9,} comp={mr.MeshComponents.getAllComponents(m).size():>4} "
          f"open={op:>5} NM={nm:>4} wind={wd:>5} selfX={sx:>6,} "
          f"vol={tm.volume:>12,.0f}  cov<10cm={cov10:6.1%} cov<25cm={cov25:6.1%}  {t:6.1f}s")
    if out:
        os.makedirs(OUTDIR, exist_ok=True)
        p = os.path.join(OUTDIR, out)
        tm.export(p)
        print(f"{'':22s} -> {p}")


tr = Transformer.from_crs("EPSG:3414", "EPSG:4326", always_xy=True)
lo, la = tr.transform([B[0], B[2]], [B[1], B[3]])
pieces = extract_domain_buildings(domain_leaf_tiles(min(lo), min(la), max(lo), max(la)),
                                  box(*B), store_dir="data/onemap_store")
v, f = seal_piece(pieces[PIECE]["verts"], pieces[PIECE]["faces"])
v = np.asarray(v, float); f = np.asarray(f, np.int32)
# recentre: fTetWild eps is RELATIVE to bbox diagonal, and huge SVY21 coords
# also cost float32 precision downstream
ctr = v.mean(axis=0)
v = v - ctr
diag = float(np.linalg.norm(v.max(axis=0) - v.min(axis=0)))
eps_rel = EPS_ABS / diag
print(f"piece {PIECE}: {len(f):,} faces, bbox diag {diag:.1f}m")
print(f"eps_abs={EPS_ABS}m -> eps_rel={eps_rel:.2e}  (default 1e-3 would be {diag*1e-3:.2f}m)\n")

os.makedirs("data/wt_raw_test", exist_ok=True)
trimesh.Trimesh(v, f, process=False).export(f"data/wt_raw_test/P{PIECE}_RAW_sealed.stl")
print(f"RAW sealed -> data/wt_raw_test/P{PIECE}_RAW_sealed.stl  ({len(f):,} faces)\n")
print(f"{'method':22s} {'faces':>11} {'comp':>5} {'open':>10} {'NM':>7} {'wind':>8} "
      f"{'selfX':>9} {'volume':>15}  coverage vs RAW")

# ---- baseline: DMC at two voxel sizes
for VS in (0.5, 2.0):
    t0 = time.time()
    g = mr.meshToLevelSet(mr.MeshPart(mlmesh(v, f)), mr.AffineXf3f(),
                          mr.Vector3f(VS, VS, VS), 3.0)
    gs = mr.GridToMeshSettings()
    gs.voxelSize = mr.Vector3f(VS, VS, VS); gs.isoValue = 0.0; gs.adaptivity = 0.0
    m = mr.gridToMesh(g, gs)
    report(f"DMC voxel {VS}", mn.getNumpyVerts(m), mn.getNumpyFaces(m.topology),
           v, time.time() - t0, out=f"P{PIECE}_dmc_vs{VS}.stl")

# ---- fTetWild
t0 = time.time()
t = wm.Tetrahedralizer(epsilon=eps_rel, edge_length_r=0.05, coarsen=True)
t.set_mesh(v, f.astype(np.int32))
t.tetrahedralize()
out = t.get_tet_mesh(floodfill=True, manifold_surface=True,
                     correct_surface_orientation=True)
tv, tt = np.asarray(out[0], float), np.asarray(out[1])
print(f"\n  [ftetwild] {len(tv):,} verts  {len(tt):,} tets  {time.time()-t0:.1f}s")
# boundary surface = tet faces used exactly once.
# Orient each OUTWARD using the tet's opposite vertex -- tet face ordering is
# arbitrary, so skipping this yields inconsistent winding (and meshlib then
# splits vertices at the seams and reports fake fragmentation).
nt = len(tt)
q = np.vstack([tt[:, [0, 1, 2]], tt[:, [0, 1, 3]], tt[:, [0, 2, 3]], tt[:, [1, 2, 3]]])
opp = np.concatenate([tt[:, 3], tt[:, 2], tt[:, 1], tt[:, 0]])
srt = np.sort(q, axis=1)
uq, idx, cnt = np.unique(srt, axis=0, return_index=True, return_counts=True)
keep = idx[cnt == 1]
bf = q[keep]
d4 = tv[opp[keep]]
a_, b_, c_ = tv[bf[:, 0]], tv[bf[:, 1]], tv[bf[:, 2]]
nrm = np.cross(b_ - a_, c_ - a_)
flip = np.einsum('ij,ij->i', nrm, d4 - a_) > 0        # normal points at opposite vtx
bf[flip] = bf[flip][:, [0, 2, 1]]
used = np.unique(bf)
remap = np.full(len(tv), -1, np.int64); remap[used] = np.arange(len(used))
report(f"fTetWild eps={EPS_ABS}", tv[used], remap[bf], v, time.time() - t0,
       out=f"P{PIECE}_ftetwild_eps{EPS_ABS}.stl")
