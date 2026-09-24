#!/usr/bin/env python
"""Build a self-contained, interactive HTML viewer for dose_ar41.npy (or any array
matching conc.npy's grid convention: (z,y,x), 10 m cells).

    .venv/bin/python kelvins_0penmc/make_dose_viewer.py
    xdg-open kelvins_0penmc/dose_ar41_viewer.html

WHY NOT PYVISTA THIS TIME

The earlier conc_pyvista.html works because it's a purely static scene (orbit/zoom
only) -- PyVista's trame export bakes one fixed vtk.js scene and ships it, no live
Python server. An ADJUSTABLE slice needs to re-slice the volume on every drag, which
is a server-side VTK filter operation -- PyVista can only do that live via a running
trame server, not from a static export. So this is hand-rolled Three.js instead:

  - semi-transparent POINT CLOUD layer: one point per (thresholded, subsampled) cell,
    color+alpha both driven by log10(value) through an inferno LUT, alpha blended
    (transparent=true, depthWrite=false) so intensity really reads as brightness/density.
  - opaque, slider-ADJUSTABLE XY slice: a single textured plane; dragging the slider
    just rewrites a small (nx x ny) canvas-backed texture and moves the plane's z --
    genuinely live, all client-side, no server.

Three.js itself is inlined (read from the local npm install, not fetched from a CDN)
so the resulting HTML has zero network dependency and opens straight from disk,
matching the self-contained quality of the pyvista export. It's loaded via a blob URL
+ dynamic import() -- a plain <script src> can't import ES module exports, and a
plain <script type=module src=...> would need a real file:// fetch, which browsers
often block for local files. A blob URL sidesteps both.
"""
import argparse
import base64
import json
from pathlib import Path
from string import Template

import numpy as np
import matplotlib.pyplot as plt

HERE = Path(__file__).parent
CELL_M = 10.0
POINT_CLOUD_MAX = 250_000          # perf cap for the transparent point layer
POINT_CLOUD_PERCENTILE = 50        # only show cells above this percentile of log10(value)
THREE_JS = HERE.parent / "webui" / "node_modules" / "three" / "build" / "three.module.min.js"


def b64(arr: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(arr).tobytes()).decode("ascii")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(HERE / "dose_ar41.npy"))
    ap.add_argument("--out", default=str(HERE / "dose_ar41_viewer.html"))
    ap.add_argument("--title", default="Ar-41 dose rate")
    ap.add_argument("--unit", default=None, help="defaults to reading <data>.json's unit if present")
    a = ap.parse_args()

    arr = np.load(a.data).astype(np.float64)  # (z,y,x)
    nz, ny, nx = arr.shape
    unit = a.unit
    if unit is None:
        meta_path = Path(a.data).with_suffix(".json")
        if meta_path.exists():
            unit = json.load(open(meta_path)).get("unit", "value")
        else:
            unit = "value"

    nonzero = arr > 0
    logv = np.full(arr.shape, np.nan)
    logv[nonzero] = np.log10(arr[nonzero])
    true_min, true_max = float(np.nanmin(logv)), float(np.nanmax(logv))
    # Clip the COLOR range to percentiles, not the true min/max: this dataset spans
    # ~12.7 orders of magnitude because a lot of far-field cells only picked up a
    # single stray Monte Carlo scoring event near the noise floor (down to 1e-11).
    # A linear color map anchored to that true min drags ordinary, unremarkable
    # ground-level values two-thirds of the way up the scale, making everything look
    # falsely "hot". Values outside [vmin,vmax] still render, just clamped to the
    # end colors, so nothing is hidden -- only the color gradient is rescaled.
    vmin, vmax = (float(v) for v in np.nanpercentile(logv, [2, 99.8]))
    print(f"{a.data}: shape={arr.shape}, nonzero={nonzero.sum()}/{arr.size}, "
          f"true log10 range [{true_min:.3f}, {true_max:.3f}], "
          f"color-clipped to [{vmin:.3f}, {vmax:.3f}] (2nd-99.8th percentile)")

    # --- full-resolution byte cube for the opaque slice (every z layer, full x/y) ---
    # 0 = no data (rendered as dark "hole"), 1..255 = scaled log10 value
    cube_byte = np.zeros(arr.shape, dtype=np.uint8)
    scaled = np.clip((logv - vmin) / (vmax - vmin), 0, 1)
    cube_byte[nonzero] = (1 + scaled[nonzero] * 254).astype(np.uint8)

    # --- point cloud subset: only cells above POINT_CLOUD_PERCENTILE, then subsample ---
    cutoff = np.nanpercentile(logv, POINT_CLOUD_PERCENTILE)
    zi, yi, xi = np.nonzero(logv >= cutoff)
    n = len(zi)
    if n > POINT_CLOUD_MAX:
        rng = np.random.default_rng(0)
        keep = rng.choice(n, size=POINT_CLOUD_MAX, replace=False)
        zi, yi, xi = zi[keep], yi[keep], xi[keep]
        n = POINT_CLOUD_MAX
    pos = np.empty((n, 3), dtype=np.float32)
    pos[:, 0] = (xi + 0.5) * CELL_M
    pos[:, 1] = (yi + 0.5) * CELL_M
    pos[:, 2] = (zi + 0.5) * CELL_M
    point_byte = cube_byte[zi, yi, xi].astype(np.uint8)
    print(f"point cloud: {n} points (cutoff log10>={cutoff:.3f}, "
          f"{POINT_CLOUD_PERCENTILE}th percentile)")

    lut = (plt.get_cmap("inferno")(np.linspace(0, 1, 256))[:, :3] * 255).astype(np.uint8)

    three_src = THREE_JS.read_text()

    tmpl = Template((HERE / "_dose_viewer_template.html").read_text())
    html = tmpl.substitute(
        TITLE=a.title,
        UNIT=unit,
        NX=nx, NY=ny, NZ=nz, NZ_MINUS_1=nz - 1, CELL_M=CELL_M,
        VMIN=f"{vmin:.3f}", VMAX=f"{vmax:.3f}",
        N_POINTS=n,
        CUBE_B64=b64(cube_byte),
        POS_B64=b64(pos),
        POINT_BYTE_B64=b64(point_byte),
        LUT_B64=b64(lut),
        THREE_SRC_JSON=json.dumps(three_src),
    )
    Path(a.out).write_text(html)
    print(f"wrote {a.out} ({Path(a.out).stat().st_size/1e6:.1f} MB)")


if __name__ == "__main__":
    main()
