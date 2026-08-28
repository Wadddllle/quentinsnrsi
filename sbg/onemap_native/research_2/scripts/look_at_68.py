"""The minimal reproducer: pieces 6 + 8. Alone -> 0 NM. Together -> 12 NM.
Dump both inputs and the output, locate the 12 bad edges, and RENDER it.
"""
import sys, os; sys.path.insert(0, "/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np, trimesh, io, contextlib
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
import wildmeshing as wm
from shapely.geometry import box
from pyproj import Transformer
from sbg.onemap_native.tiles import domain_leaf_tiles
from sbg.onemap_native.extract import extract_domain_buildings, seal_piece

B = (28941, 28758, 29341, 29158); EPS = 0.10
OUT = "data/wt_raw_test"
IDS = [int(x) for x in os.environ.get("IDS", "6,8").split(",")]

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
S = S[:10]
ctr = np.vstack([v for v, _ in S]).mean(axis=0)
S = [(v - ctr, f) for v, f in S]

V, F, off = [], [], 0
for i in IDS:
    v, f = S[i]
    V.append(v); F.append(f + off); off += len(v)
    trimesh.Trimesh(v, f, process=False).export(f"{OUT}/R_in_piece{i}.stl")
    d = v.max(axis=0) - v.min(axis=0)
    print(f"piece {i}: {len(f):4d} faces, bbox {d[0]:6.1f} x {d[1]:6.1f} x {d[2]:6.1f} m")
V = np.vstack(V); F = np.vstack(F).astype(np.int32)

diag = float(np.linalg.norm(V.max(axis=0) - V.min(axis=0)))
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    t = wm.Tetrahedralizer(epsilon=EPS / diag, edge_length_r=0.05, coarsen=True,
                           max_its=0, stop_quality=10)
    t.set_mesh(V, F); t.tetrahedralize()
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
used = np.unique(bf); rm = np.full(len(tv), -1, np.int64); rm[used] = np.arange(len(used))
bv, bff = tv[used], rm[bf]
trimesh.Trimesh(bv, bff, process=False).export(f"{OUT}/R_out_{'_'.join(map(str,IDS))}.stl")

v32 = np.asarray(bv, np.float32)
uq, inv = np.unique(v32.view([('', np.float32)] * 3).ravel(), return_inverse=True)
va = uq.view(np.float32).reshape(-1, 3).astype(float)
n = np.int64(len(uq)); ii = inv.astype(np.int64)[bff]
de = np.vstack([ii[:, [0, 1]], ii[:, [1, 2]], ii[:, [2, 0]]])
fid = np.tile(np.arange(len(bff)), 3)
a, b = de[:, 0], de[:, 1]
key = np.minimum(a, b) * n + np.maximum(a, b)
o2 = np.argsort(key); ks, fs_ = key[o2], fid[o2]
uk, st, ct = np.unique(ks, return_index=True, return_counts=True)
bad = np.where(ct > 2)[0]
print(f"\noutput: {len(bff):,} faces, {len(bad)} non-manifold edges")

fnm = trimesh.Trimesh(bv, bff, process=False).face_normals
for e in bad:
    f4 = fs_[st[e]:st[e] + ct[e]]
    p0 = va[int(uk[e] // n)]; p1 = va[int(uk[e] % n)]
    L = np.linalg.norm(p1 - p0)
    ang = sorted(np.degrees(np.arccos(np.clip(
        [np.dot(fnm[f4[i]], fnm[f4[j]]) for i in range(len(f4)) for j in range(i + 1, len(f4))],
        -1, 1))))
    print(f"  edge len {L:6.2f}m  vert={abs(p1[2]-p0[2])/max(L,1e-9):.2f}  "
          f"z={0.5*(p0[2]+p1[2]):7.2f}  angles " + " ".join(f"{x:3.0f}" for x in ang))

fig = plt.figure(figsize=(17, 6))
cols = ["#c1440e", "#1f77b4"]
ax = fig.add_subplot(131, projection="3d")
for c, i in enumerate(IDS):
    v, f = S[i]
    ax.add_collection3d(Poly3DCollection(v[f], alpha=.5, facecolor=cols[c],
                                         edgecolor="k", linewidths=.3))
ax.set_title(f"INPUT: pieces {IDS[0]} (orange) + {IDS[1]} (blue)", fontsize=10)
ax2 = fig.add_subplot(132, projection="3d")
ax2.add_collection3d(Poly3DCollection(bv[bff], alpha=.4, facecolor="#888",
                                      edgecolor="k", linewidths=.2))
for e in bad:
    p0 = va[int(uk[e] // n)]; p1 = va[int(uk[e] % n)]
    ax2.plot([p0[0], p1[0]], [p0[1], p1[1]], [p0[2], p1[2]], "r-", lw=3)
ax2.set_title(f"OUTPUT, {len(bad)} bad edges in RED", fontsize=10)
ax3 = fig.add_subplot(133, projection="3d")
e0 = bad[0]
p0 = va[int(uk[e0] // n)]; p1 = va[int(uk[e0] % n)]
c0 = 0.5 * (p0 + p1); R = max(np.linalg.norm(p1 - p0) * 3, 4.0)
cen = bv[bff].mean(axis=1)
nearm = np.linalg.norm(cen - c0, axis=1) < R
ax3.add_collection3d(Poly3DCollection(bv[bff][nearm], alpha=.5, facecolor="#888",
                                      edgecolor="k", linewidths=.4))
f4 = fs_[st[e0]:st[e0] + ct[e0]]
ax3.add_collection3d(Poly3DCollection(bv[bff][f4], alpha=.9, facecolor="#1f77b4"))
ax3.plot([p0[0], p1[0]], [p0[1], p1[1]], [p0[2], p1[2]], "r-", lw=4)
ax3.set_title("ZOOM on one bad edge (blue = its 4 faces)", fontsize=10)
for A in (ax, ax2):
    lim = np.vstack([v for v, _ in [S[i] for i in IDS]])
    for j, X in enumerate("xyz"):
        getattr(A, f"set_{X}lim")(lim[:, j].min(), lim[:, j].max())
for j, X in enumerate("xyz"):
    getattr(ax3, f"set_{X}lim")(c0[j] - R, c0[j] + R)
for A in (ax, ax2, ax3):
    A.set_xticks([]); A.set_yticks([]); A.set_zticks([])
fig.tight_layout()
fig.savefig(f"{OUT}/R_repro_{'_'.join(map(str,IDS))}.png", dpi=115)
print(f"\nwrote {OUT}/R_repro_{'_'.join(map(str,IDS))}.png")
print(f"      {OUT}/R_in_piece*.stl  {OUT}/R_out_*.stl")
