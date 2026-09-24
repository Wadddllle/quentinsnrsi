#!/usr/bin/env python
"""Read the Ar-41 OpenMC statepoint, convert to a dose-RATE array shaped exactly like
conc.npy, and save it as dose_ar41.npy + a metadata sidecar.

    .venv/bin/python kelvins_0penmc/read_dose_ar41.py --dir kelvins_0penmc/openmc_run

UNIT CHAIN (same as dose/read_dose.py, stopped one step earlier -- a RATE, not
uSv/h, per the "Bq is already per-second" discussion: this produces Sv/s-family
units, not an accumulated dose):

    tally         [pSv cm^2] * [particle-cm / src]  =  pSv cm^3 / src
    / cell volume [cm^3]                            ->  pSv / src
    * photons_per_s                                 ->  pSv / s
    -> rescaled to ONE fixed SI prefix for the whole array (picked from the actual
       max value), e.g. nSv/s or uSv/s -- not left as raw pSv/s or Sv/s.
"""
import argparse
import json
from pathlib import Path

import numpy as np
import openmc

# (prefix name, Sv/s per 1 unit of that prefix), largest first
PREFIXES = [
    ("Sv/s", 1.0),
    ("mSv/s", 1e-3),
    ("uSv/s", 1e-6),
    ("nSv/s", 1e-9),
    ("pSv/s", 1e-12),
    ("fSv/s", 1e-15),
]


def pick_prefix(max_sv_per_s):
    """Smallest-magnitude prefix such that the scaled max value is still >= 1."""
    if max_sv_per_s <= 0:
        return PREFIXES[-1]
    for name, scale in PREFIXES:
        if max_sv_per_s / scale >= 1.0:
            return name, scale
    return PREFIXES[-1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(Path(__file__).parent / "openmc_run"))
    ap.add_argument("--out", default=str(Path(__file__).parent / "dose_ar41.npy"))
    a = ap.parse_args()

    d = Path(a.dir)
    meta = json.load(open(d / "run_meta.json"))
    # NOT sorted(d.glob(...)) -- that's a lexicographic string sort, and
    # "statepoint.10.h5" < "statepoint.5.h5" as strings, so it silently picks a
    # STALE statepoint from an earlier/smaller run once batch count hits double
    # digits. Sort by the actual integer batch number instead.
    sp_path = max(d.glob("statepoint.*.h5"), key=lambda p: int(p.stem.split(".")[1]))
    nz, ny, nx = meta["grid_shape_zyx"]
    cell_cm3 = meta["tally_cell_cm3"]
    photons_per_s = meta["photons_per_s"]

    with openmc.StatePoint(sp_path) as sp:
        t = sp.get_tally(name="dose")
        # OpenMC mesh filter bins vary X fastest -> flat array is [z][y][x] already,
        # so reshaping (nz,ny,nx) with NO transpose matches conc.npy's own (z,y,x)
        # convention directly (dose/read_dose.py additionally transposes to (x,y,z)
        # for ITS OWN downstream use -- we deliberately don't, to match conc.npy).
        mean_pSv_cm3_per_src = t.mean.reshape(nz, ny, nx)
        std_pSv_cm3_per_src = t.std_dev.reshape(nz, ny, nx)
        n_batches, n_particles = sp.n_batches, sp.n_particles

    dose_pSv_per_s = (mean_pSv_cm3_per_src / cell_cm3) * photons_per_s
    dose_sv_per_s = dose_pSv_per_s * 1e-12

    # relative error, only where there's a nonzero mean to divide by
    rel_err = np.full_like(mean_pSv_cm3_per_src, np.nan)
    nz_mask = mean_pSv_cm3_per_src > 0
    rel_err[nz_mask] = std_pSv_cm3_per_src[nz_mask] / mean_pSv_cm3_per_src[nz_mask]

    unit, scale = pick_prefix(float(dose_sv_per_s.max()))
    dose_out = dose_sv_per_s / scale

    np.save(a.out, dose_out.astype(np.float32))
    print(f"wrote {a.out}: shape {dose_out.shape}, unit {unit}, "
          f"max {dose_out.max():.4g} {unit}, min-nonzero "
          f"{dose_out[dose_out>0].min() if (dose_out>0).any() else float('nan'):.4g} {unit}")

    meta_out = dict(
        unit=unit, scale_sv_per_s_per_unit=scale,
        source_run_dir=str(d), statepoint=str(sp_path),
        n_batches=int(n_batches), n_particles=int(n_particles),
        max_relative_error=float(np.nanmax(rel_err)) if nz_mask.any() else None,
        mean_relative_error=float(np.nanmean(rel_err[nz_mask])) if nz_mask.any() else None,
        nonzero_tally_cells=int(nz_mask.sum()),
        grid_shape_zyx=[nz, ny, nx],
        upstream_meta=meta,
    )
    meta_path = Path(a.out).with_suffix(".json")
    json.dump(meta_out, open(meta_path, "w"), indent=2)
    print(f"wrote {meta_path}")


if __name__ == "__main__":
    main()
