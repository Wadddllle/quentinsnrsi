#!/usr/bin/env python
"""Read the OpenMC statepoint and produce a terrain-following ground dose map.

    .venv/bin/python dose/read_dose.py [--dir data/openmc_data/dose_run]

WHY THIS IS NOT JUST A SLICE

The terrain runs from 20 m to ~60 m across this domain, so a tally slab at one
constant elevation is underground over most of the map and reports the dose inside
rock. run_dose.py therefore tallies a full 3D column stack; this script samples the
DTM per column and picks the bin at LOCAL grade + receptor height.

Columns whose receptor point falls inside the solid are masked out rather than
reported as ~0: that point is inside a building, so there is no outdoor ground
receptor there at all, and averaging a shielded in-wall cell into the statistics
would drag the whole map down.

UNIT CHAIN

OpenMC's `flux` score in fixed-source mode is a track-length estimate integrated over
the cell volume, so its units are particle-cm per source particle. The
EnergyFunctionFilter multiplies by the ICRP-116 coefficient in pSv cm^2:

    tally         [pSv cm^2] * [particle-cm / src]  =  pSv cm^3 / src
    / cell volume [cm^3]                            ->  pSv / src
    * photons_per_s                                 ->  pSv / s
    * 3600 / 1e6                                    ->  uSv / h
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
import openmc

sys.path.insert(0, str(Path(__file__).resolve().parent))
from read_particle_tracks import to_world


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="data/openmc_data/dose_run")
    ap.add_argument("--png", default="dose_map.png")
    ap.add_argument("--csv", default="dose_map.csv")
    ap.add_argument("--dtm", default="data/dtm.tif")
    a = ap.parse_args()

    d = Path(a.dir)
    meta = json.load(open(d / "run_meta.json"))
    sp_path = sorted(d.glob("statepoint.*.h5"))[-1]
    nx, ny, nz = meta["tally_dim"]
    zlo, zhi, _ = meta["tally_z"]
    rh = meta["receptor_h"]

    with openmc.StatePoint(sp_path) as sp:
        t = sp.get_tally(name="dose")
        # OpenMC orders mesh filter bins with the X index varying FASTEST, so the flat
        # array is [z][y][x]. Reshaping as (nx, ny, nz) silently scrambles the map --
        # it does not raise, because the element count still matches. Verified against
        # tally.get_pandas_dataframe(), whose first rows are x=1,2,3,4 at y=1, z=1.
        mean = t.mean.reshape(nz, ny, nx).transpose(2, 1, 0)
        std = t.std_dev.reshape(nz, ny, nx).transpose(2, 1, 0)
        n_batches, n_particles = sp.n_batches, sp.n_particles

    lo, hi = np.array(meta["lo_m"]), np.array(meta["hi_m"])
    xs = lo[0] + (np.arange(nx) + 0.5) * (hi[0] - lo[0]) / nx
    ys = lo[1] + (np.arange(ny) + 0.5) * (hi[1] - lo[1]) / ny
    X, Y = np.meshgrid(xs, ys, indexing="ij")

    # THE ONE PLACE TWO FRAMES MEET.
    #
    # X/Y are tally-cell centres, so they are in whatever frame the tally mesh was built
    # in -- and for a wind-aligned domain that is the ROTATED frame (the STL was exported
    # already rotated, so the .h5m, the Fluent case and the tracks all live there too).
    # `data/dtm.tif` is the exception: it is a static island-wide raster in TRUE EPSG:3414
    # that nothing rotates and nothing could, since it is shared across every domain and
    # every bearing.
    #
    # Sampling a world raster at rotated coordinates asks for the elevation of a different
    # physical place -- correct only at the rotation centre, and off by ~383 m at 500 m out
    # on a 45 deg bearing, which on 20-60 m terrain is enough to pick the wrong z bin
    # outright. So: un-rotate for the DTM, keep the rotated ones for everything that
    # touches the STL.
    #
    # `run_dose.py` already copied the rotation into run_meta.json, so nothing new is
    # loaded here. `to_world` returns its input unchanged when W is None, so a non-wind
    # run is bit-for-bit unaffected.
    W = meta.get("wind")
    world = to_world(np.column_stack([X.ravel(), Y.ravel(), np.zeros(X.size)]), W)
    XW, YW = world[:, 0].reshape(nx, ny), world[:, 1].reshape(nx, ny)
    if W:
        print(f"\nframe: rotated (wind from {W['wind_from_deg']:g} deg); "
              f"DTM sampled in true EPSG:3414")

    # local grade from the DTM (independent of the fused STL, which cannot tell a
    # rooftop from the ground by ray casting alone)
    import rasterio
    with rasterio.open(a.dtm) as r:
        ground = np.array([v[0] for v in r.sample(np.column_stack([XW.ravel(), YW.ravel()]))],
                          dtype=float).reshape(nx, ny)

    zr = ground + rh
    zbin = ((zr - zlo) / (zhi - zlo) * nz).astype(int)
    valid = (zbin >= 0) & (zbin < nz)

    # mask receptors that land inside a building. NOTE the ROTATED X/Y here, not the
    # un-rotated XW/YW used for the DTM above: the STL is exported already rotated, so a
    # containment test against it must be done in that same frame.
    import trimesh
    m = trimesh.load(meta["stl"], process=False)
    m.merge_vertices()
    pts = np.column_stack([X.ravel(), Y.ravel(), zr.ravel()])
    inside = m.contains(pts).reshape(nx, ny)
    ok = valid & ~inside

    to_uSv_h = meta["photons_per_s"] * 3600.0 / 1e6 / meta["tally_cell_cm3"]
    ii, jj = np.meshgrid(np.arange(nx), np.arange(ny), indexing="ij")

    # LINEARLY INTERPOLATE BETWEEN Z BINS. Snapping to the containing integer bin
    # makes the receptor height jump by a full bin wherever the terrain crosses a bin
    # boundary, which renders as regular banding across the map -- an artefact of the
    # extraction, not of the physics. Interpolate on bin CENTRES instead.
    dz = (zhi - zlo) / nz
    fz = (zr - zlo) / dz - 0.5                 # position in bin-centre coordinates
    k0 = np.clip(np.floor(fz).astype(int), 0, nz - 1)
    k1 = np.clip(k0 + 1, 0, nz - 1)
    w1 = np.clip(fz - k0, 0.0, 1.0)
    lerp = lambda A: A[ii, jj, k0] * (1 - w1) + A[ii, jj, k1] * w1

    m_i, s_i = lerp(mean), lerp(std)
    dose = m_i * to_uSv_h
    rel = np.divide(s_i, m_i, out=np.zeros_like(s_i), where=m_i > 0)
    dose = np.where(ok, dose, np.nan)

    live = ok & (dose > 0)
    print(f"{sp_path.name}: {n_batches} batches x {n_particles:,} particles")
    print(f"Q = {meta['release_bq']:.3g} Bq/s -> inventory {meta['inventory_bq']:.4g} Bq "
          f"-> {meta['photons_per_s']:.4g} photon/s")
    print(f"\nterrain-following receptor, {rh:.1f} m above local grade")
    print(f"  ground elevation over domain: {ground.min():.1f} .. {ground.max():.1f} m")
    print(f"  columns: {ok.sum():,} outdoor / {inside.sum():,} inside a building "
          f"/ {(~valid).sum():,} outside the tally stack")
    print(f"  cells with signal : {live.sum():,} / {ok.sum():,}")
    print(f"  peak              : {np.nanmax(dose):.4g} uSv/h")
    print(f"  mean  (outdoor)   : {np.nanmean(dose[live]):.4g} uSv/h")
    print(f"  median(outdoor)   : {np.nanmedian(dose[live]):.4g} uSv/h")
    print(f"  rel. error at peak: {rel.ravel()[np.nanargmax(dose)]:.1%}")
    w = dose[live]
    print(f"  rel. error, dose-weighted: {(rel[live]*w).sum()/w.sum():.1%}")
    # dose.shape is (nx, ny), so unravel_index yields (x index, y index) in that order.
    kx, ky = np.unravel_index(np.nanargmax(dose), dose.shape)
    print(f"  peak at EPSG:3414 ({XW[kx,ky]:.1f}, {YW[kx,ky]:.1f}), "
          f"grade {ground[kx,ky]:.1f} m")
    if W:
        print(f"    (rotated frame: {X[kx,ky]:.1f}, {Y[kx,ky]:.1f})")

    # BOTH frames are written. x_rot/y_rot is the regular axis-aligned grid the plotters
    # reshape on (un-rotating it would leave a tilted point cloud that np.unique cannot
    # grid); x_svy21/y_svy21 is the true-world position for georeferencing. With no wind
    # the two pairs are identical.
    np.savetxt(d / a.csv,
               np.column_stack([X.ravel(), Y.ravel(), XW.ravel(), YW.ravel(),
                                ground.ravel(), dose.ravel(), rel.ravel()]),
               delimiter=",",
               header="x_rot,y_rot,x_svy21,y_svy21,ground_m,uSv_per_h,rel_err",
               comments="", fmt="%.6g")
    print(f"\nwrote {d/a.csv}")

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.colors import LogNorm
        pos = dose[np.isfinite(dose) & (dose > 0)]
        fig, ax = plt.subplots(figsize=(7.2, 6))
        cm = plt.get_cmap("magma").copy()
        cm.set_bad("#2a2a35")
        im = ax.imshow(dose.T, origin="lower", extent=[lo[0], hi[0], lo[1], hi[1]],
                       norm=LogNorm(vmin=max(pos.min(), pos.max() / 1e3), vmax=pos.max()),
                       cmap=cm)
        ax.contour(X, Y, ground, levels=8, colors="w", linewidths=0.4, alpha=0.35)
        # The image is drawn on the tally grid, which for a wind run is the ROTATED frame
        # -- so say so rather than mislabel it EPSG:3414. Un-rotating would tilt the grid
        # and imshow cannot draw a rotated raster.
        ax.set_xlabel("rotated easting (m)" if W else "EPSG:3414 easting (m)")
        ax.set_ylabel("rotated northing (m)" if W else "EPSG:3414 northing (m)")
        frame = (f"\nrotated frame: wind from {W['wind_from_deg']:g}°, flow → +Y"
                 if W else "")
        ax.set_title(f"Cloudshine, {rh:.1f} m above grade\nQ={meta['release_bq']:.0g} Bq/s Cs-137"
                     f" (grey = inside a building){frame}", fontsize=10)
        fig.colorbar(im, ax=ax, label="uSv/h")
        fig.tight_layout()
        fig.savefig(d / a.png, dpi=130)
        print(f"wrote {d/a.png}")
    except ImportError:
        print("(matplotlib unavailable, skipped PNG)")


if __name__ == "__main__":
    main()
