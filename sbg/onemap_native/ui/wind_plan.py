"""Resolve a client's wind/buffer request into concrete geometry.

THE POINT OF THIS FILE: `/api/domain/preview` and `/api/stl/run` call the SAME
function, so what the user sees drawn on the map is, by construction, the domain that
gets built. The frontend does no trigonometry at all -- every ring and arrow it draws
comes from here, computed by the same `sbg.onemap_native.wind` the pipeline uses. Two
implementations of the rotation convention WILL diverge, and the failure mode is a
mis-georeferenced dose map that looks entirely plausible.

`plan_wind()` is pure (shapely/numpy + a cached height index); it raises ValueError on
bad input and the router turns that into a 400.
"""
from __future__ import annotations

from shapely.geometry import Polygon

from sbg.onemap_native.wind import (
    BUILD_MARGIN_M, DEFAULT_MULTIPLES, arrow_world, buffer_distances, build_envelope,
    check_envelope, flow_bearing_deg, normalise_dirs, roi_centre, rose, wind_rect,
)

# Above this the UI must warn: the build-once envelope grows fast with direction count
# and ROI size, and the user should see the number before committing.
WARN_ENVELOPE_KM2 = 6.0
# Separate from the absolute cap: a small ROI with tall buildings stays well under
# WARN_ENVELOPE_KM2 while the envelope is still tens of times its size, so nearly all
# the build cost is buffer terrain. Measured: a 0.077 km^2 ROI at p90 51 m gives 3.2
# km^2 -- 42x -- and never trips the absolute warning.
WARN_ENVELOPE_RATIO = 20.0
# Hard refusal. Measured: an 8.78 km^2 envelope builds in 36.8 s at 2.76 GB peak, and
# cost scales with AREA (the buffer is bare terrain, so it adds terrain triangles only).
# 40 km^2 is ~4.5x that -- past where this box's memory is comfortable.
MAX_ENVELOPE_KM2 = 40.0

_H_SOURCES = ("max", "p90", "median")


def _ring(poly: Polygon):
    return [[float(x), float(y)] for x, y in poly.exterior.coords]


def plan_wind(wind_req: dict, roi: Polygon, height_stats: dict | None = None) -> dict:
    """Resolve {dirs|rose, h_source, buffer_m, centre, z0_m} against an ROI polygon.

    Returns everything both callers need: the resolved directions and buffer, the build
    envelope, and per-direction rings/arrows in TRUE world coordinates for the 2D map.
    """
    req = dict(wind_req or {})

    # --- directions -----------------------------------------------------------------
    dirs = req.get("dirs") or req.get("directions")
    if not dirs and req.get("rose"):
        n = int(req["rose"])
        if n not in (4, 8, 16, 36):
            raise ValueError("rose must be 4, 8, 16 or 36")
        dirs = rose(n)
    if not dirs:
        raise ValueError("wind needs 'dirs' (bearings the wind blows FROM) or 'rose'")
    # Range-check the RAW values, before normalise_dirs' `% 360`. Otherwise a typo'd
    # 400 silently becomes 40 -- a wrong wind direction is invisible until a dose map
    # comes out rotated, so a fat-fingered bearing must be an error, not a wrap.
    for d in dirs:
        try:
            v = float(d)
        except (TypeError, ValueError):
            raise ValueError(f"wind direction {d!r} is not a number") from None
        if not (0.0 <= v <= 360.0):
            raise ValueError(f"wind direction {v:g} out of range [0, 360]")
    try:
        dirs = normalise_dirs(dirs)
    except (TypeError, ValueError) as e:
        raise ValueError(f"invalid wind directions: {e}") from e
    if len(dirs) > 36:
        raise ValueError(f"{len(dirs)} directions is more than the 36 supported")

    # --- buffer ---------------------------------------------------------------------
    override = req.get("buffer_m")
    if override is not None:
        missing = {"upwind", "downwind", "lateral"} - set(override)
        if missing:
            raise ValueError(f"buffer_m is missing {sorted(missing)}")
        for k in ("upwind", "downwind", "lateral"):
            v = float(override[k])
            if not (0.0 <= v <= 20000.0):
                raise ValueError(f"buffer_m.{k}={v} out of range [0, 20000] m")

    h_source = req.get("h_source", "p90")
    if h_source not in _H_SOURCES:
        raise ValueError(f"h_source must be one of {_H_SOURCES}")
    hs = height_stats or {}
    h_m = float(hs.get(h_source) or 0.0)

    up_m, down_m, lat_m, warnings = buffer_distances(h_m, override_m=override)

    # --- geometry -------------------------------------------------------------------
    centre = req.get("centre")
    centre = ([float(centre[0]), float(centre[1])] if centre
              else roi_centre(roi).tolist())

    env = build_envelope(roi, centre, dirs, up_m, down_m, lat_m)
    check_envelope(env, roi, centre, dirs, up_m, down_m, lat_m)   # raises if too tight

    env_km2 = env.area / 1e6
    if env_km2 > MAX_ENVELOPE_KM2:
        raise ValueError(
            f"build envelope is {env_km2:.1f} km^2, above the {MAX_ENVELOPE_KM2:g} km^2 "
            f"limit. Shrink the ROI, reduce the direction count, or set buffer_m "
            f"explicitly.")
    roi_km2 = roi.area / 1e6
    ratio = env_km2 / max(roi_km2, 1e-9)
    if env_km2 > WARN_ENVELOPE_KM2:
        warnings.append(f"build envelope is {env_km2:.1f} km^2 ({ratio:.1f}x the ROI) -- "
                        f"expect a long build and high memory use")
    elif ratio > WARN_ENVELOPE_RATIO:
        # Absolute area alone misses this: a SMALL ROI with tall buildings gets an
        # envelope tens of times its size and stays under the km^2 warning, yet nearly
        # all of the build cost is buffer terrain the user did not ask for. The buffer
        # is physically correct (COST 732 is absolute multiples of H) -- worth saying,
        # not worth refusing.
        warnings.append(f"envelope is {ratio:.0f}x your area of interest "
                        f"({roi_km2:.2f} -> {env_km2:.2f} km^2) -- most of the build is "
                        f"buffer terrain. A larger ROI or a smaller buffer is cheaper.")

    # Arrow length scales with the domain so it stays legible at any zoom.
    xmin, ymin, xmax, ymax = env.bounds
    arrow_len = 0.25 * max(xmax - xmin, ymax - ymin)

    directions = []
    for wf in dirs:
        psi = flow_bearing_deg(wf)
        rect_rot, rect_world = wind_rect(roi, centre, psi, up_m, down_m, lat_m)
        tail, head = arrow_world(centre, psi, arrow_len)
        directions.append({
            "wind_from_deg": wf,
            "psi_deg": psi,
            # NOTE the per-direction area legitimately varies with bearing: the rect is
            # built from the ROI's ROTATED bounding box, and a square rotated 45 deg has
            # a bbox of twice the area. A roughly circular ROI avoids it.
            "area_km2": round(rect_world.area / 1e6, 4),
            "rect_world": _ring(rect_world),
            "arrow_world": [tail, head],
            "filename": f"wind_{int(round(wf)) % 360:03d}.stl",
        })

    return {
        "dirs": dirs,
        "centre": centre,
        "buffer_m": {"upwind": round(up_m, 2), "downwind": round(down_m, 2),
                     "lateral": round(lat_m, 2)},
        "buffer_multiples": DEFAULT_MULTIPLES if override is None else None,
        "buffer_source": "manual" if override is not None else h_source,
        "h_m": round(h_m, 2),
        "h_stats": hs or None,
        "z0_m": float(req.get("z0_m", 0.7)),
        "envelope_world": _ring(env),
        "envelope_km2": round(env_km2, 4),
        "envelope_margin_m": BUILD_MARGIN_M,
        "directions": directions,
        "warnings": warnings,
        # What run_stl_job passes straight through to build_domain_stl.
        "build_kwargs": {"dirs": dirs, "centre": centre, "up_m": up_m,
                         "down_m": down_m, "lat_m": lat_m,
                         "z0_m": float(req.get("z0_m", 0.7))},
        "envelope": env,      # shapely, stripped before JSON
    }
