"""Sweep the nudge magnitude on ONE tetrahedralization.

Nudging is pure post-processing, so every magnitude reuses the same expensive mesh.
1 mm cleared NM 713 -> 0 on the full domain but created ~130 selfX: at some pinches
the local solid is thin and 1 mm inward exits the far side. Looking for a magnitude
that clears NM while staying inside the solid.

Floor is float32 resolution at domain coords (~30 um at 280 m) -- below that the
split copies re-merge on STL export and NM returns.

Self-contained on purpose: importing split_nudge re-runs its whole experiment.
"""
import sys, os; sys.path.insert(0, "/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np, io, contextlib, time
import meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
import wildmeshing as wm
from shapely.geometry import box
from pyproj import Transformer
from sbg.onemap_native.tiles import domain_leaf_tiles
from sbg.onemap_native.extract import extract_domain_buildings, seal_piece

B = (28941, 28758, 29341, 29158)
EPS = float(os.environ.get("EPS", "0.10"))
NP = int(os.environ.get("NP", "287"))
MAGS = [0.0, 0.00005, 0.0001, 0.00025, 0.0005, 0.001, 0.002]


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


def nudge(v, f, delta):
    """move each copy of a coincident vertex INTO ITS OWN LOBE by delta"""
    v = np.asarray(v, float).copy(); f = np.asarray(f)
    key = np.ascontiguousarray(v.astype(np.float32)).view([('', np.float32)] * 3).ravel()
    uq, inv, cts = np.unique(key, return_inverse=True, return_counts=True)
    dup = np.where(cts > 1)[0]
    if len(dup) == 0:
        return v, 0
    moving = np.isin(inv, dup)
    fn = np.cross(v[f[:, 1]] - v[f[:, 0]], v[f[:, 2]] - v[f[:, 0]])
    acc = np.zeros_like(v)
    for k in range(3):
        for c in range(3):
            acc[:, c] += np.bincount(f[:, k], weights=fn[:, c], minlength=len(v))
    ln = np.linalg.norm(acc, axis=1, keepdims=True); ln[ln == 0] = 1.0
    v[moving] -= (acc[moving] / ln[moving]) * delta
    return v, int(moving.sum())


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
S = [(v - ctr, f) for v, f in S]
V, F, off = [], [], 0
for v, f in S:
    V.append(v); F.append(f + off); off += len(v)
V = np.vstack(V); F = np.vstack(F).astype(np.int32)
diag = float(np.linalg.norm(V.max(axis=0) - V.min(axis=0)))
sp = float(np.spacing(np.float32(np.abs(V).max())))
print(f"{NP} pieces, {len(F):,} faces, eps={EPS}")
print(f"float32 spacing at max coord ({np.abs(V).max():.0f} m) = {sp*1e6:.1f} um\n", flush=True)

t0 = time.time()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    t = wm.Tetrahedralizer(epsilon=EPS / diag, edge_length_r=0.05, coarsen=True,
                           max_its=0, stop_quality=10)
    t.set_mesh(V, F)
    t.tetrahedralize()
    o = t.get_tet_mesh(floodfill=True, manifold_surface=True,
                       correct_surface_orientation=True)
print(f"[tetrahedralize] {time.time()-t0:.0f}s -- all magnitudes reuse this\n", flush=True)
bv, bf = boundary(np.asarray(o[0], float), np.asarray(o[1]))

print(f"{'nudge':>11} {'x float32':>10} {'open':>6} {'NM':>6} {'wind':>7} "
      f"{'selfX':>7} {'moved':>7}")
for d in MAGS:
    nv, nmv = nudge(bv, bf, d) if d > 0 else (bv, 0)
    op, nm, wd = strict(nv, bf)
    ml = mn.meshFromFacesVerts(np.asarray(bf, np.int32), np.asarray(nv, float))
    sx = mr.findSelfCollidingTriangles(mr.MeshPart(ml)).size()
    print(f"{d*1000:10.3f}mm {d/sp:10.0f} {op:6} {nm:6} {wd:7} {sx:7} {nmv:7}", flush=True)
