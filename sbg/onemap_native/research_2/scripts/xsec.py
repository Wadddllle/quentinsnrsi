"""Horizontal cross-sections of RAW vs DMC vs fTetWild.

The castle/battlement artifact is a PLAN-VIEW phenomenon -- a continuous wall
becoming pillar-gap-pillar -- so a horizontal slice shows it far more clearly
than any 3D view.
"""
import os; os.chdir("/home/quentin/snrsi")
import numpy as np, trimesh
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt

P = os.environ.get("PIECE", "26")
D = "data/wt_raw_test"
MESHES = [("RAW sealed", f"{D}/P{P}_RAW_sealed.stl"),
          ("DMC voxel 0.5", f"{D}/P{P}_dmc_vs0.5.stl"),
          ("DMC voxel 2.0", f"{D}/P{P}_dmc_vs2.0.stl"),
          ("fTetWild eps=0.05", f"{D}/P{P}_ftetwild_eps0.05.stl")]

loaded = [(n, trimesh.load(p, process=False)) for n, p in MESHES if os.path.exists(p)]
zlo, zhi = loaded[0][1].bounds[0][2], loaded[0][1].bounds[1][2]
FRACS = [0.15, 0.40, 0.65, 0.90]
ZS = [zlo + fr * (zhi - zlo) for fr in FRACS]

fig, axes = plt.subplots(len(ZS), len(loaded),
                         figsize=(4.2 * len(loaded), 4.0 * len(ZS)))
for r, z in enumerate(ZS):
    for c, (name, m) in enumerate(loaded):
        ax = axes[r, c]
        try:
            sec = m.section(plane_origin=[0, 0, z], plane_normal=[0, 0, 1])
            if sec is not None:
                for ent in sec.entities:
                    pts = sec.vertices[ent.points]
                    ax.plot(pts[:, 0], pts[:, 1], lw=0.9, color="#c1440e")
        except Exception as e:
            ax.text(0.5, 0.5, f"section failed\n{e}", ha="center",
                    transform=ax.transAxes, fontsize=7)
        ax.set_aspect("equal")
        ax.set_xlim(loaded[0][1].bounds[0][0] - 2, loaded[0][1].bounds[1][0] + 2)
        ax.set_ylim(loaded[0][1].bounds[0][1] - 2, loaded[0][1].bounds[1][1] + 2)
        ax.set_xticks([]); ax.set_yticks([])
        if r == 0:
            ax.set_title(name, fontsize=11)
        if c == 0:
            ax.set_ylabel(f"z = {z:.1f} m", fontsize=10)

fig.suptitle(f"piece {P}: horizontal cross-sections "
             f"(a continuous wall that becomes dashes = the aliasing artifact)",
             fontsize=12)
fig.tight_layout()
out = f"{D}/P{P}_xsections.png"
fig.savefig(out, dpi=110)
print("wrote", out)
