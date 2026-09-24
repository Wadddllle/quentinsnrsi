"""3D voxel render of nonzero conc.npy cells (Bq/m^3), true cell geometry.

Uses ax.voxels so cells are drawn as actual touching 10x10x10 m cubes
(not point markers with gaps), matching the real 20x20x2960 m rectangular
block the nonzero region forms. No axis exaggeration -- left panel is a
full-length true-scale view (reads as a thin bar because the plume really
is that thin relative to its length); right panel zooms into a short y
segment so the 2x2 cube cross-section is actually visible.
"""
import numpy as np
import matplotlib.pyplot as plt
from matplotlib import cm, colors as mcolors
from pathlib import Path

HERE = Path(__file__).parent
CELL_M = 10.0
ZOOM_Y_CELLS = 15  # how many y-cells to show in the zoomed panel


def plot_voxels(ax, a, yi_lo, yi_hi, vmin, vmax, cmap):
    """a: full (z,y,x) array. Draws voxels for y in [yi_lo, yi_hi). Returns the
    true (dx,dy,dz) physical extent in meters, for a truthful (non-exaggerated) aspect ratio."""
    zi, yi, xi = np.nonzero(a)
    keep = (yi >= yi_lo) & (yi < yi_hi)
    zi, yi, xi = zi[keep], yi[keep], xi[keep]
    if len(xi) == 0:
        return None

    x0, x1 = xi.min(), xi.max() + 1
    z0, z1 = zi.min(), zi.max() + 1

    nx, ny, nz = x1 - x0, yi_hi - yi_lo, z1 - z0
    filled = np.zeros((nx, ny, nz), dtype=bool)
    facecolors = np.zeros((nx, ny, nz, 4))

    norm = mcolors.Normalize(vmin=vmin, vmax=vmax)
    logv = np.log10(a[zi, yi, xi])
    filled[xi - x0, yi - yi_lo, zi - z0] = True
    facecolors[xi - x0, yi - yi_lo, zi - z0] = cmap(norm(logv))

    X, Y, Z = np.meshgrid(
        np.arange(x0, x1 + 1) * CELL_M,
        np.arange(yi_lo, yi_hi + 1) * CELL_M,
        np.arange(z0, z1 + 1) * CELL_M,
        indexing="ij",
    )
    ax.voxels(X, Y, Z, filled, facecolors=facecolors, edgecolor="k", linewidth=0.3)
    return (nx * CELL_M, ny * CELL_M, nz * CELL_M)


def main():
    a = np.load(HERE / "conc.npy")
    zi, yi, xi = np.nonzero(a)
    logv_all = np.log10(a[zi, yi, xi])
    vmin, vmax = logv_all.min(), logv_all.max()
    cmap = plt.get_cmap("inferno")

    y_mid = (yi.min() + yi.max()) // 2
    zoom_lo = max(yi.min(), y_mid - ZOOM_Y_CELLS // 2)
    zoom_hi = zoom_lo + ZOOM_Y_CELLS

    fig = plt.figure(figsize=(15, 6.5))
    ax1 = fig.add_subplot(1, 2, 1, projection="3d")
    ax2 = fig.add_subplot(1, 2, 2, projection="3d")

    extent1 = plot_voxels(ax1, a, yi.min(), yi.max() + 1, vmin, vmax, cmap)
    ax1.set_title(f"Full length (true scale): 20x20x{(yi.max()-yi.min()+1)*CELL_M:.0f} m",
                   pad=16, fontsize=11)

    extent2 = plot_voxels(ax2, a, zoom_lo, zoom_hi, vmin, vmax, cmap)
    ax2.set_title(f"Zoomed segment: y={zoom_lo*CELL_M:.0f}-{zoom_hi*CELL_M:.0f} m "
                   f"(cubes touching, true 10m cells)", pad=16, fontsize=11)

    for ax, extent in ((ax1, extent1), (ax2, extent2)):
        ax.set_xlabel("x (m)", labelpad=12)
        ax.set_ylabel("y (m)", labelpad=12)
        ax.set_zlabel("z / height (m)", labelpad=12)
        ax.tick_params(labelsize=8)
        ax.set_box_aspect(extent)  # true physical proportions, no exaggeration

    fig.suptitle("Plume concentration (Bq/m^3), nonzero cells rendered as real 10m cubes", y=0.99)
    fig.subplots_adjust(left=0.03, right=0.87, wspace=0.35, top=0.86, bottom=0.06)
    sm = cm.ScalarMappable(norm=mcolors.Normalize(vmin=vmin, vmax=vmax), cmap=cmap)
    cax = fig.add_axes([0.90, 0.2, 0.02, 0.6])
    fig.colorbar(sm, cax=cax, label="log10(Bq/m^3)")
    fig.savefig(HERE / "conc_3d_scatter.png", dpi=150)
    print(f"wrote {HERE / 'conc_3d_scatter.png'}")


if __name__ == "__main__":
    main()
