#!/usr/bin/env python
"""Ar-41 gamma-only dose-rate map from Kelvin's concentration grid.

    .venv/bin/python kelvins_0penmc/compute_dose_ar41.py --run
    .venv/bin/python kelvins_0penmc/read_dose_ar41.py --dir kelvins_0penmc/openmc_run

INPUT: kelvins_0penmc/conc.npy, a (z,y,x) grid of Ar-41 air concentration in Bq/m^3,
10x10x10 m cells, no real-world georeference -- so the grid's own corner is the
origin, in metres, and that's the frame everything below is built in.

GEOMETRY: plain air, no buildings/DAGMC -- this grid isn't tied to any building
geometry, so a single CSG box of dry air (vacuum outside) is all that's needed.
Box = the source/tally grid extent + MARGIN_M on every side, so photons near the
domain edge don't leak out before contributing their fair share of flux. Margin
picked relative to the ~1293.64 keV line's mean free path in air (comparable to the
106-120 m mfp figures already measured for 662 keV-1 MeV photons in dose/NOTES.md).

SOURCE: one openmc.stats.MeshSpatial built directly from conc.npy -- strength of each
mesh cell = Bq_in_cell = concentration * cell_volume_m3. MeshSpatial samples
uniformly *within* whichever cell gets picked, so (unlike a point source at each
cell's center) it doesn't collapse the emitting volume to a single point.

ENERGY: the real Ar-41 gamma spectrum from kelvins_0penmc/ar41_decay_data.py (5 real
lines from an actual ENDF/B-8.1 depletion chain file, not a hand-typed placeholder).
photons_per_s per cell = Bq_in_cell * total_gamma_yield (~0.992 photons/decay).

DOSE: same ICRP-116 flux-to-dose mechanism already used everywhere in dose/ --
openmc.data.dose_coefficients("photon", geometry="AP") folded into the tally via
EnergyFunctionFilter. ParticleFilter(["photon"]) means only photon flux is scored --
Ar-41's beta decay to K-41 is transported (electron_treatment="ttb" for realistic
secondary-photon production) but never tallied, so the output is gamma dose only, as
asked.

TALLY: same mesh as the source (one dose value per input grid cell, matching "dose
for all the grid cells in the 3D array" from the brief) -- not a separate receptor
grid like dose/run_dose.py's terrain-following column stack.

UNITS: cm internally (OpenMC/cross-section convention), m in this script's own
variables -- multiply by 100 at the geometry/mesh boundary, same as dose/run_dose.py.
"""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import openmc

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ar41_decay_data import load_ar41_gamma_spectrum, CHAIN_FILE

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dose"))
from run_dose import XS, input_hashes  # reuse, don't duplicate

CELL_M = 10.0
MARGIN_M = 400.0  # ~3x the ~120-150 m mfp of a ~1.3 MeV photon in air


def air_material():
    """Dry air, same composition as dose/run_dose.py's materials() -- no concrete
    needed here, there's no building geometry in this task."""
    a = openmc.Material(name="air")
    a.set_density("g/cm3", 1.205e-3)
    for el, w in [("C", 0.000124), ("N", 0.755268), ("O", 0.231781), ("Ar", 0.012827)]:
        a.add_element(el, w, "wo")
    return a


def build_mesh(nx, ny, nz):
    """RegularMesh matching conc.npy's own grid exactly, in cm, origin at the grid's
    own corner (no real-world georeference was supplied)."""
    m = openmc.RegularMesh()
    m.lower_left = (0.0, 0.0, 0.0)
    m.upper_right = (nx * CELL_M * 100.0, ny * CELL_M * 100.0, nz * CELL_M * 100.0)
    m.dimension = (nx, ny, nz)
    return m


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--conc", default=str(Path(__file__).parent / "conc.npy"))
    p.add_argument("--dir", default=str(Path(__file__).parent / "openmc_run"))
    p.add_argument("--particles", type=int, default=10_000)
    p.add_argument("--batches", type=int, default=10)
    p.add_argument("--run", action="store_true")
    a = p.parse_args()

    d = Path(a.dir)
    d.mkdir(parents=True, exist_ok=True)

    conc = np.load(a.conc)  # (z, y, x), Bq/m^3
    nz, ny, nx = conc.shape
    print(f"loaded {a.conc}: shape (z,y,x)={conc.shape}, "
          f"nonzero={np.count_nonzero(conc)}/{conc.size}")

    # --- decay data ---
    energies_eV, intensities, half_life_s = load_ar41_gamma_spectrum()
    total_yield = sum(intensities)
    norm_probs = [i / total_yield for i in intensities]
    print(f"Ar-41 gamma spectrum: {len(energies_eV)} lines, "
          f"total yield {total_yield:.6f} photons/decay (source: {CHAIN_FILE.name})")

    # --- materials ---
    air = air_material()
    openmc.Materials([air]).export_to_xml(d / "materials.xml")

    # --- geometry: single air box, margin on every side ---
    lo = (-MARGIN_M, -MARGIN_M, -MARGIN_M)
    hi = (nx * CELL_M + MARGIN_M, ny * CELL_M + MARGIN_M, nz * CELL_M + MARGIN_M)
    xlo, ylo, zlo = (openmc.XPlane(lo[0] * 100), openmc.YPlane(lo[1] * 100),
                     openmc.ZPlane(lo[2] * 100))
    xhi, yhi, zhi = (openmc.XPlane(hi[0] * 100, boundary_type="vacuum"),
                     openmc.YPlane(hi[1] * 100, boundary_type="vacuum"),
                     openmc.ZPlane(hi[2] * 100, boundary_type="vacuum"))
    xlo.boundary_type = ylo.boundary_type = zlo.boundary_type = "vacuum"
    region = +xlo & -xhi & +ylo & -yhi & +zlo & -zhi
    cell = openmc.Cell(fill=air, region=region)
    openmc.Geometry([cell]).export_to_xml(d / "geometry.xml")

    # --- source: conc.npy directly onto a mesh matching the grid ---
    mesh = build_mesh(nx, ny, nz)
    # array is (z,y,x); MeshSpatial/RegularMesh want x-fastest (Fortran) flattening,
    # same convention documented as a correctness trap in dose/read_dose.py
    conc_xyz = np.transpose(conc, (2, 1, 0))  # -> (x,y,z)
    bq_per_cell = conc_xyz.flatten(order="F") * (CELL_M ** 3)  # Bq per cell
    photons_per_s_total = float(bq_per_cell.sum() * total_yield)
    print(f"total activity: {bq_per_cell.sum():.6e} Bq, "
          f"photons/s: {photons_per_s_total:.6e}")

    src = openmc.IndependentSource(
        space=openmc.stats.MeshSpatial(mesh, strengths=bq_per_cell, volume_normalized=False),
        energy=openmc.stats.Discrete(energies_eV, norm_probs),
        angle=openmc.stats.Isotropic(),
        particle="photon",
    )

    st = openmc.Settings()
    st.run_mode = "fixed source"
    st.batches, st.particles = a.batches, a.particles
    st.photon_transport = True
    st.electron_treatment = "ttb"
    st.source = src
    st.export_to_xml(d / "settings.xml")

    # --- tally: same mesh as the source, ICRP-116 flux-to-dose ---
    e_grid, coeff = openmc.data.dose_coefficients("photon", geometry="AP", data_source="icrp116")
    t = openmc.Tally(name="dose")
    t.filters = [openmc.MeshFilter(mesh), openmc.ParticleFilter(["photon"]),
                 openmc.EnergyFunctionFilter(e_grid, coeff)]
    t.scores = ["flux"]
    openmc.Tallies([t]).export_to_xml(d / "tallies.xml")

    cell_volume_cm3 = (CELL_M * 100.0) ** 3
    meta = dict(
        nuclide="Ar41",
        decay_data_source=str(CHAIN_FILE),
        half_life_s=half_life_s,
        gamma_lines_eV=energies_eV,
        gamma_intensities_per_decay=intensities,
        total_gamma_yield_per_decay=total_yield,
        total_activity_bq=float(bq_per_cell.sum()),
        photons_per_s=photons_per_s_total,
        dose_coefficients=dict(particle="photon", geometry="AP", data_source="icrp116"),
        particle_scored="photon (gamma dose only, no beta/electron scoring)",
        grid_shape_zyx=list(conc.shape),
        cell_size_m=CELL_M,
        margin_m=MARGIN_M,
        tally_cell_cm3=cell_volume_cm3,
        batches=a.batches, particles=a.particles,
        inputs=input_hashes(conc=a.conc, chain=str(CHAIN_FILE)),
    )
    json.dump(meta, open(d / "run_meta.json", "w"), indent=2)
    print(f"\nwrote XML to {d}/")

    if a.run:
        print("\n=== openmc ===")
        r = subprocess.run(["/usr/local/bin/openmc"], cwd=d,
                           env={"OPENMC_CROSS_SECTIONS": XS, "PATH": "/usr/bin:/bin",
                                "HOME": str(Path.home())})
        sys.exit(r.returncode)


if __name__ == "__main__":
    main()
