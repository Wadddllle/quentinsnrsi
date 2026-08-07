import sys
import numpy as np
import trimesh
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection

out_dir = "/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583/scratchpad/md1_area"

for name in ["00_raw", "00_dedup"]:
    m = trimesh.load(f"{out_dir}/ab_{name}.stl", process=False)
    fig = plt.figure(figsize=(12, 10))
    ax = fig.add_subplot(111, projection="3d")
    pc = Poly3DCollection(m.vertices[m.faces], facecolor="tab:red", edgecolor="k", linewidths=0.05, alpha=0.9)
    ax.add_collection3d(pc)
    v = m.vertices
    ax.set_xlim(v[:,0].min(), v[:,0].max())
    ax.set_ylim(v[:,1].min(), v[:,1].max())
    ax.set_zlim(v[:,2].min(), v[:,2].max())
    ax.set_box_aspect((np.ptp(v[:,0]), np.ptp(v[:,1]), np.ptp(v[:,2])))
    ax.view_init(elev=20, azim=-60)
    ax.set_title(f"post voxel-remesh (2m, no solidify): {name}")
    fig.savefig(f"{out_dir}/render_{name}_a.png", dpi=140)
    ax.view_init(elev=20, azim=120)
    fig.savefig(f"{out_dir}/render_{name}_b.png", dpi=140)
    plt.close(fig)
    print(f"{name}: verts={len(m.vertices)} faces={len(m.faces)} watertight(trimesh)={m.is_watertight}")

print("done")
