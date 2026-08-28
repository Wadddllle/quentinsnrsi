#!/usr/bin/env python
"""End-to-end PoC: Fluent particle tracks -> OpenMC cloudshine dose map.

Builds the OpenMC XML in the project .venv (which has the openmc python API but no
compiled libopenmc.so), then runs the transport with the system CLI:

    .venv/bin/python dose/run_dose.py --run

THE COUPLING, IN ONE LINE

Fluent writes roughly one track point per control-volume crossing, so the time
between consecutive points of a track is the residence time in the cell traversed.
Binning `sum(dt)` onto a mesh therefore gives something directly proportional to
concentration, with no resampling:

    activity in cell i  =  (Q / N) * sum(dt_i)      [Bq]     Q = release rate [Bq/s]
    concentration       =  that / cell volume       [Bq/m3]  N = tracer count

This only holds because every tracer is EQUAL WEIGHT, which in turn only holds
because the injection is a single point source. A surface injection off the inlet
is face-count weighted (it follows mesh refinement, not mass flux) and would need a
per-track correction -- see NOTES.md.

`Q` is a single scalar applied at the very end. OpenMC normalises `strengths`
internally, so it only ever rescales the final tally.

UNITS: the .h5m is in centimetres and recentred (see stl_to_h5m.py). Track positions
are world metres. The transform in `cfd.transform.json` must be applied to the source
mesh AND the tally mesh, or the dose map is silently mis-georeferenced.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import openmc

sys.path.insert(0, str(Path(__file__).resolve().parent))
from read_particle_tracks import by_name, read_items, read_tracks, residence_time

XS = "/home/quentin/nuclear_data/cross_sections.xml"

# Cs-137: 661.657 keV gamma, 85.1% per decay. The single most relevant nuclide for
# a ground-level release, and monoenergetic enough to keep a PoC interpretable.
E_GAMMA, YIELD = 661.657e3, 0.851


def materials():
    """NIST ordinary concrete and dry air. Full elemental composition -- the photon
    library now carries all 84 elements, so no proxies are needed."""
    c = openmc.Material(name="concrete")
    c.set_density("g/cm3", 2.3)
    for el, w in [("H", 0.010), ("C", 0.001), ("O", 0.529), ("Na", 0.016), ("Mg", 0.002),
                  ("Al", 0.034), ("Si", 0.337), ("K", 0.013), ("Ca", 0.044), ("Fe", 0.014)]:
        c.add_element(el, w, "wo")
    a = openmc.Material(name="air")
    a.set_density("g/cm3", 1.205e-3)
    for el, w in [("C", 0.000124), ("N", 0.755268), ("O", 0.231781), ("Ar", 0.012827)]:
        a.add_element(el, w, "wo")
    return openmc.Materials([c, a])


def source_strengths(tracks, origin, bins, lo_m, hi_m):
    """Residence-time field -> (RegularMesh in cm, strengths, diagnostics).

    Weighted by dt and deposited at the MIDPOINT of each step, which is where the
    particle actually spent that time.
    """
    items = read_items(tracks)
    c = by_name(items, read_tracks(tracks, items))
    pid = c["Particle ID"].astype(np.int64)
    t = c["Particle Time"].astype(np.float64)
    xyz = np.column_stack([c[f"Particle {k} Position"] for k in "XYZ"]).astype(np.float64)

    order = np.lexsort((t, pid))
    pid, t, xyz = pid[order], t[order], xyz[order]
    mask, dt = residence_time(pid, t)
    mid = 0.5 * (xyz[:-1][mask] + xyz[1:][mask])

    idx = ((mid - lo_m) / (hi_m - lo_m) * bins).astype(int)
    keep = np.all((idx >= 0) & (idx < bins), axis=1)
    idx, w = idx[keep], dt[keep]
    flat = (idx[:, 0] * bins[1] + idx[:, 1]) * bins[2] + idx[:, 2]
    s = np.bincount(flat, weights=w, minlength=int(np.prod(bins)))

    mesh = openmc.RegularMesh()
    mesh.lower_left = tuple((lo_m - origin) * 100.0)
    mesh.upper_right = tuple((hi_m - origin) * 100.0)
    mesh.dimension = tuple(int(b) for b in bins)

    n_tracks = len(np.unique(pid))
    diag = dict(points=len(pid), tracks=n_tracks, total_residence=float(dt.sum()),
                binned=float(w.sum()), outside=float(dt.sum() - w.sum()),
                occupied=int((s > 0).sum()), cells=int(s.size))
    return mesh, s, diag, n_tracks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tracks", default="data/openmc_data/particles3.xml")
    ap.add_argument("--h5m", default="data/openmc_data/cfd.h5m")
    ap.add_argument("--dir", default="data/openmc_data/dose_run")
    ap.add_argument("--release-bq", type=float, default=1e12, help="release rate Q [Bq/s]")
    ap.add_argument("--particles", type=int, default=200_000)
    ap.add_argument("--batches", type=int, default=10)
    ap.add_argument("--src-bin", type=float, default=10.0, help="source mesh cell size [m]")
    ap.add_argument("--tally-bin", type=float, default=10.0, help="tally cell size [m]")
    ap.add_argument("--receptor-h", type=float, default=1.5,
                    help="receptor height above LOCAL grade [m]")
    ap.add_argument("--tally-ztop", type=float, default=80.0,
                    help="top of the tally column stack [m]; must clear the highest terrain")
    ap.add_argument("--tally-zbin", type=float, default=2.0, help="tally z bin [m]")
    ap.add_argument("--run", action="store_true", help="invoke the openmc CLI after export")
    a = ap.parse_args()

    T = json.load(open(str(a.h5m).rsplit(".", 1)[0] + ".transform.json"))
    origin = np.array(T["origin_m"])

    # Source mesh spans the plume; tally mesh is a thin ground-level slab.
    lo_m = np.array([21712.5, 30255.4, 20.0])
    hi_m = np.array([22112.5, 30655.4, 250.0])
    bins = np.maximum(np.round((hi_m - lo_m) / a.src_bin), 1).astype(int)

    mesh, strengths, diag, n_tracks = source_strengths(a.tracks, origin, bins, lo_m, hi_m)
    print(f"source: {diag['points']:,} points / {diag['tracks']:,} tracks")
    print(f"  residence {diag['total_residence']:.4g} particle-s "
          f"({100*diag['outside']/diag['total_residence']:.2f}% fell outside the mesh)")
    print(f"  mesh {tuple(bins)} = {diag['cells']:,} cells, {diag['occupied']:,} occupied "
          f"({100*diag['occupied']/diag['cells']:.1f}%)")

    # ABSOLUTE NORMALISATION -- the subtle bit, and easy to get wrong by ~2 orders.
    # `strengths` only fixes the SHAPE (OpenMC normalises it to a PDF), so the total
    # emission rate has to be supplied separately, and it is NOT Q*yield.
    #
    # Q is an activity RELEASE RATE [Bq/s]. Material lingers, so the steady-state
    # inventory sitting in the domain is Q * (mean residence time), and it is that
    # inventory which is decaying right now:
    #
    #   inventory [Bq] = (Q/N) * sum(dt)      <- exactly the sum of the cell activities
    #   photons/s      = inventory * yield
    #
    # Using Q*yield instead silently under-predicts by the mean residence time
    # (91.4 s here, i.e. a factor of ~91).
    inventory_bq = (a.release_bq / n_tracks) * diag["binned"]
    photons_per_s = inventory_bq * YIELD
    print(f"  Q = {a.release_bq:.3g} Bq/s, mean residence {diag['binned']/n_tracks:.1f} s")
    print(f"  steady-state inventory {inventory_bq:.4g} Bq -> {photons_per_s:.4g} photon/s")

    d = Path(a.dir)
    d.mkdir(parents=True, exist_ok=True)
    mats = materials()
    mats.export_to_xml(d / "materials.xml")
    openmc.Geometry(openmc.DAGMCUniverse(str(Path(a.h5m).resolve()))).export_to_xml(d / "geometry.xml")

    # Reject births inside the solid. MeshSpatial samples uniformly within a cell, so
    # any cell straddling a building emits throughout it even though all of its
    # residence time came from air. Measured at a 10 m bin: 2.35% of samples land in
    # concrete. That is physically wrong on its own (a plume tracer never exists inside
    # a building, and those births are self-shielded, biasing the dose low), and it is
    # also what drives rays into the 7 self-colliding facet pairs in the STL and gets
    # them lost. Rejection fixes both. Safe against infinite rejection loops: a cell
    # fully inside the solid has zero CFD residence time, hence zero strength.
    air_mat = [m for m in mats if m.name == "air"][0]
    src = openmc.IndependentSource(
        space=openmc.stats.MeshSpatial(mesh, strengths=strengths, volume_normalized=False),
        energy=openmc.stats.Discrete([E_GAMMA], [1.0]),
        angle=openmc.stats.Isotropic(), particle="photon", domains=[air_mat])

    st = openmc.Settings()
    st.run_mode = "fixed source"
    st.batches, st.particles = a.batches, a.particles
    st.photon_transport = True
    st.electron_treatment = "ttb"
    st.source = src
    # The STL is closed and edge-manifold (check_watertight: 0/0/0) but has 7
    # self-colliding facet pairs, where a ray can fail to find an exit. Measured loss
    # rate was ~8e-6, statistically negligible -- but the default cap of 10 aborts the
    # run, so allow a bounded number rather than pretend the defect is not there.
    st.max_lost_particles = 200
    st.rel_max_lost_particles = 1e-4
    st.export_to_xml(d / "settings.xml")

    # Receptor tally. The terrain is NOT flat -- it runs from 20 m to ~60 m across this
    # domain -- so a slab at one constant elevation would be underground over most of
    # the map and would report the dose inside rock. Instead tally a full 3D column
    # stack; read_dose.py then ray-casts the ground per column and picks the bin at
    # local grade + receptor height, giving a genuinely terrain-following map.
    tb = np.maximum(np.round((hi_m[:2] - lo_m[:2]) / a.tally_bin), 1).astype(int)
    zlo, zhi = lo_m[2], a.tally_ztop
    nz = int(round((zhi - zlo) / a.tally_zbin))
    gm = openmc.RegularMesh()
    gm.lower_left = tuple(np.append((lo_m[:2] - origin[:2]) * 100.0, zlo * 100.0))
    gm.upper_right = tuple(np.append((hi_m[:2] - origin[:2]) * 100.0, zhi * 100.0))
    gm.dimension = (int(tb[0]), int(tb[1]), nz)

    e_grid, coeff = openmc.data.dose_coefficients("photon", geometry="AP")
    t = openmc.Tally(name="dose")
    t.filters = [openmc.MeshFilter(gm), openmc.ParticleFilter(["photon"]),
                 openmc.EnergyFunctionFilter(e_grid, coeff)]
    t.scores = ["flux"]
    openmc.Tallies([t]).export_to_xml(d / "tallies.xml")

    meta = dict(photons_per_s=photons_per_s, inventory_bq=inventory_bq,
                release_bq=a.release_bq, yield_=YIELD,
                tally_cell_cm3=float(np.prod(
                    (np.array(gm.upper_right) - np.array(gm.lower_left)) / np.array(gm.dimension))),
                origin_m=origin.tolist(), lo_m=lo_m.tolist(), hi_m=hi_m.tolist(),
                tally_dim=list(gm.dimension), n_tracks=n_tracks,
                tally_z=[zlo, zhi, nz], receptor_h=a.receptor_h,
                stl=str(Path("data/openmc_data/cfd_watertight.stl").resolve()))
    json.dump(meta, open(d / "run_meta.json", "w"), indent=2)
    print(f"\nwrote XML to {d}/")
    print("  tally: %dx%dx%d cells, z %.0f..%.0f m in %.1f m bins, cell volume %.4g cm3"
          % (*gm.dimension, zlo, zhi, a.tally_zbin, meta["tally_cell_cm3"]))

    if a.run:
        print("\n=== openmc ===")
        r = subprocess.run(["/usr/local/bin/openmc"], cwd=d,
                           env={"OPENMC_CROSS_SECTIONS": XS, "PATH": "/usr/bin:/bin",
                                "HOME": str(Path.home())})
        sys.exit(r.returncode)


if __name__ == "__main__":
    main()
