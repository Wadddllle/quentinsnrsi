"""Find the MINIMAL subset of real pieces (at REAL positions) that produces a
non-manifold edge.

10 real pieces together -> 40 NM. Every constructed pair -> 0 NM. So run all
singles, then all pairs, then grow. Whatever the smallest failing set is, that is
the reproducer to actually look at.
"""
import sys, os; sys.path.insert(0, "/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np, io, contextlib, itertools, time
import wildmeshing as wm

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shapely.geometry import box
from pyproj import Transformer
from sbg.onemap_native.tiles import domain_leaf_tiles
from sbg.onemap_native.extract import extract_domain_buildings, seal_piece

B = (28941, 28758, 29341, 29158)
EPS = 0.10
NP = int(os.environ.get("NP", "10"))


def nm_of(V, F):
    diag = float(np.linalg.norm(V.max(axis=0) - V.min(axis=0)))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        t = wm.Tetrahedralizer(epsilon=EPS / diag, edge_length_r=0.05, coarsen=True,
                               max_its=0, stop_quality=10)
        t.set_mesh(V, F.astype(np.int32))
        t.tetrahedralize()
        o = t.get_tet_mesh(floodfill=True, manifold_surface=True,
                           correct_surface_orientation=True)
    tv, tt = np.asarray(o[0], float), np.asarray(o[1])
    q = np.vstack([tt[:, [0, 1, 2]], tt[:, [0, 1, 3]], tt[:, [0, 2, 3]], tt[:, [1, 2, 3]]])
    opp = np.concatenate([tt[:, 3], tt[:, 2], tt[:, 1], tt[:, 0]])
    _, idx, cnt = np.unique(np.sort(q, axis=1), axis=0, return_index=True, return_counts=True)
    k = idx[cnt == 1]; bf = q[k]
    a_, b_, c_ = tv[bf[:, 0]], tv[bf[:, 1]], tv[bf[:, 2]]
    fl = np.einsum('ij,ij->i', np.cross(b_ - a_, c_ - a_), tv[opp[k]] - a_) > 0
    bf[fl] = bf[fl][:, [0, 2, 1]]
    used = np.unique(bf)
    rm = np.full(len(tv), -1, np.int64); rm[used] = np.arange(len(used))
    bv, bf = tv[used], rm[bf]
    v32 = np.asarray(bv, np.float32)
    uq, inv = np.unique(v32.view([('', np.float32)] * 3).ravel(), return_inverse=True)
    i = inv.astype(np.int64)[bf]
    i = i[(i[:, 0] != i[:, 1]) & (i[:, 1] != i[:, 2]) & (i[:, 0] != i[:, 2])]
    n = np.int64(len(uq))
    d = np.vstack([i[:, [0, 1]], i[:, [1, 2]], i[:, [2, 0]]])
    a, b = d[:, 0], d[:, 1]
    _, c = np.unique(np.minimum(a, b) * n + np.maximum(a, b), return_counts=True)
    return int((c > 2).sum())


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
S = [(v - ctr, f) for v, f in S]      # REAL relative positions preserved


def build(ids):
    V, F, off = [], [], 0
    for i in ids:
        v, f = S[i]
        V.append(v); F.append(f + off); off += len(v)
    return np.vstack(V), np.vstack(F)


t0 = time.time()
print(f"{len(S)} real pieces at REAL positions, eps={EPS}\n")
all_ids = list(range(len(S)))
print(f"ALL {len(S)} together -> NM = {nm_of(*build(all_ids))}\n")

print("singles:")
bad_single = []
for i in all_ids:
    k = nm_of(*build([i]))
    if k:
        bad_single.append(i)
    print(f"   [{i}] NM={k}", end="   " if (i + 1) % 5 else "\n")
print()

print("\npairs with NM > 0:")
badpairs = []
for i, j in itertools.combinations(all_ids, 2):
    k = nm_of(*build([i, j]))
    base = 0
    if k > base:
        badpairs.append((i, j, k))
        print(f"   ({i},{j}) NM={k}")
if not badpairs:
    print("   NONE -- no pair reproduces it")

print(f"\ntriples with NM > 0 (first 15 found):")
found = 0
for c in itertools.combinations(all_ids, 3):
    k = nm_of(*build(list(c)))
    if k:
        print(f"   {c} NM={k}")
        found += 1
        if found >= 15:
            break
if not found:
    print("   NONE -- no triple reproduces it either")
print(f"\n[{time.time()-t0:.0f}s]")
