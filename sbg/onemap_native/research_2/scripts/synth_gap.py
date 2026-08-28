"""Synthetic ground truth: TWO BOXES a known distance apart, through fTetWild.

Everything about the input is known exactly, so the output is unambiguous.
Prediction under test: a gap smaller than eps gets collapsed, putting the two
facing walls at identical coordinates -> 4-face (non-manifold) edges after a
positional weld. A gap larger than eps survives.

Reports the gap MEASURED IN THE OUTPUT, so we see what actually happened to it.
"""
import sys, os; sys.path.insert(0, "/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np, trimesh, io, contextlib, time
import meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
import wildmeshing as wm

OUT = "data/wt_raw_test"
EPS = float(os.environ.get("EPS", "0.10"))
SIZE = 10.0
GAPS = [float(x) for x in os.environ.get(
    "GAPS", "-0.05,0.0,0.01,0.02,0.05,0.10,0.15,0.20,0.30,0.50").split(",")]


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


print(f"two {SIZE:.0f}m boxes, gap swept, eps = {EPS} m")
print(f"box A spans x [-{SIZE:.0f}, 0];  box B starts at x = gap\n")
print(f"{'gap(m)':>8} {'faces':>7} {'comp':>5} {'open':>5} {'NM':>4} {'wind':>5} "
      f"{'selfX':>6} {'volume':>9} {'gap in OUTPUT':>15} {'verdict':>22}")

for g in GAPS:
    A = trimesh.creation.box(extents=[SIZE, SIZE, SIZE])
    A.apply_translation([-SIZE / 2, 0, 0])
    Bx = trimesh.creation.box(extents=[SIZE, SIZE, SIZE])
    Bx.apply_translation([SIZE / 2 + g, 0, 0])
    V = np.vstack([A.vertices, Bx.vertices])
    F = np.vstack([A.faces, Bx.faces + len(A.vertices)]).astype(np.int32)
    diag = float(np.linalg.norm(V.max(axis=0) - V.min(axis=0)))
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            t = wm.Tetrahedralizer(epsilon=EPS / diag, edge_length_r=0.05,
                                   coarsen=True, max_its=0, stop_quality=10)
            t.set_mesh(V, F)
            t.tetrahedralize()
            o = t.get_tet_mesh(floodfill=True, manifold_surface=True,
                               correct_surface_orientation=True)
        bv, bf = boundary(np.asarray(o[0], float), np.asarray(o[1]))
        op, nm, wd = strict(bv, bf)
        ml = mn.meshFromFacesVerts(np.asarray(bf, np.int32), np.asarray(bv, float))
        sx = mr.findSelfCollidingTriangles(mr.MeshPart(ml)).size()
        ncomp = mr.MeshComponents.getAllComponents(ml).size()
        tm = trimesh.Trimesh(bv, bf, process=False)
        # the two facing walls are at x=0 and x=g. measure what survives between.
        xs = bv[:, 0]
        inner = xs[(xs > -1.0) & (xs < g + 1.0)]
        got = "-"
        if inner.size:
            lo_ = inner[inner <= (g / 2 if g > 0 else 0)]
            hi_ = inner[inner > (g / 2 if g > 0 else 0)]
            if lo_.size and hi_.size:
                got = f"{hi_.min() - lo_.max():+.4f}"
        if nm == 0 and ncomp == 2:
            verdict = "gap kept, clean"
        elif nm == 0 and ncomp == 1:
            verdict = "FUSED, clean"
        else:
            verdict = f"collapsed -> {nm} NM"
        print(f"{g:8.3f} {len(bf):7,} {ncomp:5} {op:5} {nm:4} {wd:5} {sx:6} "
              f"{tm.volume:9,.0f} {got:>15} {verdict:>22}")
        if g in (0.02, 0.20):
            tm.export(f"{OUT}/SYNTH_gap{g}_eps{EPS}.stl")
    except Exception as e:
        print(f"{g:8.3f}  FAILED {type(e).__name__}: {str(e)[:40]}")

print(f"\n  2 boxes of {SIZE:.0f}m => true volume 2000 m3 if separate")
print(f"  STLs for gap 0.02 and 0.20 written to {OUT}/")
