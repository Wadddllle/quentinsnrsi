import sys
sys.path.insert(0, "/home/quentin/snrsi")
import numpy as np
import triangle as tr
from scipy.spatial import Delaunay
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

OUT = "/home/quentin/snrsi/presentation prep/stage4_terrain"

rng = np.random.default_rng(3)
bg = rng.uniform(2, 38, size=(110, 2))
footprint = np.array([[16,16],[24,15],[27,20],[24,26],[17,25],[13,20]], dtype=float)
boundary = np.array([[0,0],[40,0],[40,40],[0,40]], dtype=float)  # explicit domain edge --
# required: 'p' mode with no explicit outer boundary silently returns almost no
# triangles (a real bug this project already hit -- see terrain.py/conforming_terrain,
# which always adds domain_polygon's own ring as segments for exactly this reason).

fig, axes = plt.subplots(1, 3, figsize=(16, 5.5))

# --- Panel 1: plain (unconstrained) Delaunay, footprint drawn on top only ---
ax = axes[0]
d = Delaunay(bg)
ax.triplot(bg[:,0], bg[:,1], d.simplices, color="0.6", linewidth=0.6)
ax.plot(*np.vstack([footprint, footprint[:1]]).T, "r-", linewidth=2)
ax.set_title("plain Delaunay, footprint ignored\n(terrain triangles cut straight through\nwhere the building actually stands)")
ax.set_aspect("equal"); ax.set_xticks([]); ax.set_yticks([])

def build(pts_extra, segs_extra, holes=None):
    all_pts = np.vstack([boundary, bg, pts_extra])
    nb = len(boundary)
    bsegs = [[i, (i+1) % nb] for i in range(nb)]
    segs = bsegs + [[nb+len(bg)+a, nb+len(bg)+b] for a, b in segs_extra]
    d = {"vertices": all_pts, "segments": np.array(segs)}
    if holes:
        d["holes"] = holes
    return tr.triangulate(d, "p")

fseg = [(i, (i+1) % len(footprint)) for i in range(len(footprint))]

# --- Panel 2: footprint ring as a CONSTRAINT (no hole yet -- still triangulated inside) ---
d2 = build(footprint, fseg)
ax = axes[1]
ax.triplot(d2["vertices"][:,0], d2["vertices"][:,1], d2["triangles"], color="0.6", linewidth=0.6)
ax.plot(*np.vstack([footprint, footprint[:1]]).T, "r-", linewidth=2)
ax.set_title("constrained Delaunay ('p' flag)\nfootprint edges are now forced triangle\nedges -- terrain vertices land EXACTLY on them")
ax.set_aspect("equal"); ax.set_xticks([]); ax.set_yticks([])

# --- Panel 3: + footprint interior held out as a hole (own flat pad, capped separately) ---
d3 = build(footprint, fseg, holes=[footprint.mean(axis=0).tolist()])
ax = axes[2]
ax.triplot(d3["vertices"][:,0], d3["vertices"][:,1], d3["triangles"], color="0.6", linewidth=0.6)
ax.fill(*footprint.T, color="#e0958f", alpha=0.6)
ax.plot(*np.vstack([footprint, footprint[:1]]).T, "r-", linewidth=2)
ax.set_title("+ footprint interior as a HOLE\nterrain stops exactly at the wall; the flat\nbuilding pad (pink) is capped separately")
ax.set_aspect("equal"); ax.set_xticks([]); ax.set_yticks([])

plt.tight_layout()
fig.savefig(f"{OUT}/constrained_delaunay_concept.png", dpi=150, facecolor="white")
print("wrote constrained_delaunay_concept.png")
