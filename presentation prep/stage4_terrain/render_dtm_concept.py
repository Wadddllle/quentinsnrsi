import sys
sys.path.insert(0, "/home/quentin/snrsi")
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D

from sbg.onemap_native.terrain import build_domain_dtm

OUT = "/home/quentin/snrsi/presentation prep/stage4_terrain"

# a real, already-used-elsewhere 900m domain with genuine relief (Queenstown area)
bbox = (25600, 28800, 26500, 29700)
grid_z, affine = build_domain_dtm(bbox, step=5.0)
print("grid shape:", grid_z.shape, "z range:", grid_z.min(), grid_z.max())

nrows, ncols = grid_z.shape
cx = affine.c + affine.a * (np.arange(ncols) + 0.5)
cy = affine.f + affine.e * (np.arange(nrows) + 0.5)
GX, GY = np.meshgrid(cx, cy)

fig = plt.figure(figsize=(15, 6))

ax1 = fig.add_subplot(1, 3, 1)
im = ax1.imshow(grid_z, extent=(cx.min(), cx.max(), cy.min(), cy.max()), origin="lower",
                cmap="terrain")
plt.colorbar(im, ax=ax1, label="elevation (m)", fraction=0.046)
ax1.set_title("DTM: a grid of elevation SAMPLES\n(top-down, 5m cells)")
ax1.set_xlabel("x (SVY21)"); ax1.set_ylabel("y (SVY21)")

ax2 = fig.add_subplot(1, 3, 2, projection="3d")
ax2.plot_surface(GX, GY, grid_z, cmap="terrain", linewidth=0, antialiased=True,
                 rcount=100, ccount=100)
ax2.set_title("same DTM, as a real surface\n(what conforming_terrain triangulates)")
ax2.set_zlim(grid_z.min()-5, grid_z.min()+120)
ax2.set_box_aspect((1, 1, 0.35))
ax2.view_init(elev=35, azim=-60)

# sparse scatter -- what the DTM actually IS before gridding: sparse SLA contour
# points, interpolated. Show a coarse version to make "it's samples, not truth" visible.
ax3 = fig.add_subplot(1, 3, 3, projection="3d")
step = 8
ax3.plot_wireframe(GX[::step, ::step], GY[::step, ::step], grid_z[::step, ::step],
                   color="0.3", linewidth=0.5)
ax3.scatter(GX[::step, ::step], GY[::step, ::step], grid_z[::step, ::step],
           c=grid_z[::step, ::step], cmap="terrain", s=8)
ax3.set_title("the grid IS the elevation samples\n(a coarser view -- these are what get\ninterpolated + triangulated)")
ax3.set_zlim(grid_z.min()-5, grid_z.min()+120)
ax3.set_box_aspect((1, 1, 0.35))
ax3.view_init(elev=35, azim=-60)

plt.tight_layout()
fig.savefig(f"{OUT}/dtm_concept.png", dpi=150, facecolor="white")
print("wrote dtm_concept.png")
