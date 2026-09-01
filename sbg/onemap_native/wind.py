"""Wind-aligned CFD domains: rotation convention, buffer sizing, domain rectangles.

Pure shapely/numpy. No meshlib, no I/O, no network -- so it is unit-testable in
milliseconds and is the SINGLE SOURCE OF TRUTH for the geometry, shared by the build
pipeline and the `/api/domain/preview` endpoint. The frontend must never recompute any
of this: two implementations of the rotation convention will diverge, and the result is
a mis-georeferenced dose map that looks entirely plausible.

WHY ROTATE AT ALL
    Fluent's "Magnitude, Normal to Boundary" velocity inlet blows perpendicular to a
    face, so on an axis-aligned box only the four grid-aligned wind directions are
    reachable without rotating the mesh by hand. Emitting geometry already rotated so
    the flow is always +Y means the Fluent setup NEVER changes -- inlet is always ymin,
    outlet always ymax -- and 45 degrees is exactly as easy as 0. It also lets the
    buffer be sized asymmetrically along-wind (5H upwind / 15H downwind), which is ~3x
    less mesh than a direction-agnostic symmetric buffer.

THE CONVENTION (get this wrong and everything downstream is silently 90 degrees off)
    wind_from_deg -- METEOROLOGICAL: the compass bearing the wind blows FROM,
                     clockwise from grid north. "Wind from the north" = 0.
    psi_deg       -- (wind_from_deg + 180) % 360, the bearing the flow travels TOWARD.
                     This is the CCW rotation applied to world XY.

    A bearing b is the unit vector (sin b, cos b) in (easting, northing) = (x, y).
    R(psi) is CCW by psi. Then R(psi) @ (sin psi, cos psi) == (0, 1) identically, i.e.
    the flow ends up along +Y. Worked example: wind from north -> psi = 180 -> flow
    vector (0,-1) -> R(180) @ (0,-1) = (0,1). And a point NORTH of centre maps to
    SMALLER rotated y, so the upwind (north) side really is ymin. That last sentence is
    the assertion in the tests -- it is what catches a sign error.

    Rotation is about the ROI centre with ZERO translation, so the centre is a fixed
    point, coordinates stay at SVY21 magnitude, and dose/stl_to_h5m.py's own recentring
    behaves identically. z is never touched.
"""
from __future__ import annotations

import numpy as np
from shapely.affinity import rotate as _shapely_rotate
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

# --- buffer sizing -----------------------------------------------------------------
# COST 732 / Franke et al. (2007) best practice for urban CFD.
DEFAULT_MULTIPLES = {"upwind": 5.0, "downwind": 15.0, "lateral": 5.0}

H_MIN_M = 10.0    # floor: an ROI over open field/water has no buildings at all
H_CAP_M = 60.0    # above this, auto sizing is refused -- 15H on a 200 m tower is 3 km
                  # downwind, and an 8-direction hull then spans ~6 km (tens of km^2).

# The build envelope is pushed out by this much. NOT cosmetic: the convex hull of the
# wind rectangles has boundary segments EXACTLY coincident with the outer edges of the
# extreme-direction rectangles, and clipping with a prism wall coplanar to
# terrain_flat_base_solid's own outer wall is the degenerate exact-CSG case that
# produces collinear zero-area slivers (measured as 8.00/16.00 m bad edges; see the
# SBG_CLIP_NUDGE_M work). 5 m is >> that 1 mm nudge and << any physical effect.
BUILD_MARGIN_M = 5.0


def flow_bearing_deg(wind_from_deg: float) -> float:
    """Meteorological 'wind from' bearing -> the bearing the flow travels toward."""
    return float((float(wind_from_deg) + 180.0) % 360.0)


def rot2(psi_deg: float) -> np.ndarray:
    """CCW rotation matrix by psi (degrees). R(psi) @ (sin psi, cos psi) == (0, 1)."""
    a = np.deg2rad(float(psi_deg))
    c, s = np.cos(a), np.sin(a)
    return np.array([[c, -s], [s, c]], dtype=float)


def rotate_xy(pts, psi_deg: float, centre) -> np.ndarray:
    """Rotate Nx2 (or Nx3 -- z passes through) about `centre` by psi, CCW."""
    p = np.asarray(pts, dtype=float)
    c = np.asarray(centre, dtype=float)[:2]
    xy = p[:, :2] - c
    out = p.copy()
    out[:, :2] = xy @ rot2(psi_deg).T + c
    return out


def unrotate_xy(pts, psi_deg: float, centre) -> np.ndarray:
    """Inverse of rotate_xy: rotated frame -> true world."""
    return rotate_xy(pts, -float(psi_deg), centre)


def buffer_distances(h_m: float, multiples=None, override_m=None):
    """(up_m, down_m, lat_m, warnings) from a building height.

    `override_m` (a dict with upwind/downwind/lateral in metres) wins outright and
    skips every check -- an explicit user choice is not second-guessed.
    """
    warnings: list[str] = []
    if override_m:
        return (float(override_m["upwind"]), float(override_m["downwind"]),
                float(override_m["lateral"]), warnings)

    m = dict(DEFAULT_MULTIPLES if multiples is None else multiples)
    h = float(h_m)
    if not np.isfinite(h) or h <= 0.0:
        warnings.append(f"no building height available; using the {H_MIN_M:g} m floor")
        h = H_MIN_M
    elif h < H_MIN_M:
        warnings.append(f"tallest building is {h:.1f} m; using the {H_MIN_M:g} m floor")
        h = H_MIN_M
    if h > H_CAP_M:
        warnings.append(
            f"tallest building is {h:.1f} m, above the {H_CAP_M:g} m auto-sizing cap "
            f"({m['downwind']:g}H would be {m['downwind'] * h:.0f} m downwind). "
            f"Capped at {H_CAP_M:g} m -- set the buffer explicitly to override.")
        h = H_CAP_M
    return h * m["upwind"], h * m["downwind"], h * m["lateral"], warnings


def roi_centre(roi_poly: Polygon) -> np.ndarray:
    """Default domain centre: the ROI's bounding-box centre.

    The choice of centre does NOT change the domain: `wind_rect` derives the rectangle
    from the ROI's rotated bounding box, and rotating about a different origin shifts
    everything by a constant that the inverse rotation cancels exactly -- so rect_world
    is provably independent of `centre` (verified in verify_wind.py). What it DOES affect
    is where the wind arrow is drawn and the numbers in the sidecar's `rotation` block.

    Bbox centre rather than centroid because it is the middle of the extent the domain is
    actually built around. Note that for a concave ROI (an L, say) EITHER can fall outside
    the polygon, which is why the UI lets the user drag it.
    """
    xmin, ymin, xmax, ymax = roi_poly.bounds
    return np.array([0.5 * (xmin + xmax), 0.5 * (ymin + ymax)], dtype=float)


def wind_rect(roi_poly: Polygon, centre, psi_deg: float,
              up_m: float, down_m: float, lat_m: float):
    """The CFD domain rectangle for one wind direction.

    Returns (rect_rot, rect_world):
      rect_rot   -- axis-aligned shapely box IN THE ROTATED FRAME. Inlet at ymin,
                    outlet at ymax, laterals at xmin/xmax. This is what the clip uses,
                    which is why the mesh is rotated BEFORE clipping: an axis-aligned
                    box is a 12-triangle mr.makeCube rather than an extruded rotated
                    quad, so there are fewer near-degenerate boolean configurations.
      rect_world -- the same rectangle back in true SVY21, for drawing on the 2D map.

    Built analytically from shapely.box, never Polygon.buffer(): buffer rounds corners
    into many segments, and _polygon_clip_solid needs exactly 4 edges to give clean
    vertical CFD walls.
    """
    c = np.asarray(centre, dtype=float)[:2]
    ring = np.asarray(roi_poly.exterior.coords, dtype=float)[:, :2]
    r = rotate_xy(ring, psi_deg, c)
    x0, y0 = r[:, 0].min(), r[:, 1].min()
    x1, y1 = r[:, 0].max(), r[:, 1].max()
    rect_rot = box(x0 - lat_m, y0 - up_m, x1 + lat_m, y1 + down_m)
    corners = np.asarray(rect_rot.exterior.coords, dtype=float)
    rect_world = Polygon(unrotate_xy(corners, psi_deg, c))
    return rect_rot, rect_world


def build_envelope(roi_poly: Polygon, centre, wind_from_degs, up_m, down_m, lat_m,
                   margin_m: float = BUILD_MARGIN_M) -> Polygon:
    """One polygon containing EVERY direction's wind rectangle -- the 'build once' domain.

    Convex hull of the N rectangles rather than a bounding disc: for 8 directions the
    hull is ~30-40% smaller in area, has <= 4N vertices, and stays convex so both the
    CDT and the clip behave. Then pushed out by `margin_m` -- see BUILD_MARGIN_M.
    """
    c = np.asarray(centre, dtype=float)[:2]
    rects = [wind_rect(roi_poly, c, flow_bearing_deg(w), up_m, down_m, lat_m)[1]
             for w in wind_from_degs]
    hull = unary_union(rects).convex_hull
    return hull.buffer(margin_m, join_style=2) if margin_m > 0 else hull


def check_envelope(env: Polygon, roi_poly: Polygon, centre, wind_from_degs,
                   up_m, down_m, lat_m, margin_m: float = BUILD_MARGIN_M) -> None:
    """Assert the envelope really contains every rectangle with clearance to spare.

    The clearance half is the load-bearing one: if any rectangle's edge lies ON the
    envelope boundary, its clip wall is coplanar with the terrain solid's outer wall and
    watertightness fails intermittently in a way that is very hard to trace back here.
    """
    c = np.asarray(centre, dtype=float)[:2]
    for w in wind_from_degs:
        _, rw = wind_rect(roi_poly, c, flow_bearing_deg(w), up_m, down_m, lat_m)
        if not env.contains(rw):
            raise ValueError(f"build envelope does not contain the wind rectangle "
                             f"for wind_from={w}")
        gap = env.exterior.distance(rw.exterior)
        if gap < margin_m - 1e-6:
            raise ValueError(f"wind rectangle for wind_from={w} comes within {gap:.4f} m "
                             f"of the envelope boundary (need >= {margin_m} m); a "
                             f"coincident clip wall will break watertightness")


def arrow_world(centre, psi_deg: float, length_m: float):
    """Two world-frame points for the map's wind arrow, pointing DOWNSTREAM."""
    c = np.asarray(centre, dtype=float)[:2]
    f = np.array([np.sin(np.deg2rad(psi_deg)), np.cos(np.deg2rad(psi_deg))])
    return (c - 0.5 * length_m * f).tolist(), (c + 0.5 * length_m * f).tolist()


def normalise_dirs(wind_from_degs) -> list[float]:
    """Coerce to floats in [0,360), dedupe, sort. Raises on an empty/invalid set."""
    out = sorted({float(d) % 360.0 for d in wind_from_degs})
    if not out:
        raise ValueError("no wind directions given")
    return out


def rose(n: int) -> list[float]:
    """The standard n-sector wind rose (4, 8, 16 ...) as wind_from bearings."""
    if n <= 0:
        raise ValueError("rose(n) needs n >= 1")
    return [round(i * 360.0 / n, 6) for i in range(n)]
