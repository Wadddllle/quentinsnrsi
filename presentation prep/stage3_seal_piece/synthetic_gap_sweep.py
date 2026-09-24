"""Follow-up controlled test after the thickness sweep came back negative
(meshlib's offsetMesh reproduced an isolated thin wall's exact thickness down
to 0.1m at a 2.0m voxel -- no rounding, no battlement). The real MD1 crop
showed adjacent parapet FINS merging into one mound, which is a GAP-between-
features effect, not a single-wall-thickness effect. Test that directly: two
teeth on a shared base, fixed tooth width/thickness, ONLY the gap between them
varies, fixed 2.0m voxel throughout."""
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
TOOTH_W, TOOTH_T, TOOTH_H = 4.0, 1.0, 4.0   # width, thickness(y), height above base
BASE_H = 3.0
GAPS = [2.0, 1.5, 1.0, 0.75, 0.5, 0.25, 0.1]


def offset_remesh(V, F, voxel):
    m = mn.meshFromFacesVerts(np.asarray(F, dtype=np.int32), np.asarray(V, dtype=np.float64))
    params = mr.OffsetParameters()
    params.voxelSize = voxel
    params.signDetectionMode = mr.SignDetectionMode.OpenVDB
    out = mr.offsetMesh(m, 0.0, params)
    return mn.getNumpyVerts(out), mn.getNumpyFaces(out.topology)


def build_scene(gap):
    total_w = 2 * TOOTH_W + gap
    base = trimesh.creation.box(extents=[total_w + 4, TOOTH_T, BASE_H])
    base.apply_translation([0, 0, BASE_H / 2])
    t1 = trimesh.creation.box(extents=[TOOTH_W, TOOTH_T, TOOTH_H])
    t1.apply_translation([-(gap / 2 + TOOTH_W / 2), 0, BASE_H + TOOTH_H / 2])
    t2 = trimesh.creation.box(extents=[TOOTH_W, TOOTH_T, TOOTH_H])
    t2.apply_translation([(gap / 2 + TOOTH_W / 2), 0, BASE_H + TOOTH_H / 2])
    scene = trimesh.util.concatenate([base, t1, t2])
    gap_probe = np.array([0.0, 0.0, BASE_H + TOOTH_H / 2])  # should be OPEN AIR
    return scene, gap_probe, total_w + 4


def render(ax, V, F, title, color, span, gap_probe=None, filled=None):
    tris = V[F] if len(F) else np.zeros((0, 3, 3))
    if len(F):
        pc = Poly3DCollection(tris, facecolor=color, edgecolor="k", linewidths=0.15)
        ax.add_collection3d(pc)
    ax.set_xlim(-span / 2, span / 2); ax.set_ylim(-2, 2); ax.set_zlim(0, BASE_H + TOOTH_H + 1)
    ax.set_box_aspect((span, 4, BASE_H + TOOTH_H + 1))
    if gap_probe is not None:
        c = "red" if filled else "lime"
        ax.scatter(*gap_probe, color=c, s=60, depthshade=False)
    ax.set_title(title, fontsize=9)
    ax.set_axis_off()


fig, axes = plt.subplots(2, len(GAPS), figsize=(3 * len(GAPS), 6),
                         subplot_kw={"projection": "3d"})

results = []
for i, gap in enumerate(GAPS):
    scene, probe, span = build_scene(gap)
    V0, F0 = np.asarray(scene.vertices), np.asarray(scene.faces)
    rv, rf = offset_remesh(V0, F0, VOXEL)
    rt = trimesh.Trimesh(rv, rf, process=True)  # process=True to get a real watertight query mesh
    filled = bool(rt.contains([probe])[0]) if rt.is_watertight else None
    if filled is None:
        # fall back: nearest-surface signed distance sign via proximity
        pq = trimesh.proximity.ProximityQuery(rt)
        sd = pq.signed_distance([probe])[0]
        filled = bool(sd > 0)
    print(f"gap={gap:>4.2f}m  half_voxel={VOXEL/2:.1f}m  gap_probe_is_SOLID(filled)={filled}  "
          f"remeshed_faces={len(rf)}")
    results.append({"gap_m": gap, "gap_collapsed_into_solid": filled, "faces": int(len(rf))})
    render(axes[0, i], V0, F0, f"gap={gap}m (original)", "#8fbfe0", span, probe, False)
    render(axes[1, i], rv, rf, f"remeshed @ {VOXEL}m voxel\ngap {'COLLAPSED' if filled else 'preserved'}",
          "#e0958f" if filled else "#5cb85c", span, probe, filled)

plt.tight_layout()
fig.savefig(f"{OUT}/synthetic_gap_sweep.png", dpi=150, facecolor="white")
print("wrote synthetic_gap_sweep.png")

import json
with open(f"{OUT}/synthetic_gap_manifest.json", "w") as f:
    json.dump({"voxel_size_m": VOXEL, "tooth_width_m": TOOTH_W, "tooth_thickness_m": TOOTH_T,
               "half_voxel_m": VOXEL / 2, "results": results}, f, indent=2)
print("wrote synthetic_gap_manifest.json")
