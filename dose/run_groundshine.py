#!/usr/bin/env python
"""Groundshine: dry-deposit the plume onto the terrain, then transport from the ground.

    .venv/bin/python dose/run_groundshine.py --run
    .venv/bin/python dose/read_dose.py --dir data/openmc_data/gs_run

Reuses the same DAGMC geometry, the same tally convention and the same reader as
run_dose.py -- only the SOURCE changes, from an airborne cloud to a surface deposit.

WHY DEPOSITION IS COMPUTED HERE AND NOT IN FLUENT

The CFD ran with `reflect` walls, so no track ever terminates on a surface and there
are no deposition endpoints to bin. That is deliberate, not a gap:

  reflect = v_d 0        trap = v_d infinity        reality = 1e-3 .. 1e-2 m/s

`trap` is the WORSE of the two extremes -- it deletes every particle that brushes a
wall, depleting the airborne field and corrupting cloudshine as well. A sticking
probability in a DEFINE_DPM_BC UDF is not a fix either: wall-strike frequency depends
on grid resolution and DRW time scale, so the same probability yields a different
effective v_d on a different mesh.

The standard route is to keep `reflect` and post-process:

    deposition flux [Bq/m2/s] = v_d * C_air(near surface)
    surface activity [Bq/m2]  = flux * release duration

and the non-depleting assumption is quantitatively excellent here: at v_d = 1e-3 m/s
over a ~50 m mixing depth the loss rate is 2e-5 /s, so over the 80 s domain transit
the plume loses ~0.16%. Deposition is a diagnostic, not a sink, at this scale.

SCOPE (stated, not hidden)

* GROUND deposition only. Roofs and walls also collect, but roof deposit is shielded
  from a ground receptor by the building beneath it, and wall deposit is a second-order
  term. Adding them means a surface-element source, not a per-column one.
* DRY deposition only. Wet deposition (rain scavenging) is often dominant in a real
  assessment and is orders of magnitude faster -- it is not a CFD question and is out
  of scope here.
* No decay, weathering or resuspension: activity accumulates linearly over `--duration`.
  Fine for durations short against the 30 y Cs-137 half-life.
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import openmc

sys.path.insert(0, str(Path(__file__).resolve().parent))
from read_particle_tracks import (by_name, load_wind, read_items, read_tracks,
                                  residence_time, to_world)
from run_dose import (E_GAMMA, XS, YIELD, input_hashes, materials,
                      resolve_extent)

# Reference height for v_d. Deposition velocity is conventionally quoted against the
# concentration at a stated height; 1 m is the usual choice for ground-level work.
VD_REF_H = 1.0


def residence_grid(tracks, lo_m, hi_m, bin_xy, bin_z):
    """3D residence-time field [particle-s per cell] from the CFD tracks."""
    items = read_items(tracks)
    c = by_name(items, read_tracks(tracks, items))
    pid = c["Particle ID"].astype(np.int64)
    t = c["Particle Time"].astype(np.float64)
    xyz = np.column_stack([c[f"Particle {k} Position"] for k in "XYZ"]).astype(np.float64)
    o = np.lexsort((t, pid))
    pid, t, xyz = pid[o], t[o], xyz[o]
    mask, dt = residence_time(pid, t)
    mid = 0.5 * (xyz[:-1][mask] + xyz[1:][mask])

    dims = np.array([int(round((hi_m[0] - lo_m[0]) / bin_xy)),
                     int(round((hi_m[1] - lo_m[1]) / bin_xy)),
                     int(round((hi_m[2] - lo_m[2]) / bin_z))])
    idx = ((mid - lo_m) / (hi_m - lo_m) * dims).astype(int)
    keep = np.all((idx >= 0) & (idx < dims), axis=1)
    idx, w = idx[keep], dt[keep]
    flat = (idx[:, 0] * dims[1] + idx[:, 1]) * dims[2] + idx[:, 2]
    g = np.bincount(flat, weights=w, minlength=int(np.prod(dims))).reshape(dims)
    return g, dims, len(np.unique(pid)), float(dt.sum())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tracks", default="data/openmc_data/particles3.xml")
    ap.add_argument("--h5m", default="data/openmc_data/cfd.h5m")
    ap.add_argument("--stl", default="data/openmc_data/cfd_watertight.stl")
    ap.add_argument("--dtm", default="data/dtm.tif")
    ap.add_argument("--dir", default="data/openmc_data/gs_run")
    ap.add_argument("--release-bq", type=float, default=1e12, help="Q [Bq/s]")
    ap.add_argument("--vd", type=float, default=1e-3,
                    help="dry deposition velocity [m/s] at %.0f m. 1e-3 ~ 1 um aerosol; "
                         "1e-2 ~ elemental iodine; 0 for a noble gas." % VD_REF_H)
    ap.add_argument("--duration", type=float, default=3600.0, help="release duration [s]")
    ap.add_argument("--src-bin", type=float, default=2.5,
                    help="deposit source grid [m]; keep it BELOW the receptor height or "
                         "a receptor sitting over a point source gets a 1/r^2 spike")
    ap.add_argument("--conc-bin", type=float, default=10.0, help="concentration grid [m]")
    ap.add_argument("--src-offset", type=float, default=0.5,
                    help="source height above grade [m]; nonzero so points do not sit "
                         "exactly on a DAGMC facet")
    ap.add_argument("--particles", type=int, default=200_000)
    ap.add_argument("--batches", type=int, default=10)
    ap.add_argument("--tally-bin", type=float, default=10.0)
    ap.add_argument("--receptor-h", type=float, default=1.5)
    ap.add_argument("--tally-ztop", type=float, default=80.0)
    ap.add_argument("--tally-zbin", type=float, default=2.0)
    ap.add_argument("--src-zlo", type=float, default=None,
                    help="source-mesh floor [m]; see run_dose.py::resolve_extent")
    ap.add_argument("--wind", default=None,
                    help="a <stem>.wind.json sidecar (defaults to one beside --h5m). "
                         "Supplies the domain extent AND records the frame in run_meta.")
    ap.add_argument("--run", action="store_true")
    a = ap.parse_args()

    # Same resolution ladder as run_dose.py (CLI -> wind sidecar -> STL bbox -> legacy
    # Kent Ridge), shared rather than duplicated so the two cannot drift apart.
    lo_m, hi_m, W, origin, src = resolve_extent(a.h5m, a.wind, zlo=a.src_zlo)
    print(f"domain extent from {src}")
    print(f"  lo {np.round(lo_m, 1).tolist()}  hi {np.round(hi_m, 1).tolist()}")

    # ---- near-surface air concentration -----------------------------------------
    g, dims, n_tracks, total_res = residence_grid(a.tracks, lo_m, hi_m,
                                                  a.conc_bin, a.tally_zbin)
    cell_v = a.conc_bin ** 2 * a.tally_zbin
    conc = (a.release_bq / n_tracks) * g / cell_v            # Bq/m3
    print(f"tracks {n_tracks:,}, residence {total_res:.4g} particle-s")
    print(f"concentration grid {tuple(dims)} @ {a.conc_bin} x {a.tally_zbin} m, "
          f"max {conc.max():.4g} Bq/m3")

    # ---- deposit onto the terrain -------------------------------------------------
    nsx = int(round((hi_m[0] - lo_m[0]) / a.src_bin))
    nsy = int(round((hi_m[1] - lo_m[1]) / a.src_bin))
    sx = lo_m[0] + (np.arange(nsx) + 0.5) * a.src_bin
    sy = lo_m[1] + (np.arange(nsy) + 0.5) * a.src_bin
    SX, SY = np.meshgrid(sx, sy, indexing="ij")

    # SX/SY are in the tally frame, which for a wind-aligned domain is the ROTATED one.
    # `data/dtm.tif` is a static island-wide raster in TRUE EPSG:3414 that nothing
    # rotates, so it must be sampled at un-rotated coordinates -- otherwise every column
    # gets the grade of a different physical place (~383 m off at 500 m out on a 45 deg
    # bearing). Only the DTM lookup moves; the deposition source itself stays rotated,
    # because the DAGMC geometry it emits into is rotated. `to_world` is a no-op when
    # W is None, so a non-wind run is unaffected.
    import rasterio
    SW = to_world(np.column_stack([SX.ravel(), SY.ravel(), np.zeros(SX.size)]), W)
    with rasterio.open(a.dtm) as r:
        grade = np.array([v[0] for v in r.sample(SW[:, :2])],
                         dtype=float).reshape(nsx, nsy)

    # sample C at grade + VD_REF_H, interpolating in z (bin centres)
    ix = np.clip(((SX - lo_m[0]) / a.conc_bin).astype(int), 0, dims[0] - 1)
    iy = np.clip(((SY - lo_m[1]) / a.conc_bin).astype(int), 0, dims[1] - 1)
    fz = (grade + VD_REF_H - lo_m[2]) / a.tally_zbin - 0.5
    k0 = np.clip(np.floor(fz).astype(int), 0, dims[2] - 1)
    k1 = np.clip(k0 + 1, 0, dims[2] - 1)
    w1 = np.clip(fz - k0, 0.0, 1.0)
    c_surf = conc[ix, iy, k0] * (1 - w1) + conc[ix, iy, k1] * w1     # Bq/m3

    flux = a.vd * c_surf                                             # Bq/m2/s
    dep = flux * a.duration                                          # Bq/m2
    act = dep * a.src_bin ** 2                                       # Bq per source cell

    released = a.release_bq * a.duration
    print(f"\nv_d {a.vd:g} m/s, duration {a.duration:g} s")
    print(f"  deposit: max {dep.max():.4g} Bq/m2, mean {dep.mean():.4g} Bq/m2")
    print(f"  total deposited {act.sum():.4g} Bq of {released:.4g} Bq released "
          f"= {100*act.sum()/released:.3f}%")

    # ---- source points, lifted clear of the geometry ------------------------------
    import trimesh
    m = trimesh.load(a.stl, process=False)
    m.merge_vertices()
    z = grade + a.src_offset
    pos = np.column_stack([SX.ravel(), SY.ravel(), z.ravel()])
    w = act.ravel()
    live = w > 0
    pos, w = pos[live], w[live]

    # A point under a building is not a ground deposit site; a point that the DTM puts
    # slightly inside the (voxel-remeshed) solid is a discretisation mismatch. Raise a
    # little, then drop whatever is still buried.
    inside = m.contains(pos)
    for _ in range(4):
        if not inside.any():
            break
        pos[inside, 2] += 0.5
        inside = m.contains(pos)
    kept = ~inside
    print(f"\nsource points: {live.sum():,} with deposit, "
          f"{(~kept).sum():,} dropped as buried, {kept.sum():,} kept")
    pos, w = pos[kept], w[kept]

    photons_per_s = w.sum() * YIELD
    print(f"  surface activity {w.sum():.4g} Bq -> {photons_per_s:.4g} photon/s")

    # ---- OpenMC -------------------------------------------------------------------
    d = Path(a.dir)
    d.mkdir(parents=True, exist_ok=True)
    materials().export_to_xml(d / "materials.xml")
    openmc.Geometry(openmc.DAGMCUniverse(str(Path(a.h5m).resolve()))).export_to_xml(d / "geometry.xml")

    pts_cm = (pos - origin) * 100.0
    src = openmc.IndependentSource(
        space=openmc.stats.PointCloud(pts_cm, strengths=w),
        energy=openmc.stats.Discrete([E_GAMMA], [1.0]),
        angle=openmc.stats.Isotropic(), particle="photon")

    st = openmc.Settings()
    st.run_mode = "fixed source"
    st.batches, st.particles = a.batches, a.particles
    st.photon_transport = True
    st.electron_treatment = "ttb"
    st.source = src
    st.max_lost_particles = 200
    st.rel_max_lost_particles = 1e-4
    st.export_to_xml(d / "settings.xml")

    tb = int(round((hi_m[0] - lo_m[0]) / a.tally_bin))
    nz = int(round((a.tally_ztop - lo_m[2]) / a.tally_zbin))
    gm = openmc.RegularMesh()
    gm.lower_left = tuple(np.append((lo_m[:2] - origin[:2]) * 100.0, lo_m[2] * 100.0))
    gm.upper_right = tuple(np.append((hi_m[:2] - origin[:2]) * 100.0, a.tally_ztop * 100.0))
    gm.dimension = (tb, tb, nz)

    e_grid, coeff = openmc.data.dose_coefficients("photon", geometry="AP")
    t = openmc.Tally(name="dose")
    t.filters = [openmc.MeshFilter(gm), openmc.ParticleFilter(["photon"]),
                 openmc.EnergyFunctionFilter(e_grid, coeff)]
    t.scores = ["flux"]
    openmc.Tallies([t]).export_to_xml(d / "tallies.xml")

    json.dump(dict(photons_per_s=photons_per_s, inventory_bq=float(w.sum()),
                   release_bq=a.release_bq, yield_=YIELD, vd=a.vd, duration=a.duration,
                   deposited_frac=float(act.sum() / released),
                   tally_cell_cm3=float(np.prod(
                       (np.array(gm.upper_right) - np.array(gm.lower_left)) / np.array(gm.dimension))),
                   origin_m=origin.tolist(), lo_m=lo_m.tolist(), hi_m=hi_m.tolist(),
                   tally_dim=list(gm.dimension), n_tracks=n_tracks,
                   inputs=input_hashes(stl=a.stl, h5m=a.h5m, tracks=a.tracks),
                   # read_dose.py needs this to un-rotate before sampling the DTM.
                   wind=({"wind_from_deg": W["wind_from_deg"], "psi_deg": W["psi_deg"],
                          "rotation": W["rotation"]} if W else None),
                   tally_z=[lo_m[2], a.tally_ztop, nz], receptor_h=a.receptor_h,
                   stl=str(Path(a.stl).resolve())), open(d / "run_meta.json", "w"), indent=2)
    print(f"\nwrote XML to {d}/  ({tb}x{tb}x{nz} tally)")

    if a.run:
        print("\n=== openmc ===")
        r = subprocess.run(["/usr/local/bin/openmc"], cwd=d,
                           env={"OPENMC_CROSS_SECTIONS": XS, "PATH": "/usr/bin:/bin",
                                "HOME": str(Path.home())})
        sys.exit(r.returncode)


if __name__ == "__main__":
    main()
