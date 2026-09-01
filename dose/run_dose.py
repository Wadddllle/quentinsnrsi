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
from read_particle_tracks import (by_name, load_wind, read_items, read_tracks,
                                  residence_time, to_world)

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


def input_hashes(**paths):
    """{name: {path, sha256, bytes}} for the files a run consumed.

    A dose map is otherwise untraceable: the STL, the .h5m and the tracks XML all get
    regenerated in place, so a result on disk cannot prove which geometry produced it.
    Streamed in 1 MB chunks -- the tracks XML is ~90 MB and the .h5m larger.
    """
    import hashlib
    out = {}
    for name, p in paths.items():
        if not p:
            continue
        f = Path(p)
        if not f.is_file():
            out[name] = {"path": str(p), "sha256": None, "error": "not found"}
            continue
        h = hashlib.sha256()
        with open(f, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        out[name] = {"path": str(f.resolve()), "sha256": h.hexdigest(),
                     "bytes": f.stat().st_size}
    return out


def resolve_extent(h5m, wind_path=None, lo=None, hi=None, zlo=None, ztop=250.0):
    """-> (lo_m, hi_m, W, origin_m, description).

    Resolution ladder, most explicit first: CLI --lo/--hi -> the wind sidecar -> the STL
    beside the .h5m -> the Kent Ridge values this script used to hardcode.

    THE FRAME. If the domain was wind-aligned, the STL was exported already rotated, so
    the .h5m, the Fluent case and the particle tracks ALL live in the rotated frame.
    Everything downstream therefore stays rotated -- un-rotating tracks before binning
    would put the source where the geometry is not and every photon would start in a
    vacuum. Un-rotation belongs at map export only (read_dose.py). Bonus: in the rotated
    frame the domain is exactly axis-aligned, so a RegularMesh fits it with no waste.

    `zlo` is the source-mesh FLOOR. Its old default of 20.0 is Kent Ridge's ground
    elevation: on a domain whose ground sits lower, every source point beneath it is
    silently discarded as "outside the mesh" -- and that is precisely the near-ground
    plume the whole groundshine map is built from. When a sidecar exists the mesh's own
    zmin is used instead; without one the legacy default is preserved so pre-wind runs
    reproduce bit for bit.
    """
    stem = str(h5m).rsplit(".", 1)[0]
    origin = np.array(json.load(open(stem + ".transform.json"))["origin_m"])
    W = load_wind(wind_path or (stem + ".wind.json"))

    if zlo is None:
        zlo = float(W["mesh_bounds_rotated"]["zmin"]) if W else 20.0

    if lo is not None and hi is not None:
        return np.asarray(lo, float), np.asarray(hi, float), W, origin, "--lo/--hi"
    if W is not None:
        d = W["domain_rotated"] if W.get("clipped") else W["mesh_bounds_rotated"]
        return (np.array([d["xmin"], d["ymin"], zlo]),
                np.array([d["xmax"], d["ymax"], ztop]), W, origin,
                f"wind sidecar (wind from {W['wind_from_deg']:g}, rotated frame)")

    # No sidecar: fall back to the STL beside the .h5m. The stem may or may not carry a
    # suffix (cfd.h5m sits next to cfd_watertight.stl), so try the obvious names.
    hit = next((p for p in (Path(stem + s) for s in (".stl", "_watertight.stl", "_raw.stl"))
                if p.is_file()), None)
    if hit is not None:
        import trimesh
        b = trimesh.load(str(hit), process=False).bounds
        return (np.array([b[0][0], b[0][1], zlo]), np.array([b[1][0], b[1][1], ztop]),
                None, origin, f"{hit.name} bounding box (no wind sidecar)")

    # Last resort: the Kent Ridge domain's exact XY bounds, so pre-wind runs reproduce
    # bit for bit -- but they are domain-specific and wrong for anything else.
    return (np.array([21712.5, 30255.4, zlo]), np.array([22112.5, 30655.4, ztop]), None,
            origin, "LEGACY HARDCODED Kent Ridge bounds -- pass --lo/--hi for any other domain")


def terrain_ztop(dtm_path, lo_m, hi_m, W, receptor_h, zbin, fallback):
    """Top of the receptor column stack: the highest ground in the domain plus headroom.

    Sized from the TERRAIN, not from the mesh's zmax (the tallest building) -- at Kent
    Ridge that would stretch the stack from 30 to 51 bins and cost ~1.3x relative error
    for cells no receptor is ever read from. Returns `fallback` when the DTM is
    unreadable, or whenever there is no wind sidecar, so pre-wind runs are unchanged.
    """
    if W is None:
        return fallback, f"legacy default ({fallback:g} m)"
    try:
        import rasterio
        # Corners are enough: the DTM is smooth at 20 m native, and this only sets a
        # ceiling with headroom on top. Un-rotate first -- the DTM is true EPSG:3414.
        gx, gy = np.meshgrid(np.linspace(lo_m[0], hi_m[0], 40),
                             np.linspace(lo_m[1], hi_m[1], 40), indexing="ij")
        w = to_world(np.column_stack([gx.ravel(), gy.ravel(), np.zeros(gx.size)]), W)
        with rasterio.open(dtm_path) as r:
            g = np.array([v[0] for v in r.sample(w[:, :2])], dtype=float)
        top = float(np.nanmax(g)) + receptor_h + 2.0 * zbin
        return top, f"max terrain {np.nanmax(g):.1f} m + {receptor_h:g} m + 2 bins"
    except Exception as e:
        return fallback, f"DTM unreadable ({e}); fell back to {fallback:g} m"


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
    ap.add_argument("--tally-ztop", type=float, default=None,
                    help="top of the tally column stack [m]; must clear the highest "
                         "terrain. Default: max terrain + receptor height + 2 bins when a "
                         "wind sidecar is present, else the legacy 80 m")
    ap.add_argument("--tally-zbin", type=float, default=2.0, help="tally z bin [m]")
    ap.add_argument("--run", action="store_true", help="invoke the openmc CLI after export")
    ap.add_argument("--lo", default=None, help="source-mesh lower corner 'x,y,z' [m]")
    ap.add_argument("--hi", default=None, help="source-mesh upper corner 'x,y,z' [m]")
    ap.add_argument("--src-zlo", type=float, default=None,
                    help="source-mesh floor [m]. Default: the mesh's own zmin when a wind "
                         "sidecar is present, else the legacy 20 m (Kent Ridge's ground)")
    ap.add_argument("--dtm", default="data/dtm.tif",
                    help="DTM used to size the tally column stack")
    ap.add_argument("--src-ztop", type=float, default=250.0, help="source-mesh ceiling [m]")
    ap.add_argument("--wind", default=None,
                    help="a <stem>.wind.json sidecar (defaults to one beside --h5m). "
                         "Supplies the domain extent AND records the frame in run_meta.")
    a = ap.parse_args()

    lo_cli = [float(v) for v in a.lo.split(",")] if a.lo else None
    hi_cli = [float(v) for v in a.hi.split(",")] if a.hi else None
    lo_m, hi_m, W, origin, src = resolve_extent(
        a.h5m, a.wind, lo_cli, hi_cli, zlo=a.src_zlo, ztop=a.src_ztop)
    print(f"domain extent from {src}")
    print(f"  lo {np.round(lo_m, 1).tolist()}  hi {np.round(hi_m, 1).tolist()}")
    bins = np.maximum(np.round((hi_m - lo_m) / a.src_bin), 1).astype(int)

    mesh, strengths, diag, n_tracks = source_strengths(a.tracks, origin, bins, lo_m, hi_m)
    print(f"source: {diag['points']:,} points / {diag['tracks']:,} tracks")
    frac_out = 100 * diag["outside"] / diag["total_residence"]
    print(f"  residence {diag['total_residence']:.4g} particle-s "
          f"({frac_out:.2f}% fell outside the mesh)")
    # Losing a few tenths of a percent at the edges is normal. Losing several percent
    # almost always means the source mesh floor is above the local ground, which silently
    # discards exactly the near-ground plume the groundshine map is built from.
    if frac_out > 2.0:
        print(f"  !! WARNING: {frac_out:.1f}% of residence time is outside the source "
              f"mesh (z {lo_m[2]:.1f}..{hi_m[2]:.1f} m). If the ground here sits below "
              f"{lo_m[2]:.1f} m, raise the domain or pass --src-zlo.")
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
    zhi, ztop_src = terrain_ztop(a.dtm, lo_m, hi_m, W, a.receptor_h, a.tally_zbin,
                                 fallback=a.tally_ztop if a.tally_ztop else 80.0)
    if a.tally_ztop:
        zhi, ztop_src = a.tally_ztop, "--tally-ztop"
    zlo = lo_m[2]
    print(f"  tally column stack top {zhi:.1f} m from {ztop_src}")
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
                wind=({'wind_from_deg': W['wind_from_deg'], 'psi_deg': W['psi_deg'],
                       'rotation': W['rotation']} if W else None),
                tally_dim=list(gm.dimension), n_tracks=n_tracks,
                inputs=input_hashes(stl=Path("data/openmc_data/cfd_watertight.stl"),
                                    h5m=a.h5m, tracks=a.tracks),
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
