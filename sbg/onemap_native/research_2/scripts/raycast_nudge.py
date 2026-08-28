"""Raycast-limited nudge.

A fixed nudge moves BOTH copies of a split vertex by the same amount regardless of
how much solid is behind each -- so where a lobe is thin it punches through
(selfX ~120 at domain scale, and magnitude-independent, so tuning does not help).

Fix: cast a ray inward from each copy along its own inward normal, measure the real
local thickness, and cap that copy's motion at a fraction of it. Punch-through
becomes impossible by construction. Where one lobe is thin, the separation is
achieved by moving the OTHER copy further, so the pair still separates enough to
survive a float32 weld.

Strategies compared on ONE tetrahedralization (nudging is pure post-processing).
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
NP = int(os.environ.get("NP", "287"))
TARGET = float(os.environ.get("TARGET", "0.0005"))   # separation we want, metres
FRAC = float(os.environ.get("FRAC", "0.25"))         # max fraction of local thickness


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


def vnormals(v, f):
    fn = np.cross(v[f[:, 1]] - v[f[:, 0]], v[f[:, 2]] - v[f[:, 0]])
    acc = np.zeros_like(v)
    for k in range(3):
        for c in range(3):
            acc[:, c] += np.bincount(f[:, k], weights=fn[:, c], minlength=len(v))
    ln = np.linalg.norm(acc, axis=1, keepdims=True); ln[ln == 0] = 1.0
    return acc / ln


def coincident_groups(v):
    key = np.ascontiguousarray(v.astype(np.float32)).view([('', np.float32)] * 3).ravel()
    uq, inv, cts = np.unique(key, return_inverse=True, return_counts=True)
    dup = np.where(cts > 1)[0]
    return inv, dup


def run(tag, v, f):
    op, nm, wd = strict(v, f)
    ml = mn.meshFromFacesVerts(np.asarray(f, np.int32), np.asarray(v, float))
    sx = mr.findSelfCollidingTriangles(mr.MeshPart(ml)).size()
    print(f"{tag:38s} open={op:4} NM={nm:5} wind={wd:6} selfX={sx:5}", flush=True)
    return nm, sx


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
print(f"{NP} pieces, eps={EPS}, target separation {TARGET*1000:.2f}mm, "
      f"cap {FRAC:.0%} of local thickness\n", flush=True)

t0 = time.time()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    t = wm.Tetrahedralizer(epsilon=EPS / diag, edge_length_r=0.05, coarsen=True,
                           max_its=0, stop_quality=10)
    t.set_mesh(V, F)
    t.tetrahedralize()
    o = t.get_tet_mesh(floodfill=True, manifold_surface=True,
                       correct_surface_orientation=True)
print(f"[tetrahedralize] {time.time()-t0:.0f}s\n", flush=True)
bv, bf = boundary(np.asarray(o[0], float), np.asarray(o[1]))

run("no nudge", bv, bf)

# uniform, for reference
nrm = vnormals(bv, bf)
inv, dup = coincident_groups(bv)
moving = np.isin(inv, dup)
v1 = bv.copy(); v1[moving] -= nrm[moving] * TARGET
run(f"uniform {TARGET*1000:.2f}mm", v1, bf)

# --- raycast-limited
tm = trimesh.Trimesh(bv, bf, process=False)
idxs = np.where(moving)[0]
org = bv[idxs] - nrm[idxs] * 1e-6              # start just inside
loc, ridx, _ = tm.ray.intersects_location(org, -nrm[idxs], multiple_hits=False)
thick = np.full(len(idxs), np.inf)
if len(ridx):
    thick[ridx] = np.linalg.norm(loc - org[ridx], axis=1)
print(f"\nlocal thickness at the {len(idxs)} split vertices: "
      f"median={np.median(thick[np.isfinite(thick)]):.3f}m  "
      f"p10={np.percentile(thick[np.isfinite(thick)],10):.4f}m  "
      f"below {TARGET*2*1000:.1f}mm: {int((thick < 2*TARGET).sum())}", flush=True)

allowed = np.minimum(TARGET, FRAC * thick)
v2 = bv.copy()
v2[idxs] -= nrm[idxs] * allowed[:, None]
run(f"raycast-capped ({FRAC:.0%} of thickness)", v2, bf)

# --- asymmetric: give each pair's shortfall to its thicker copy
v3 = bv.copy()
amt = allowed.copy()
pos_of = {}
for a, i in enumerate(idxs):
    pos_of.setdefault(inv[i], []).append(a)
nfix = 0
for g, members in pos_of.items():
    if len(members) < 2:
        continue
    tot = 2 * TARGET
    got = sum(amt[m] for m in members)
    if got < tot:
        room = [FRAC * thick[m] - amt[m] for m in members]
        order = np.argsort(-np.array(room))
        need = tot - got
        for oi in order:
            m = members[oi]
            give = min(room[oi], need)
            if give > 0:
                amt[m] += give; need -= give; nfix += 1
            if need <= 0:
                break
v3[idxs] -= nrm[idxs] * amt[:, None]
run(f"asymmetric (shortfall to thicker lobe)", v3, bf)
print(f"  ({nfix} copies given extra displacement)")
