"""Verify the wind-domain geometry. Pure geometry, no meshlib, runs in ~1 s.

    .venv/bin/python -m sbg.onemap_native.verify_wind

Exits non-zero on any failure. This project has no pytest suite by convention
(verification is direct), so this is a self-contained runnable check.

The load-bearing test is CONVENTION, not round-tripping. A round trip passes even when
the rotation sign is flipped -- forward and inverse are still consistent with each other,
they are just both wrong. Only an absolute statement about which side the inlet lands on
catches that, and a sign error here stays invisible until a dose map comes out 90 degrees
wrong. See `check_convention`.
"""
import sys

import numpy as np
from shapely.geometry import Polygon, box

from sbg.onemap_native.wind import (
    BUILD_MARGIN_M, arrow_world, build_envelope, buffer_distances, check_envelope,
    flow_bearing_deg, roi_centre, normalise_dirs, rose, rot2, rotate_xy, unrotate_xy,
    wind_rect,
)

_FAILS: list[str] = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}   {detail}")
        _FAILS.append(name)


# ---------------------------------------------------------------- the convention
def check_convention():
    print("\n[convention] flow ends up along +Y, and the inlet is on the upwind side")

    # R(psi) @ (sin psi, cos psi) == (0,1) for every psi
    worst = 0.0
    for psi in range(0, 360):
        f = np.array([np.sin(np.deg2rad(psi)), np.cos(np.deg2rad(psi))])
        worst = max(worst, float(np.abs(rot2(psi) @ f - np.array([0.0, 1.0])).max()))
    check("R(psi) maps the flow vector to +Y for all 360 bearings", worst < 1e-12,
          f"max deviation {worst:.2e}")

    # The absolute one: with wind FROM a given side, that side must become ymin.
    roi = box(-100.0, -100.0, 100.0, 100.0)
    c = roi_centre(roi)
    probes = {"north": [0.0, 90.0], "south": [0.0, -90.0],
              "east": [90.0, 0.0], "west": [-90.0, 0.0]}
    for wind_from, upwind_side in [(0.0, "north"), (180.0, "south"),
                                   (90.0, "east"), (270.0, "west")]:
        psi = flow_bearing_deg(wind_from)
        ys = {k: rotate_xy(np.array([p]), psi, c)[0, 1] for k, p in probes.items()}
        lowest = min(ys, key=ys.get)
        check(f"wind from {wind_from:5.1f} ({upwind_side:5s}) -> {upwind_side} side is ymin",
              lowest == upwind_side, f"got {lowest}; ys={ {k: round(v,1) for k,v in ys.items()} }")

    # ...and the rectangle agrees: the ROI centre sits `up` from ymin, `down` from ymax.
    up, down, lat = 250.0, 750.0, 250.0
    for wind_from in [0.0, 45.0, 90.0, 217.3]:
        psi = flow_bearing_deg(wind_from)
        rect_rot, _ = wind_rect(roi, c, psi, up, down, lat)
        x0, y0, x1, y1 = rect_rot.bounds
        r = rotate_xy(np.asarray(roi.exterior.coords)[:, :2], psi, c)
        ok = (abs((r[:, 1].min() - y0) - up) < 1e-9 and abs((y1 - r[:, 1].max()) - down) < 1e-9
              and abs((r[:, 0].min() - x0) - lat) < 1e-9 and abs((x1 - r[:, 0].max()) - lat) < 1e-9)
        check(f"wind from {wind_from:5.1f}: rect margins are exactly up/down/lat", ok)


# ---------------------------------------------------------------- round trip
def check_round_trip():
    print("\n[round trip] forward then inverse returns the original point")
    rng = np.random.default_rng(0)
    roi = box(21950.0, 30250.0, 22850.0, 31150.0)   # real SVY21 magnitudes
    c = roi_centre(roi)
    pts = np.column_stack([rng.uniform(21000, 23800, 10000),
                           rng.uniform(29300, 32100, 10000),
                           rng.uniform(0, 150, 10000)])
    worst = 0.0
    for wf in rose(16):
        psi = flow_bearing_deg(wf)
        back = unrotate_xy(rotate_xy(pts, psi, c), psi, c)
        worst = max(worst, float(np.abs(back - pts).max()))
    check("10k points x 16 directions round-trip to < 1e-6 m", worst < 1e-6,
          f"max error {worst:.3e} m")

    z_moved = float(np.abs(rotate_xy(pts, 37.0, c)[:, 2] - pts[:, 2]).max())
    check("z is never touched by the rotation", z_moved == 0.0, f"max dz {z_moved}")


# ---------------------------------------------------------------- envelope
def check_envelope_geometry():
    print("\n[envelope] contains every rectangle, with clearance, and stays a clean polygon")
    roi = box(21950.0, 30250.0, 22850.0, 31150.0)
    c = roi_centre(roi)
    up, down, lat = 250.0, 750.0, 250.0

    for n in (4, 8, 16):
        dirs = rose(n)
        env = build_envelope(roi, c, dirs, up, down, lat)
        try:
            check_envelope(env, roi, c, dirs, up, down, lat)
            ok, detail = True, ""
        except ValueError as e:
            ok, detail = False, str(e)
        check(f"{n:2d} directions: every rect contained with >= {BUILD_MARGIN_M} m clearance",
              ok, detail)
        check(f"{n:2d} directions: envelope is a single Polygon",
              env.geom_type == "Polygon" and env.is_valid)
        # A default-join_style buffer would round the corners into dozens of segments.
        nv = len(env.exterior.coords) - 1
        check(f"{n:2d} directions: no rounded corners (mitre join), {nv} vertices <= {4*n+8}",
              nv <= 4 * n + 8, f"got {nv}")

    # Zero margin must actually touch -- proves the clearance test can fail.
    env0 = build_envelope(roi, c, rose(8), up, down, lat, margin_m=0.0)
    try:
        check_envelope(env0, roi, c, rose(8), up, down, lat, margin_m=BUILD_MARGIN_M)
        touched = False
    except ValueError:
        touched = True
    check("margin=0 is correctly REJECTED by the clearance check (the test has teeth)",
          touched)

    # Build-once really is cheaper than N separate domains.
    dirs = rose(16)
    env = build_envelope(roi, c, dirs, up, down, lat)
    per = sum(wind_rect(roi, c, flow_bearing_deg(w), up, down, lat)[1].area for w in dirs)
    print(f"        envelope {env.area/1e6:.2f} km^2  vs  16 separate rects "
          f"{per/1e6:.2f} km^2  ({per/env.area:.1f}x saving)")
    check("build-once envelope is much smaller than the sum of per-direction domains",
          env.area < per / 3.0)


# ---------------------------------------------------------------- buffer sizing
def check_buffer():
    print("\n[buffer] COST 732 multiples, floor, cap, and override")
    up, down, lat, w = buffer_distances(50.0)
    check("H=50 -> 250/750/250", (up, down, lat) == (250.0, 750.0, 250.0) and not w)

    up, down, lat, w = buffer_distances(2.0)
    check("H below the floor is raised to H_MIN with a warning",
          up == 50.0 and len(w) == 1, f"{up} {w}")

    up, down, lat, w = buffer_distances(200.0)
    check("H above the cap is capped with a warning",
          down == 15.0 * 60.0 and len(w) == 1, f"{down} {w}")

    up, down, lat, w = buffer_distances(200.0, override_m={"upwind": 100.0,
                                                           "downwind": 300.0,
                                                           "lateral": 120.0})
    check("an explicit override wins outright and warns about nothing",
          (up, down, lat) == (100.0, 300.0, 120.0) and not w)

    up, _, _, w = buffer_distances(float("nan"))
    check("no buildings (NaN H) falls back to the floor", up == 50.0 and len(w) == 1)


# ---------------------------------------------------------------- misc
def check_misc():
    print("\n[misc] direction sets, arrow, non-square ROI")
    check("normalise_dirs dedupes, wraps and sorts",
          normalise_dirs([370.0, 10.0, 90.0, -270.0]) == [10.0, 90.0])
    check("rose(8) is the standard 8-sector set",
          rose(8) == [0.0, 45.0, 90.0, 135.0, 180.0, 225.0, 270.0, 315.0])

    roi = box(0.0, 0.0, 100.0, 100.0)
    c = roi_centre(roi)
    a, b = arrow_world(c, flow_bearing_deg(0.0), 100.0)   # wind from north -> flows south
    check("arrow points downstream (wind from north -> arrow head is to the south)",
          b[1] < a[1], f"{a} -> {b}")

    # Concave ROI: the rect must still swallow the whole thing for every direction.
    L = Polygon([(0, 0), (200, 0), (200, 60), (60, 60), (60, 200), (0, 200)])
    cL = roi_centre(L)
    for wf in rose(8):
        _, rw = wind_rect(L, cL, flow_bearing_deg(wf), 50.0, 150.0, 50.0)
        if not rw.contains(L):
            check(f"L-shaped ROI fully inside its wind rect (wind from {wf})", False)
            break
    else:
        check("L-shaped ROI fully inside its wind rect for all 8 directions", True)

    # The domain must not depend on WHERE we chose to put the rotation origin -- only
    # the arrow and the sidecar numbers do. Proves centre is a free choice.
    worst = 0.0
    for wf in rose(8):
        psi = flow_bearing_deg(wf)
        a = np.asarray(wind_rect(L, cL, psi, 50.0, 150.0, 50.0)[1].exterior.coords)
        b = np.asarray(wind_rect(L, cL + np.array([731.0, -412.0]), psi,
                                 50.0, 150.0, 50.0)[1].exterior.coords)
        worst = max(worst, float(np.abs(np.sort(a, axis=0) - np.sort(b, axis=0)).max()))
    check("world rect is independent of the rotation origin (centre is a free choice)",
          worst < 1e-6, f"max corner shift {worst:.3e} m")


if __name__ == "__main__":
    check_convention()
    check_round_trip()
    check_envelope_geometry()
    check_buffer()
    check_misc()
    print()
    if _FAILS:
        print(f"{len(_FAILS)} FAILED: " + ", ".join(_FAILS))
        sys.exit(1)
    print("all wind-geometry checks passed")
