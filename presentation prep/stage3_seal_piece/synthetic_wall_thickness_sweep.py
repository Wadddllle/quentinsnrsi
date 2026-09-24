"""Controlled, single-variable demo: one flat wall (box), varying ONLY its
thickness, voxel-remeshed at a FIXED 2.0m voxel size every time. No MD1, no
sealing, no confounds -- just: how much of a thin wall survives a fixed-size
voxel grid."""
import sys
sys.path.insert(0, "/home/quentin/snrsi")

import numpy as np
import trimesh
import meshlib.mrmeshpy as mr
import meshlib.mrmeshnumpy as mn
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection

OUT = "/home/quentin/snrsi/presentation prep/stage3_seal_piece"
VOXEL = 2.0
LENGTH, HEIGHT = 40.0, 20.0  # big relative to the voxel so ONLY thickness is marginal
THICKNESSES = [2.0, 1.5, 1.0, 0.75, 0.5, 0.25, 0.1]


def offset_remesh(V, F, voxel):
    m = mn.meshFromFacesVerts(np.asarray(F, dtype=np.int32), np.asarray(V, dtype=np.float64))
    params = mr.OffsetParameters()
    params.voxelSize = voxel
    params.signDetectionMode = mr.SignDetectionMode.OpenVDB
    out = mr.offsetMesh(m, 0.0, params)
    return mn.getNumpyVerts(out), mn.getNumpyFaces(out.topology)


def render(ax, V, F, title, color):
    if len(F) == 0:
        ax.set_title(title + "\n(NOTHING LEFT)", fontsize=9, color="red")
        ax.set_xlim(0, LENGTH); ax.set_ylim(-1.5, 1.5); ax.set_zlim(0, HEIGHT)
        ax.set_box_aspect((LENGTH, 3, HEIGHT))
        ax.set_axis_off()
        return
    tris = V[F]
    pc = Poly3DCollection(tris, facecolor=color, edgecolor="k", linewidths=0.2)
    ax.add_collection3d(pc)
    ax.set_xlim(0, LENGTH); ax.set_ylim(-1.5, 1.5); ax.set_zlim(0, HEIGHT)
    ax.set_box_aspect((LENGTH, 3, HEIGHT))
    ax.set_title(title, fontsize=9)
    ax.set_axis_off()


fig, axes = plt.subplots(2, len(THICKNESSES), figsize=(3 * len(THICKNESSES), 6),
                         subplot_kw={"projection": "3d"})

results = []
for i, t in enumerate(THICKNESSES):
    box = trimesh.creation.box(extents=[LENGTH, t, HEIGHT])
    box.apply_translation([LENGTH / 2, 0, HEIGHT / 2])  # sit on z=0, centered on y=0
    V0, F0 = np.asarray(box.vertices), np.asarray(box.faces)
    true_vol = box.volume

    rv, rf = offset_remesh(V0, F0, VOXEL)
    if len(rf):
        rt = trimesh.Trimesh(rv, rf, process=False)
        rvol = rt.volume
        try:
            bodies = rt.body_count
        except Exception:
            bodies = -1
    else:
        rvol, bodies = 0.0, 0

    pct = 100 * rvol / true_vol if true_vol else 0
    bbox_str = ""
    if len(rf):
        bb = rv.max(0) - rv.min(0)
        bbox_str = f"  recon_bbox=({bb[0]:.2f},{bb[1]:.2f},{bb[2]:.2f})"
    print(f"thickness={t:>4.2f}m  half_voxel={VOXEL/2:.1f}m  true_vol={true_vol:6.2f} m3  "
          f"remeshed_vol={rvol:6.2f} m3 ({pct:5.1f}%)  bodies={bodies}  faces={len(rf)}{bbox_str}")
    results.append({"thickness_m": t, "true_volume_m3": float(true_vol),
                    "remeshed_volume_m3": float(rvol), "pct_recovered": float(pct),
                    "bodies": int(bodies), "faces": int(len(rf))})

    render(axes[0, i], V0, F0, f"t={t}m (original)", "#8fbfe0")
    render(axes[1, i], rv, rf, f"remeshed @ {VOXEL}m voxel\n{pct:.0f}% volume, {bodies} body", "#e0958f")

plt.tight_layout()
fig.savefig(f"{OUT}/synthetic_wall_thickness_sweep.png", dpi=150, facecolor="white")
print("wrote synthetic_wall_thickness_sweep.png")

import json
(OUT + "/synthetic_wall_manifest.json") if False else None
with open(f"{OUT}/synthetic_wall_manifest.json", "w") as f:
    json.dump({"voxel_size_m": VOXEL, "wall_length_m": LENGTH, "wall_height_m": HEIGHT,
               "half_voxel_m": VOXEL / 2, "results": results}, f, indent=2)
print("wrote synthetic_wall_manifest.json")
