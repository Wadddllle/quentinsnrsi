import numpy as np
import trimesh
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection

out_dir = "/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583/scratchpad/trace"

REGIONS = {
    "MD1": (22500, 30590, 22630, 30710),       # generous box around MD1
    "NEIGHBOR": (22380, 30490, 22510, 30610),  # generous box around neighbor
}

STAGES = ["stage2_joined_soup", "stage3_voxel_remeshed"]

def crop(mesh, bbox):
    xmin, ymin, xmax, ymax = bbox
    v = mesh.vertices
    f = mesh.faces
    cen = v[f].mean(axis=1)
    keep = (cen[:,0] >= xmin) & (cen[:,0] <= xmax) & (cen[:,1] >= ymin) & (cen[:,1] <= ymax)
    return v, f[keep]

for stage in STAGES:
    print(f"loading {stage}...")
    m = trimesh.load(f"{out_dir}/{stage}.stl", process=False)
    for region, bbox in REGIONS.items():
        v, f = crop(m, bbox)
        if len(f) == 0:
            print(f"  {region}/{stage}: NO FACES in region")
            continue
        fig = plt.figure(figsize=(11, 9))
        ax = fig.add_subplot(111, projection="3d")
        tris = v[f]
        pc = Poly3DCollection(tris, facecolor="tab:cyan" if "soup" in stage else "tab:red",
                               edgecolor="k", linewidths=0.03, alpha=0.9)
        ax.add_collection3d(pc)
        used_v = v[np.unique(f)]
        ax.set_xlim(used_v[:,0].min(), used_v[:,0].max())
        ax.set_ylim(used_v[:,1].min(), used_v[:,1].max())
        ax.set_zlim(used_v[:,2].min(), used_v[:,2].max())
        ax.set_box_aspect((np.ptp(used_v[:,0]), np.ptp(used_v[:,1]), max(np.ptp(used_v[:,2]),1)))
        ax.view_init(elev=20, azim=-60)
        ax.set_title(f"{region} @ {stage}  (faces={len(f)})")
        fig.savefig(f"{out_dir}/render_{region}_{stage}.png", dpi=130)
        plt.close(fig)
        print(f"  {region}/{stage}: {len(f)} faces, z=[{used_v[:,2].min():.1f},{used_v[:,2].max():.1f}] -> render_{region}_{stage}.png")
print("done")
