"""Is the domain-scale defect caused by edge_length_r being RELATIVE to the bbox?

ideal_edge_length = bbox_diag * edge_length_r. One building (diag ~117m) -> 5.8m
target edge, comparable to the building. A 400m domain (diag ~545m) -> 27m target
edge, far LARGER than a 10m shophouse.

Synthetic: N boxes of fixed size scattered over an increasing extent. Box size is
constant, only the domain grows -- so if NM tracks the extent, edge_length_r is
the culprit, not proximity or building shape.
"""
import sys, os; sys.path.insert(0, "/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np, trimesh, io, contextlib, time
import meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
import wildmeshing as wm

EPS = 0.10
BOX = 10.0
N = int(os.environ.get("N", "20"))


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


def run(V, F, elr):
    diag = float(np.linalg.norm(V.max(axis=0) - V.min(axis=0)))
    t0 = time.time()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        t = wm.Tetrahedralizer(epsilon=EPS / diag, edge_length_r=elr, coarsen=True,
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
    vol = trimesh.Trimesh(bv, bf, process=False).volume
    return dict(diag=diag, edge=diag * elr, faces=len(bf), comp=nc, open=op,
                nm=nm, wind=wd, selfX=sx, vol=vol, t=time.time() - t0)


def scene(extent, n=N, seed=0):
    rng = np.random.default_rng(seed)
    pos = rng.uniform(0, max(extent - BOX, 1e-6), size=(n, 2))
    V, F, off = [], [], 0
    for p in pos:
        b = trimesh.creation.box(extents=[BOX, BOX, BOX])
        b.apply_translation([p[0], p[1], BOX / 2])
        V.append(b.vertices); F.append(np.asarray(b.faces) + off); off += len(b.vertices)
    return np.vstack(V), np.vstack(F)


print(f"{N} boxes of {BOX:.0f}m, scattered over a growing extent. eps={EPS}m\n")
print("A) default edge_length_r = 0.05  (ideal edge scales WITH the domain)")
print(f"{'extent':>8} {'diag':>7} {'ideal edge':>11} {'faces':>7} {'comp':>5} "
      f"{'open':>5} {'NM':>5} {'selfX':>6} {'volume':>9} {'t':>7}")
for ext in (50, 100, 200, 400, 800):
    V, F = scene(ext)
    try:
        r = run(V, F, 0.05)
        print(f"{ext:8.0f} {r['diag']:7.0f} {r['edge']:11.1f} {r['faces']:7,} "
              f"{r['comp']:5} {r['open']:5} {r['nm']:5} {r['selfX']:6} "
              f"{r['vol']:9,.0f} {r['t']:6.1f}s", flush=True)
    except Exception as e:
        print(f"{ext:8.0f}  FAILED {type(e).__name__}: {str(e)[:40]}", flush=True)

print(f"\nB) edge_length_r pinned so ideal edge stays ~{BOX/2:.0f}m regardless of extent")
print(f"{'extent':>8} {'diag':>7} {'ideal edge':>11} {'faces':>7} {'comp':>5} "
      f"{'open':>5} {'NM':>5} {'selfX':>6} {'volume':>9} {'t':>7}")
for ext in (50, 100, 200, 400, 800):
    V, F = scene(ext)
    diag = float(np.linalg.norm(V.max(axis=0) - V.min(axis=0)))
    elr = (BOX / 2) / diag
    try:
        r = run(V, F, elr)
        print(f"{ext:8.0f} {r['diag']:7.0f} {r['edge']:11.1f} {r['faces']:7,} "
              f"{r['comp']:5} {r['open']:5} {r['nm']:5} {r['selfX']:6} "
              f"{r['vol']:9,.0f} {r['t']:6.1f}s", flush=True)
    except Exception as e:
        print(f"{ext:8.0f}  FAILED {type(e).__name__}: {str(e)[:40]}", flush=True)

print(f"\n  {N} separate boxes => expected {N} components, "
      f"volume {N*BOX**3:,.0f} m3 (overlaps reduce both)")
