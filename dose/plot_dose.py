#!/usr/bin/env python
"""Heatmap of the terrain-following dose map written by read_dose.py.

    .venv/bin/python dose/plot_dose.py                       # auto scale
    .venv/bin/python dose/plot_dose.py --scale log --hist
    .venv/bin/python dose/plot_dose.py --dark -o dark.png

Reads `dose_map.csv` (x, y, ground_m, uSv_per_h, rel_err) so it is decoupled from
the statepoint -- re-plot without re-running transport.

DESIGN NOTES (why it looks the way it does)

* Sequential magnitude -> a single perceptually-uniform luminance ramp with a scale
  legend, never a rainbow. `magma` and `viridis` are monotonic in lightness and
  CVD-safe; `jet`/`turbo` are not and are refused.
* Percentile clipping is ON by default. 13 cells in this map sit below 1 uSv/h with
  ~66% relative error -- statistical noise, not physical shadow. Letting them set
  the colour floor throws away the entire dynamic range of the real signal.
* Cells whose receptor lands inside a building are NOT zero, they are undefined.
  They get a distinct neutral, and a legend entry, rather than the ramp's dark end
  (which would read as "safe here").
* `--scale auto` picks log only when p95/p05 > 20. This field is genuinely flat
  (~3.8x across well-resolved cells because the 662 keV photon mfp in air is 106 m
  against a 400 m domain), so log would *manufacture* apparent structure out of
  Monte-Carlo noise. Forcing `--scale log` is available but says so on the figure.
"""
import argparse
from pathlib import Path

import numpy as np

BAD_CMAPS = {"jet", "turbo", "rainbow", "gist_rainbow", "nipy_spectral", "hsv"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default="data/openmc_data/dose_run/dose_map.csv")
    ap.add_argument("-o", "--out", default=None)
    ap.add_argument("--scale", choices=["auto", "log", "linear"], default="auto")
    ap.add_argument("--cmap", default="magma")
    ap.add_argument("--clip", default="2,99.5",
                    help="percentile clip for the colour scale, 'lo,hi' (0,100 disables)")
    ap.add_argument("--contours", action="store_true", default=True)
    ap.add_argument("--no-contours", dest="contours", action="store_false")
    ap.add_argument("--hist", action="store_true", help="add a distribution panel")
    ap.add_argument("--max-err", type=float, default=None,
                    help="grey out cells whose relative error exceeds this (e.g. 0.2)")
    ap.add_argument("--dark", action="store_true")
    ap.add_argument("--title", default=None)
    a = ap.parse_args()

    if a.cmap in BAD_CMAPS:
        raise SystemExit(f"{a.cmap!r} is not monotonic in lightness and is not CVD-safe; "
                         "use magma, inferno, viridis or cividis")

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.colors import LogNorm, Normalize
    from matplotlib.patches import Patch

    d = np.genfromtxt(a.csv, delimiter=",", names=True)
    # Reshape on the ROTATED grid -- it is the regular one. `x_svy21` is that same grid
    # un-rotated (a tilted point cloud), which np.unique cannot grid. Older CSVs carry
    # only x_svy21, and for a non-wind run the two are identical anyway.
    rot = "x_rot" in d.dtype.names
    x, y = (d["x_rot"], d["y_rot"]) if rot else (d["x_svy21"], d["y_svy21"])
    dose, err, ground = d["uSv_per_h"], d["rel_err"], d["ground_m"]

    xs, ys = np.unique(x), np.unique(y)
    nx, ny = len(xs), len(ys)
    order = np.lexsort((y, x))
    D = dose[order].reshape(nx, ny)
    E = err[order].reshape(nx, ny)
    G = ground[order].reshape(nx, ny)

    indoor = ~np.isfinite(D)
    if a.max_err is not None:
        D = np.where(E > a.max_err, np.nan, D)
    live = np.isfinite(D) & (D > 0)
    pos = D[live]

    lo_p, hi_p = (float(v) for v in a.clip.split(","))
    vmin, vmax = np.percentile(pos, lo_p), np.percentile(pos, hi_p)
    spread = np.percentile(pos, 95) / max(np.percentile(pos, 5), 1e-12)
    scale = a.scale
    if scale == "auto":
        scale = "log" if spread > 20 else "linear"
    norm = (LogNorm(vmin=max(vmin, vmax / 1e4), vmax=vmax) if scale == "log"
            else Normalize(vmin=vmin, vmax=vmax))

    ink = "#e8e8ee" if a.dark else "#1c1c22"
    muted = "#9a9aa6" if a.dark else "#6b6b76"
    surface = "#16161c" if a.dark else "#ffffff"
    void = "#3a3a46" if a.dark else "#c9c9d2"

    fig, axes = plt.subplots(1, 2 if a.hist else 1,
                             figsize=(10.6 if a.hist else 7.4, 5.8),
                             gridspec_kw={"width_ratios": [3, 2]} if a.hist else None)
    ax = axes[0] if a.hist else axes
    fig.patch.set_facecolor(surface)

    cm = plt.get_cmap(a.cmap).copy()
    cm.set_bad(void)
    ax.set_facecolor(void)
    im = ax.imshow(np.ma.masked_invalid(D).T, origin="lower", cmap=cm, norm=norm,
                   extent=[xs.min(), xs.max(), ys.min(), ys.max()],
                   interpolation="nearest", aspect="equal")

    if a.contours:
        X, Y = np.meshgrid(xs, ys, indexing="ij")
        cs = ax.contour(X, Y, G, levels=7, colors=[muted], linewidths=0.5, alpha=0.5)
        ax.clabel(cs, inline=True, fontsize=6, fmt="%.0f m", colors=[muted])

    # A wind run's grid is the rotated frame, so only claim EPSG:3414 when it really is
    # one. Detected by whether the two frames in the CSV actually differ.
    rotated = rot and not np.allclose(d["x_rot"], d["x_svy21"])
    axes_frame = "rotated (wind → +Y)" if rotated else "EPSG:3414"
    ax.set_xlabel(f"{axes_frame} easting (m)", color=ink, fontsize=9)
    ax.set_ylabel(f"{axes_frame} northing (m)", color=ink, fontsize=9)
    ax.tick_params(colors=muted, labelsize=8, length=3, width=0.6)
    for s in ax.spines.values():
        s.set_color(muted); s.set_linewidth(0.6)

    cb = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.03,
                      extend="both" if (lo_p > 0 or hi_p < 100) else "neither")
    cb.set_label("dose rate (µSv/h)", color=ink, fontsize=9)
    cb.ax.tick_params(colors=muted, labelsize=8)
    cb.outline.set_edgecolor(muted); cb.outline.set_linewidth(0.6)

    title = a.title or "Cloudshine dose, 1.5 m above local grade"
    sub = (f"{scale} scale · clipped to {lo_p:g}–{hi_p:g} pct · "
           f"p95/p05 = {spread:.1f}× (field is genuinely flat)")
    if scale == "log" and spread <= 20:
        sub += " — log FORCED; apparent structure may be MC noise"
    ax.set_title(title, color=ink, fontsize=11, loc="left", pad=14)
    ax.text(0, 1.015, sub, transform=ax.transAxes, color=muted, fontsize=7.5, va="bottom")

    handles = [Patch(facecolor=void, edgecolor="none", label="receptor inside a building")]
    if a.max_err is not None:
        handles.append(Patch(facecolor=void, edgecolor="none",
                             label=f"rel. error > {a.max_err:.0%}"))
    leg = ax.legend(handles=handles, loc="upper left", bbox_to_anchor=(0, -0.10),
                    frameon=False, fontsize=8, labelcolor=ink, handlelength=1.2)
    leg.set_zorder(1)

    if a.hist:
        h = axes[1]
        h.set_facecolor(surface)
        bins = (np.logspace(np.log10(max(pos.min(), 1e-4)), np.log10(pos.max()), 40)
                if scale == "log" else 40)
        h.hist(pos, bins=bins, color=plt.get_cmap(a.cmap)(0.62), edgecolor="none")
        if scale == "log":
            h.set_xscale("log")
        for q, lbl in ((np.median(pos), "median"), (pos.max(), "peak")):
            h.axvline(q, color=ink, lw=1.0, alpha=0.7)
            h.text(q, h.get_ylim()[1] * 0.96, f" {lbl} {q:.0f}", color=ink,
                   fontsize=7.5, ha="left", va="top", rotation=90)
        h.set_xlabel("dose rate (µSv/h)", color=ink, fontsize=9)
        h.set_ylabel("outdoor receptor cells", color=ink, fontsize=9)
        h.tick_params(colors=muted, labelsize=8, length=3, width=0.6)
        for s in h.spines.values():
            s.set_color(muted); s.set_linewidth(0.6)
        h.spines["top"].set_visible(False); h.spines["right"].set_visible(False)
        h.grid(axis="y", color=muted, alpha=0.18, lw=0.5)
        h.set_axisbelow(True)

    out = Path(a.out) if a.out else Path(a.csv).with_name("dose_heatmap.png")
    fig.tight_layout()
    fig.savefig(out, dpi=150, facecolor=surface, bbox_inches="tight")
    print(f"{live.sum():,} outdoor cells, {indoor.sum():,} inside buildings")
    print(f"  p05 {np.percentile(pos,5):.4g}  median {np.median(pos):.4g}  "
          f"p95 {np.percentile(pos,95):.4g}  peak {pos.max():.4g} µSv/h")
    print(f"  p95/p05 = {spread:.1f}x -> {scale} scale")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
