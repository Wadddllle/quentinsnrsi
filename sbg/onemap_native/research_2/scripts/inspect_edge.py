"""Stop hypothesising. LOOK at what a bad edge actually is.

For a sample of the 4-face edges: dump the 4 faces' normals, the dihedral
arrangement around the edge, and whether the two sheets are back-to-back
(normals opposed => zero-thickness gap) or fold-like (normals aligned).
Renders a few local neighbourhoods too.
"""
import sys, os; sys.path.insert(0, "/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np, trimesh
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection

STL = os.environ.get("STL", "data/wt_raw_test/duxton_ftetwild_domain.stl")
m = trimesh.load(STL, process=False)
v = np.asarray(m.vertices, np.float32)
f = np.asarray(m.faces).astype(np.int64)
uq, inv = np.unique(v.view([('', np.float32)] * 3).ravel(), return_inverse=True)
va = uq.view(np.float32).reshape(-1, 3).astype(float)
n = np.int64(len(uq))
idx = inv.astype(np.int64)[f]

de = np.vstack([idx[:, [0, 1]], idx[:, [1, 2]], idx[:, [2, 0]]])
fid = np.tile(np.arange(len(f)), 3)
a, b = de[:, 0], de[:, 1]
key = np.minimum(a, b) * n + np.maximum(a, b)
order = np.argsort(key)
key_s, fid_s = key[order], fid[order]
uk, start, cnt = np.unique(key_s, return_index=True, return_counts=True)
bad = np.where(cnt > 2)[0]
print(f"{len(bad)} bad edges in {STL}\n")

fn = m.face_normals
rng = np.random.default_rng(0)
sample = rng.choice(bad, min(8, len(bad)), replace=False)

print(f"{'edge':>5} {'len':>7} {'vert?':>6} | pairwise angles between the 4 face normals (deg)")
for e in sample:
    fs = fid_s[start[e]:start[e] + cnt[e]]
    p0 = va[int(uk[e] // n)]; p1 = va[int(uk[e] % n)]
    L = np.linalg.norm(p1 - p0)
    vfrac = abs(p1[2] - p0[2]) / max(L, 1e-9)
    N = fn[fs]
    ang = []
    for i in range(len(N)):
        for j in range(i + 1, len(N)):
            ang.append(np.degrees(np.arccos(np.clip(np.dot(N[i], N[j]), -1, 1))))
    print(f"{e:5} {L:7.2f} {vfrac:6.2f} | " + " ".join(f"{x:5.0f}" for x in sorted(ang)))

# how many bad edges have a near-180 pair (back-to-back sheets)?
n180 = n0 = 0
for e in bad:
    fs = fid_s[start[e]:start[e] + cnt[e]]
    N = fn[fs]
    best180 = 0.0; best0 = 0.0
    for i in range(len(N)):
        for j in range(i + 1, len(N)):
            d = float(np.dot(N[i], N[j]))
            best180 = max(best180, -d); best0 = max(best0, d)
    if best180 > 0.95:
        n180 += 1
    if best0 > 0.95:
        n0 += 1
print(f"\nof {len(bad)} bad edges:")
print(f"  {n180} ({n180/len(bad):.0%}) have a pair of near-OPPOSED normals "
      f"(back-to-back sheets = zero-thickness gap)")
print(f"  {n0} ({n0/len(bad):.0%}) have a pair of near-PARALLEL normals "
      f"(coincident duplicate faces)")

# render 3 neighbourhoods
fig = plt.figure(figsize=(16, 5.5))
for k, e in enumerate(sample[:3]):
    p0 = va[int(uk[e] // n)]; p1 = va[int(uk[e] % n)]
    c = 0.5 * (p0 + p1); R = max(np.linalg.norm(p1 - p0) * 2.5, 3.0)
    near = np.where(np.linalg.norm(va[idx].mean(axis=1) - c, axis=1) < R)[0]
    ax = fig.add_subplot(1, 3, k + 1, projection="3d")
    tri = va[idx[near]]
    ax.add_collection3d(Poly3DCollection(tri, alpha=.45, facecolor="#c1440e",
                                         edgecolor="k", linewidths=.25))
    fs = fid_s[start[e]:start[e] + cnt[e]]
    ax.add_collection3d(Poly3DCollection(va[idx[fs]], alpha=.95,
                                         facecolor="#1f77b4", edgecolor="b"))
    ax.plot([p0[0], p1[0]], [p0[1], p1[1]], [p0[2], p1[2]], "g-", lw=3)
    for A in "xyz":
        getattr(ax, f"set_{A}lim")(c["xyz".index(A)] - R, c["xyz".index(A)] + R)
    ax.set_title(f"edge {e}  ({cnt[e]} faces, len {np.linalg.norm(p1-p0):.2f}m)",
                 fontsize=9)
    ax.set_xticks([]); ax.set_yticks([]); ax.set_zticks([])
fig.suptitle("blue = the 4 faces on the non-manifold edge, green = the edge itself")
fig.tight_layout()
out = "data/wt_raw_test/bad_edge_neighbourhoods.png"
fig.savefig(out, dpi=110)
print(f"\nwrote {out}")
