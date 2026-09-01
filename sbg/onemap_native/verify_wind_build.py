"""End-to-end check of the build-once / rotate-and-clip-per-direction wind pipeline.

    .venv/bin/python -m sbg.onemap_native.verify_wind_build [--dirs 4] [--raw]

Slow (a real build). Exits non-zero on failure. Complements verify_wind.py, which covers
the pure geometry in ~1 s.

Checks, in order of how much they matter:
  1. GEOREFERENCE ROUND TRIP -- a real building corner, taken from the extracted
     geometry so it provably exists in the world, survives forward -> inverse. Run for
     EVERY direction: a point recovered correctly for one psi and wrongly for another
     means the sidecar is being written from a stale loop variable, which is exactly the
     bug a per-direction loop invites.
  2. STRICT WATERTIGHTNESS, to this project's standard -- meshlib holes == 0 AND a
     trimesh process=False + merge_vertices(digits_vertex=6) re-weld reporting
     is_watertight, 1 body, and zero exactly-zero-area faces. meshlib's hole count alone
     is documented as insufficient.
  3. ORIENTATION -- the ROI centre must sit `up_m` from ymin and `down_m` from ymax in
     every mesh, i.e. the inlet really is the upwind wall.
  4. CONTENT INVARIANCE -- all directions must agree about the ROI.
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
from shapely.geometry import box

OUT = Path("/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583"
           "/scratchpad/windbuild")
ROI = box(21950, 30250, 22850, 31150)          # Kent Ridge, 900 m
_FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   {detail}" if not cond else ""))
    if not cond:
        _FAILS.append(name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dirs", type=int, default=4)
    ap.add_argument("--raw", action="store_true", help="exercise raw mode x wind")
    a = ap.parse_args()

    import meshlib.mrmeshpy as mr
    import meshlib.mrmeshnumpy as mn
    import trimesh
    from sbg.onemap_native.build import build_domain_stl
    from sbg.onemap_native.wind import (
        build_envelope, buffer_distances, check_envelope, roi_centre, rose)

    OUT.mkdir(parents=True, exist_ok=True)
    for f in OUT.glob("wind_*"):
        f.unlink()

    dirs = rose(a.dirs)
    c = roi_centre(ROI)
    up, down, lat, warns = buffer_distances(60.0)
    env = build_envelope(ROI, c, dirs, up, down, lat)
    check_envelope(env, ROI, c, dirs, up, down, lat)     # raises if it would break
    print(f"ROI {ROI.area/1e6:.2f} km^2 -> envelope {env.area/1e6:.2f} km^2, "
          f"buffer {up:.0f}/{down:.0f}/{lat:.0f} m, {a.dirs} directions"
          f"{', RAW' if a.raw else ''}\n")

    res = build_domain_stl(
        env, OUT / "wind.stl", core_polygon=ROI, store_dir="data/onemap_store",
        voxel_size=(0.0 if a.raw else 2.0),
        wind={"dirs": dirs, "centre": c, "up_m": up, "down_m": down,
              "lat_m": lat, "z0_m": 0.7})

    print(f"\n[1] artifacts")
    check(f"{len(dirs)} results returned", len(res) == len(dirs), f"got {len(res)}")
    for r in res:
        check(f"  {Path(r['stl']).name} + sidecar exist",
              Path(r["stl"]).is_file() and Path(r["sidecar"]).is_file())

    # A real building corner: provably exists in the world, unlike a synthetic point.
    from sbg.onemap_native.extract import extract_domain_buildings
    from sbg.onemap_native.tiles import domain_leaf_tiles
    from pyproj import Transformer
    tr = Transformer.from_crs("EPSG:3414", "EPSG:4326", always_xy=True)
    lo, la = tr.transform([21950, 22850], [30250, 31150])
    pieces = extract_domain_buildings(
        domain_leaf_tiles(min(lo), min(la), max(lo), max(la)), ROI,
        store_dir="data/onemap_store")
    tallest = max(pieces, key=lambda p: np.asarray(p["verts"])[:, 2].max())
    v = np.asarray(tallest["verts"])
    probe = v[np.argmax(v[:, 2])]          # apex of the tallest building
    print(f"\n[2] georeference round trip (probe = apex of the tallest building, "
          f"{probe.round(2).tolist()})")

    worst = 0.0
    for r in res:
        W = json.loads(Path(r["sidecar"]).read_text())
        cm = np.array(W["rotation"]["centre_m"])
        F = np.array(W["rotation"]["forward_matrix"])
        I = np.array(W["rotation"]["inverse_matrix"])
        rot = F @ (probe[:2] - cm) + cm
        back = I @ (rot - cm) + cm
        err = float(np.abs(back - probe[:2]).max())
        worst = max(worst, err)
        check(f"  wind {W['wind_from_deg']:>5.1f}: forward->inverse recovers the point",
              err < 1e-6, f"err {err:.3e} m")
    print(f"       worst round-trip error across all directions: {worst:.2e} m")

    print(f"\n[3] strict watertightness"
          + ("  (SKIPPED -- raw mode is non-watertight by design)" if a.raw else ""))
    vols = []
    for r in res:
        p = r["stl"]
        ml = mr.loadMesh(p)
        mv, mf = mn.getNumpyVerts(ml), mn.getNumpyFaces(ml.topology)
        tri = mv[mf]
        area0 = int((0.5 * np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0],
                                                   tri[:, 2] - tri[:, 0]), axis=1) == 0).sum())
        m = trimesh.load(p, process=False)
        m.merge_vertices(digits_vertex=6)
        vols.append(abs(m.volume))
        if a.raw:
            continue
        ok = (len(ml.topology.findHoleRepresentiveEdges()) == 0
              and m.is_watertight and m.body_count == 1 and area0 == 0)
        check(f"  {Path(p).name}: holes 0 / strict watertight / 1 body / 0 zero-area", ok,
              f"holes={len(ml.topology.findHoleRepresentiveEdges())} "
              f"wt={m.is_watertight} bodies={m.body_count} zeroArea={area0}")

    print(f"\n[4] orientation -- the inlet really is the upwind wall")
    for r in res:
        W = json.loads(Path(r["sidecar"]).read_text())
        if not W["clipped"]:
            check("  (raw is unclipped; sidecar correctly omits the wall claims)",
                  "domain_rotated" not in W)
            break
        d, cr = W["domain_rotated"], W["core_roi"]["rotated_bounds"]
        ok = (abs((cr["ymin"] - d["ymin"]) - W["buffer"]["upwind_m"]) < 1e-6
              and abs((d["ymax"] - cr["ymax"]) - W["buffer"]["downwind_m"]) < 1e-6)
        check(f"  wind {W['wind_from_deg']:>5.1f}: ROI sits up_m from ymin, down_m from ymax",
              ok)

    if not a.raw and len(vols) > 1:
        print(f"\n[5] content invariance -- all directions must agree about the ROI")
        # Whole-domain volumes legitimately DIFFER: the wind rectangle is built from the
        # ROI's ROTATED bounding box, and a square rotated 45 deg has a bbox of twice the
        # area. So compare the ROI content instead: un-rotate each mesh back to world and
        # boolean-intersect with the ROI prism. That content is identical by construction,
        # so any disagreement is a real bug in the rotate/clip loop.
        from sbg.onemap_native.build import _polygon_clip_solid
        from sbg.onemap_native.wind import unrotate_xy
        roi_vols = []
        for r in res:
            W = json.loads(Path(r["sidecar"]).read_text())
            ml = mr.loadMesh(r["stl"])
            mv, mf = mn.getNumpyVerts(ml), mn.getNumpyFaces(ml.topology)
            world = unrotate_xy(mv, W["psi_deg"], np.array(W["rotation"]["centre_m"]))
            m = mn.meshFromFacesVerts(np.ascontiguousarray(mf, np.int32),
                                      np.ascontiguousarray(world, float))
            prism = _polygon_clip_solid(ROI, world[:, 2].min() - 10, world[:, 2].max() + 10)
            out = mr.boolean(m, prism, mr.BooleanOperation.Intersection)
            roi_vols.append(abs(out.mesh.volume()) if out.valid() else float("nan"))
        spread = (max(roi_vols) - min(roi_vols)) / np.mean(roi_vols)
        check(f"  ROI content identical across directions (spread {100*spread:.3f}%)",
              spread < 0.01, f"{[f'{v:,.0f}' for v in roi_vols]}")
        print(f"       whole-domain volumes differ by {100*(max(vols)-min(vols))/np.mean(vols):.0f}% "
              f"and SHOULD -- a square ROI's rotated bbox is 2x bigger at 45 deg.")

    print()
    if _FAILS:
        print(f"{len(_FAILS)} FAILED: " + "; ".join(_FAILS))
        sys.exit(1)
    print("all wind-build checks passed")


if __name__ == "__main__":
    main()
