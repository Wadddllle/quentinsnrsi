#!/usr/bin/env python
"""Side-by-side cloudshine vs groundshine, plus their ratio.

    .venv/bin/python dose/compare_dose.py

Both panels share ONE colour scale so they are visually comparable -- separate
auto-scaled panels would make a weak field look identical to a strong one, which is
the whole question here.

The ratio panel is the only diverging encoding in this project: it has a meaningful
neutral (1.0 = the two contribute equally), so it gets two opposite hues with a
neutral midpoint, symmetric in log space. A sequential ramp there would hide the
crossover.
"""
import argparse
from pathlib import Path

import numpy as np


def load(csv):
    d = np.genfromtxt(csv, delimiter=",", names=True)
    xs, ys = np.unique(d["x_svy21"]), np.unique(d["y_svy21"])
    o = np.lexsort((d["y_svy21"], d["x_svy21"]))
    shape = (len(xs), len(ys))
    return xs, ys, d["uSv_per_h"][o].reshape(shape), d["ground_m"][o].reshape(shape)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cloud", default="data/openmc_data/dose_run/dose_map.csv")
    ap.add_argument("--ground", default="data/openmc_data/gs_run/dose_map.csv")
    ap.add_argument("-o", "--out", default="data/openmc_data/dose_compare.png")
    ap.add_argument("--cmap", default="magma")
    ap.add_argument("--dark", action="store_true")
    a = ap.parse_args()

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LogNorm, TwoSlopeNorm

    xs, ys, C, G = load(a.cloud)
    _, _, S, _ = load(a.ground)
    ext = [xs.min(), xs.max(), ys.min(), ys.max()]

    ink = "#e8e8ee" if a.dark else "#1c1c22"
    muted = "#9a9aa6" if a.dark else "#6b6b76"
    surface = "#16161c" if a.dark else "#ffffff"
    void = "#3a3a46" if a.dark else "#c9c9d2"

    both = np.concatenate([C[np.isfinite(C) & (C > 0)], S[np.isfinite(S) & (S > 0)]])
    vmin, vmax = np.percentile(both, 2), np.percentile(both, 99.5)

    fig, ax = plt.subplots(1, 3, figsize=(16.5, 5.2))
    fig.subplots_adjust(wspace=0.42, top=0.84)
    fig.patch.set_facecolor(surface)
    cm = plt.get_cmap(a.cmap).copy()
    cm.set_bad(void)

    for k, (A, ttl) in enumerate(((C, "Cloudshine (airborne plume)"),
                                  (S, "Groundshine (dry deposit)"))):
        im = ax[k].imshow(np.ma.masked_invalid(A).T, origin="lower", extent=ext, cmap=cm,
                          norm=LogNorm(vmin=max(vmin, vmax / 1e4), vmax=vmax),
                          interpolation="nearest", aspect="equal")
        ax[k].set_title(ttl, color=ink, fontsize=11, loc="left", pad=16)
        tot = A[np.isfinite(A)]
        ax[k].text(0, 1.005, f"median {np.median(tot[tot>0]):.3g} · peak {np.nanmax(A):.3g} µSv/h",
                   transform=ax[k].transAxes, color=muted, fontsize=8, va="bottom")
        if k == 1:
            cb = fig.colorbar(im, ax=ax[:2].tolist(), fraction=0.028, pad=0.055)
            cb.set_label("dose rate (µSv/h) — shared log scale", color=ink, fontsize=9, labelpad=8)
            cb.ax.tick_params(colors=muted, labelsize=8)
            cb.outline.set_edgecolor(muted); cb.outline.set_linewidth(0.6)

    with np.errstate(divide="ignore", invalid="ignore"):
        R = np.where((C > 0) & (S > 0), S / C, np.nan)
    lim = np.nanpercentile(np.abs(np.log10(R[np.isfinite(R)])), 98)
    im2 = ax[2].imshow(np.ma.masked_invalid(np.log10(R)).T, origin="lower", extent=ext,
                       cmap="RdBu_r", norm=TwoSlopeNorm(vcenter=0, vmin=-lim, vmax=lim),
                       interpolation="nearest", aspect="equal")
    ax[2].set_title("Ratio  groundshine / cloudshine", color=ink, fontsize=11, loc="left", pad=16)
    frac = np.isfinite(R) & (R > 1)
    ax[2].text(0, 1.005, f"groundshine dominates in {100*frac.sum()/np.isfinite(R).sum():.0f}% "
                         f"of cells · median ratio {np.nanmedian(R):.2g}",
               transform=ax[2].transAxes, color=muted, fontsize=8, va="bottom")
    cb2 = fig.colorbar(im2, ax=ax[2], fraction=0.046, pad=0.02,
                       ticks=[-lim, -lim / 2, 0, lim / 2, lim])
    cb2.ax.set_yticklabels([f"{10**v:.2g}x" for v in cb2.get_ticks()])
    cb2.set_label("ratio (blue = cloudshine wins)", color=ink, fontsize=9, labelpad=8)
    cb2.ax.tick_params(colors=muted, labelsize=8)
    cb2.outline.set_edgecolor(muted); cb2.outline.set_linewidth(0.6)

    for A in ax:
        A.set_xlabel("easting (m)", color=ink, fontsize=9)
        A.tick_params(colors=muted, labelsize=8, length=3, width=0.6)
        for s in A.spines.values():
            s.set_color(muted); s.set_linewidth(0.6)
        A.set_facecolor(void)
    ax[0].set_ylabel("northing (m)", color=ink, fontsize=9)

    fig.savefig(a.out, dpi=145, facecolor=surface, bbox_inches="tight", pad_inches=0.25)
    print(f"cloudshine  median {np.nanmedian(C[C>0]):.4g}  peak {np.nanmax(C):.4g} µSv/h")
    print(f"groundshine median {np.nanmedian(S[S>0]):.4g}  peak {np.nanmax(S):.4g} µSv/h")
    print(f"ratio       median {np.nanmedian(R):.3g}  "
          f"(groundshine dominates {100*frac.sum()/np.isfinite(R).sum():.0f}% of cells)")
    print(f"structure: cloudshine p95/p05 = {np.nanpercentile(C,95)/np.nanpercentile(C,5):.1f}x, "
          f"groundshine p95/p05 = {np.nanpercentile(S,95)/np.nanpercentile(S,5):.1f}x")
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
