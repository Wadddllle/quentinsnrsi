import sys, os
sys.path.insert(0, "/home/quentin/snrsi")
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection

out_dir = "/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583/scratchpad/md1_area"

def load_obj(path):
    v, f = [], []
    for line in open(path):
        if line.startswith("v "):
            v.append([float(x) for x in line.split()[1:4]])
        elif line.startswith("f "):
            f.append([int(x.split("/")[0]) - 1 for x in line.split()[1:4]])
    return np.array(v), np.array(f)

TARGET = np.array([22564.79267465497, 30648.979156176567])

files = ["00_tile9_0_batch0.obj", "01_tile18_0_batch0.obj", "02_tile18_0_batch1.obj",
         "03_tile18_0_batch2.obj", "04_tile17_0_batch0.obj"]
colors = ["tab:red", "tab:blue", "tab:green", "tab:orange", "tab:purple"]

# --- Top-down overview, all 5 pieces, colored + labeled ---
fig, ax = plt.subplots(figsize=(10, 10))
for fn, col in zip(files, colors):
    v, f = load_obj(os.path.join(out_dir, fn))
    for tri in f:
        pts = v[tri][:, :2]
        ax.fill(np.append(pts[:,0], pts[0,0]), np.append(pts[:,1], pts[0,1]),
                color=col, alpha=0.15, edgecolor=None)
    cen = v[:, :2].mean(axis=0)
    ax.annotate(fn.split("_tile")[0], cen, color=col, fontsize=12, weight="bold")
ax.scatter(*TARGET, color="black", marker="x", s=200, linewidths=3, zorder=10, label="user's target point")
ax.set_aspect("equal")
ax.legend()
ax.set_title("Top-down: all pieces near target (22564.8, 30649.0)")
fig.savefig(os.path.join(out_dir, "overview_topdown.png"), dpi=140)
plt.close(fig)

# --- Piece 00 (the big one) oblique 3D render ---
v0, f0 = load_obj(os.path.join(out_dir, "00_tile9_0_batch0.obj"))
fig = plt.figure(figsize=(12, 10))
ax = fig.add_subplot(111, projection="3d")
tris = v0[f0]
pc = Poly3DCollection(tris, facecolor="tab:red", edgecolor="k", linewidths=0.05, alpha=0.9)
ax.add_collection3d(pc)
ax.set_xlim(v0[:,0].min(), v0[:,0].max())
ax.set_ylim(v0[:,1].min(), v0[:,1].max())
ax.set_zlim(v0[:,2].min(), v0[:,2].max())
ax.set_box_aspect((np.ptp(v0[:,0]), np.ptp(v0[:,1]), np.ptp(v0[:,2])))
ax.view_init(elev=25, azim=-60)
ax.set_title("Piece 00 (batch0, tile 9_0) -- closest to target, 15.4m away")
fig.savefig(os.path.join(out_dir, "piece00_oblique.png"), dpi=140)
plt.close(fig)

ax.view_init(elev=80, azim=-60)
fig2 = plt.figure(figsize=(12,10))
ax2 = fig2.add_subplot(111, projection="3d")
pc2 = Poly3DCollection(tris, facecolor="tab:red", edgecolor="k", linewidths=0.05, alpha=0.9)
ax2.add_collection3d(pc2)
ax2.set_xlim(v0[:,0].min(), v0[:,0].max())
ax2.set_ylim(v0[:,1].min(), v0[:,1].max())
ax2.set_zlim(v0[:,2].min(), v0[:,2].max())
ax2.set_box_aspect((np.ptp(v0[:,0]), np.ptp(v0[:,1]), np.ptp(v0[:,2])))
ax2.view_init(elev=85, azim=-60)
ax2.set_title("Piece 00 top-down")
fig2.savefig(os.path.join(out_dir, "piece00_topdown.png"), dpi=140)
plt.close(fig2)

print("wrote", os.path.join(out_dir, "overview_topdown.png"))
print("wrote", os.path.join(out_dir, "piece00_oblique.png"))
print("wrote", os.path.join(out_dir, "piece00_topdown.png"))
