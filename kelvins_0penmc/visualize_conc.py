"""Visualize conc.npy (Bq/m^3 concentration field).

Array layout: conc[z, y, x], grid cell = 10x10x10 m. All plots use
log10 color scale (masking zeros) since the field is extremely sparse
and spans several orders of magnitude.

Outputs (in this directory):
  - conc_xy_at_zmax.png : horizontal (x,y) slice through the z-layer
                          with the highest total activity
  - conc_xz_at_ymax.png : vertical (x,z) slice through the y-index of
                          the peak concentration
  - conc_yz_at_xmax.png : vertical (y,z) slice through the x-index of
                          the peak concentration
  - conc_projections.png: max-intensity projections along each axis
  - conc_z_profile.png  : total activity and nonzero-cell count per z-layer
"""
import numpy as np
import matplotlib.pyplot as plt
from pathlib import Path

HERE = Path(__file__).parent
CELL_M = 10.0


def log_extent(nx, ny, cell=CELL_M):
    return [0, nx * cell, 0, ny * cell]


def main():
    a = np.load(HERE / "conc.npy")
    nz, ny, nx = a.shape
    print(f"shape (z,y,x)={a.shape}, nonzero={np.count_nonzero(a)}/{a.size}")

    masked = np.ma.masked_equal(a, 0.0)
    log_a = np.ma.log10(masked)
    vmin, vmax = log_a.min(), log_a.max()
    print(f"log10 range: {vmin:.2f} .. {vmax:.2f}  (Bq/m^3: {10**vmin:.3g} .. {10**vmax:.3g})")

    z_peak, y_peak, x_peak = np.unravel_index(np.argmax(a), a.shape)
    print(f"peak cell at (z,y,x)=({z_peak},{y_peak},{x_peak}), "
          f"height={z_peak*CELL_M:.0f} m, conc={a[z_peak,y_peak,x_peak]:.3g} Bq/m^3")

    per_z_sum = a.sum(axis=(1, 2))
    z_active = int(np.argmax(per_z_sum))

    # --- horizontal slice at the most active z layer ---
    fig, ax = plt.subplots(figsize=(8, 7))
    im = ax.imshow(log_a[z_active], origin="lower",
                    extent=log_extent(nx, ny), cmap="inferno", vmin=vmin, vmax=vmax)
    ax.set_title(f"Horizontal slice z={z_active} (height={z_active*CELL_M:.0f} m)")
    ax.set_xlabel("x (m)")
    ax.set_ylabel("y (m)")
    fig.colorbar(im, ax=ax, label="log10(Bq/m^3)")
    fig.tight_layout()
    fig.savefig(HERE / "conc_xy_at_zmax.png", dpi=150)
    plt.close(fig)

    # --- vertical slice (x,z) through peak y ---
    fig, ax = plt.subplots(figsize=(8, 5))
    im = ax.imshow(log_a[:, y_peak, :], origin="lower",
                    extent=log_extent(nx, nz), cmap="inferno", vmin=vmin, vmax=vmax,
                    aspect="auto")
    ax.set_title(f"Vertical slice y={y_peak} (y={y_peak*CELL_M:.0f} m)")
    ax.set_xlabel("x (m)")
    ax.set_ylabel("z / height (m)")
    fig.colorbar(im, ax=ax, label="log10(Bq/m^3)")
    fig.tight_layout()
    fig.savefig(HERE / "conc_xz_at_ymax.png", dpi=150)
    plt.close(fig)

    # --- vertical slice (y,z) through peak x ---
    fig, ax = plt.subplots(figsize=(8, 5))
    im = ax.imshow(log_a[:, :, x_peak], origin="lower",
                    extent=log_extent(ny, nz), cmap="inferno", vmin=vmin, vmax=vmax,
                    aspect="auto")
    ax.set_title(f"Vertical slice x={x_peak} (x={x_peak*CELL_M:.0f} m)")
    ax.set_xlabel("y (m)")
    ax.set_ylabel("z / height (m)")
    fig.colorbar(im, ax=ax, label="log10(Bq/m^3)")
    fig.tight_layout()
    fig.savefig(HERE / "conc_xz_at_ymax.png".replace("xz_at_ymax", "yz_at_xmax"), dpi=150)
    plt.close(fig)

    # --- max intensity projections along each axis ---
    proj_top = np.ma.log10(np.ma.masked_equal(a.max(axis=0), 0.0))    # (y,x): top-down
    proj_front = np.ma.log10(np.ma.masked_equal(a.max(axis=1), 0.0))  # (z,x): front
    proj_side = np.ma.log10(np.ma.masked_equal(a.max(axis=2), 0.0))   # (z,y): side

    fig, axes = plt.subplots(1, 3, figsize=(18, 6))
    im0 = axes[0].imshow(proj_top, origin="lower", extent=log_extent(nx, ny),
                          cmap="inferno", vmin=vmin, vmax=vmax)
    axes[0].set_title("Top-down max projection (x,y)")
    axes[0].set_xlabel("x (m)"); axes[0].set_ylabel("y (m)")

    im1 = axes[1].imshow(proj_front, origin="lower", extent=log_extent(nx, nz),
                          cmap="inferno", vmin=vmin, vmax=vmax, aspect="auto")
    axes[1].set_title("Front max projection (x,z)")
    axes[1].set_xlabel("x (m)"); axes[1].set_ylabel("z / height (m)")

    im2 = axes[2].imshow(proj_side, origin="lower", extent=log_extent(ny, nz),
                          cmap="inferno", vmin=vmin, vmax=vmax, aspect="auto")
    axes[2].set_title("Side max projection (y,z)")
    axes[2].set_xlabel("y (m)"); axes[2].set_ylabel("z / height (m)")

    fig.colorbar(im0, ax=axes, label="log10(Bq/m^3)", shrink=0.8)
    fig.savefig(HERE / "conc_projections.png", dpi=150, bbox_inches="tight")
    plt.close(fig)

    # --- per-z-layer summary ---
    nz_counts = np.count_nonzero(a, axis=(1, 2))
    fig, ax1 = plt.subplots(figsize=(8, 5))
    heights = np.arange(nz) * CELL_M
    ax1.bar(heights, per_z_sum, width=CELL_M * 0.9, color="firebrick")
    ax1.set_xlabel("height z (m)")
    ax1.set_ylabel("total activity in layer (Bq/m^3 summed)", color="firebrick")
    ax1.tick_params(axis="y", labelcolor="firebrick")
    ax1.set_title("Activity vs. height")

    ax2 = ax1.twinx()
    ax2.plot(heights, nz_counts, color="steelblue", marker="o", markersize=3)
    ax2.set_ylabel("nonzero cell count", color="steelblue")
    ax2.tick_params(axis="y", labelcolor="steelblue")

    fig.tight_layout()
    fig.savefig(HERE / "conc_z_profile.png", dpi=150)
    plt.close(fig)

    print("Saved: conc_xy_at_zmax.png, conc_xz_at_ymax.png, conc_yz_at_xmax.png, "
          "conc_projections.png, conc_z_profile.png")


if __name__ == "__main__":
    main()
