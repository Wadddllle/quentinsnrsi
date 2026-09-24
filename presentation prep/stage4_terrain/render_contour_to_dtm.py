import sys
sys.path.insert(0, "/home/quentin/snrsi")
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from sbg.topo.dtm import load_points, build_dtm

OUT = "/home/quentin/snrsi/presentation prep/stage4_terrain"

# same window as dtm_concept.png, with a margin so real contour points around
# the domain are visible too
margin = 400
bbox = (25600 - margin, 28800 - margin, 26500 + margin, 29700 + margin)
xs, ys, zs = load_points(bbox=bbox)
print(f"{len(xs)} raw contour points in this window, elevations {zs.min()}-{zs.max()}m, "
     f"unique elevation values: {sorted(set(zs.tolist()))[:15]}...")

grid_z, transform = build_dtm(xs, ys, zs, step=5.0)
# transform is rasterio's `from_origin` Affine; matches terrain.py's own convention
nrows, ncols = grid_z.shape
cx = transform.c + transform.a * (np.arange(ncols) + 0.5)
cy = transform.f + transform.e * (np.arange(nrows) + 0.5)

fig, axes = plt.subplots(1, 3, figsize=(16, 5.5))

ax = axes[0]
sca = ax.scatter(xs, ys, c=zs, s=2, cmap="terrain")
plt.colorbar(sca, ax=ax, label="elevation (m)", fraction=0.046)
ax.set_title(f"1. raw contour-LINE vertices\nstreamed from NationalMapLine.geojson\n({len(xs)} points, each at its own\ncontour's fixed elevation)")
ax.set_aspect("equal"); ax.set_xlabel("x (SVY21)"); ax.set_ylabel("y (SVY21)")

ax = axes[1]
im = ax.imshow(grid_z, extent=(cx.min(), cx.max(), cy.min(), cy.max()), origin="upper",
               cmap="terrain")
plt.colorbar(im, ax=ax, label="elevation (m)", fraction=0.046)
ax.set_title(f"2. scipy.griddata linear interpolation\nonto a regular {int(transform.a)}m grid\n(build_dtm)")
ax.set_aspect("equal"); ax.set_xlabel("x (SVY21)"); ax.set_yticks([])

ax = axes[2]
inner = plt.Rectangle((25600, 28800), 900, 900, fill=False, edgecolor="red", linewidth=2)
im2 = ax.imshow(grid_z, extent=(cx.min(), cx.max(), cy.min(), cy.max()), origin="upper",
               cmap="terrain")
ax.add_patch(inner)
ax.set_title("3. this is data/dtm.tif's own resolution\n(20m native) -- terrain.py's\nbuild_domain_dtm just crops+resamples it\n(red box = the 900m domain used elsewhere)")
ax.set_aspect("equal"); ax.set_xticks([]); ax.set_yticks([])

plt.tight_layout()
fig.savefig(f"{OUT}/contour_to_dtm.png", dpi=150, facecolor="white")
print("wrote contour_to_dtm.png")
