"""GeoTIFF DEM/DSM export ("JAEA mode") -- one height per pixel, sampled from the mesh.

The JAEA dose code takes terrain as a GeoTIFF where each pixel is a height. Nothing else
in this repo emits one: `sbg.topo.dtm.write_geotiff` writes the bare 20 m island DTM, and
the v2 pipeline's only outputs are STLs.

WHY THE RAW SOUP IS THE RIGHT SOURCE (and the watertight STL is not)
--------------------------------------------------------------------
A downward max never asks whether the mesh is closed, whether winding is consistent, or
whether triangles intersect -- it only ever keeps the topmost surface. So raw's open
bottoms, its double-sided soup and its self-intersections are all invisible here. The mesh
that FAILS the Ansys watertight workflow is therefore the best DEM source available: exact
captured geometry, with no 2 m voxel staircase and no 2.5 m decimation error baked into
every roof. Sampling the finished STL instead would be strictly worse for no gain.

Nor should this be built from footprints. That instinct is right for v1, where geometry was
extruded FROM the footprint so the two agreed by construction, and inverted for v2:
`extract._footprint_rings` is a convex hull of scattered near-base points, measured in this
project at 4.3x SMALLER than the true cross-section (p10 1.9x, p90 14.6x), frequently
disconnected fragments in the wrong place, and one real building at hull area 0 against a
true 227 m^2. Rasterising those gives buildings too small, misplaced, or missing.

Vertical walls need no special case: they project to zero area looking down, so they fall
out of the barycentric test on their own, and their top edge is shared with a roof triangle
that does get sampled. The same is true of the terrain slab's bottom cap and side walls --
they sit below the surface, so the max ignores them.

THE TWO COLLAPSES
-----------------
Squashing a 3D solid into one-height-per-pixel is two independent problems, and only the
second is about mean/median:

(1) HORIZONTAL -- `agg`. A 4 m cell straddling a building edge contains both roof and
    ground, so its height distribution is bimodal and percentile p is exactly a coverage
    threshold at (100-p)%. `median` is therefore precisely "is more than half of this cell
    inside the building", which is unbiased in footprint area. `max` dilates every building
    by up to half a pixel on every side -- at 4 m pixels that is a systematic ~+40% footprint
    area on a 20 m building. `mean` is available but not advised: it invents heights that
    exist nowhere and ramps every building edge. A sub-pixel mast vanishes under `median`
    and becomes a full solid 4 m block under `max`; vanishing is the right answer at a 4 m
    grid, where it is sub-grid anyway.

(2) VERTICAL -- `overhang`. A heightfield cannot express solid/void/solid, so under a bridge
    or canopy `max` fills all the way to the ground. Detected cheaply by rasterising a MIN
    alongside the MAX over building triangles only: if a column's lowest building geometry
    floats more than OVERHANG_M above the terrain, nothing solid touches the ground there.
    No normals, no winding, no ray intervals -- which matters, because winding is exactly
    what is unreliable in double-sided OneMap soup. `keep` is the classic DSM and is
    conservative for shielding; `drop` reverts those columns to terrain, which is correct
    for flow. The affected pixel fraction is logged either way, so the choice is made on a
    measurement rather than in the abstract.

Both are reported, not just chosen: every run logs the implied building volume under the
selected `agg` AND under `max`, which is the number that actually settles whether the
default matters for a given domain.

Standalone use (a DEM from an STL already on disk, no rebuild):
    .venv/bin/python -m sbg.onemap_native.dem domain.raw.stl -o dem.tif --px 4
"""
import numpy as np
from affine import Affine

# A column whose lowest building geometry floats more than this above the terrain has open
# air beneath it. Generous on purpose: real buildings reach the ground via walls and plunge
# skirts, so the gap for a genuine building is ~0, while a bridge deck or canopy clears by
# many metres. Anything between is ambiguous and is treated as a building (kept).
OVERHANG_M = 2.0

# Named reductions -> percentile. `mean` is handled separately (it is not a percentile).
_AGG_PCT = {"min": 0.0, "p10": 10.0, "median": 50.0, "p50": 50.0,
            "p90": 90.0, "p95": 95.0, "max": 100.0}
AGGS = tuple(_AGG_PCT) + ("mean",)
OVERHANGS = ("keep", "drop")
CRS_CHOICES = (3414, 4326)

# GeoTIFF nodata sentinel. Never actually appears when outside-the-domain pixels are filled
# from the island DTM (the default), but it is declared so a reader has one.
NODATA = -9999.0


# --------------------------------------------------------------------------------------
# rasterisation
# --------------------------------------------------------------------------------------

def rasterize_z(verts, faces, affine, shape, want_min=False, chunk=4_000_000):
    """Top-down per-pixel max (and optionally min) z of a triangle mesh.

    Returns (zmax, zmin) as (nrows, ncols) float64 arrays; uncovered pixels are -inf / +inf,
    and `zmin` is None unless `want_min`. Both are seeded with +-inf and NEVER NaN: this
    project has already been bitten by `np.maximum.at` into a NaN-filled array, which stays
    NaN forever because max(nan, x) is nan.

    A pixel is covered iff its CENTRE falls inside the triangle, which is what makes the
    result agree with `rasterio.sample` at the same world coordinate.
    """
    nrows, ncols = shape
    zmax = np.full(nrows * ncols, -np.inf)
    zmin = np.full(nrows * ncols, np.inf) if want_min else None
    faces = np.asarray(faces)
    if len(faces) == 0:
        return zmax.reshape(shape), (zmin.reshape(shape) if want_min else None)

    verts = np.asarray(verts, dtype=np.float64)
    inv = ~affine
    # world -> continuous pixel index, measured from the pixel CORNER, so the centre of
    # pixel j sits at j + 0.5. Vectorised affine application, as terrain._rasterize_pads.
    pcol, prow = inv * (verts[:, 0], verts[:, 1])
    P = np.column_stack([np.asarray(pcol), np.asarray(prow)])
    Z = verts[:, 2]

    ax, ay = P[faces[:, 0], 0], P[faces[:, 0], 1]
    bx, by = P[faces[:, 1], 0], P[faces[:, 1], 1]
    cx, cy = P[faces[:, 2], 0], P[faces[:, 2], 1]
    za, zb, zc = Z[faces[:, 0]], Z[faces[:, 1]], Z[faces[:, 2]]

    # Twice the signed area in pixel space. A vertical wall projects to a line, so this is
    # ~0 and the triangle is skipped -- which is exactly right for a top-down max: the wall
    # contributes no horizontal extent, and its top edge belongs to a roof triangle anyway.
    det = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
    ok = np.abs(det) > 1e-12

    # candidate pixel-index range per triangle: centres (j + 0.5) inside the vertex bbox
    lo_c = np.ceil(np.minimum(np.minimum(ax, bx), cx) - 0.5)
    hi_c = np.floor(np.maximum(np.maximum(ax, bx), cx) - 0.5)
    lo_r = np.ceil(np.minimum(np.minimum(ay, by), cy) - 0.5)
    hi_r = np.floor(np.maximum(np.maximum(ay, by), cy) - 0.5)
    c0 = np.clip(lo_c, 0, ncols - 1).astype(np.int64)
    c1 = np.clip(hi_c, 0, ncols - 1).astype(np.int64)
    r0 = np.clip(lo_r, 0, nrows - 1).astype(np.int64)
    r1 = np.clip(hi_r, 0, nrows - 1).astype(np.int64)
    # drop triangles entirely off-grid as well as degenerate ones
    ok &= (hi_c >= lo_c) & (hi_r >= lo_r) & (hi_c >= 0) & (hi_r >= 0)
    ok &= (lo_c <= ncols - 1) & (lo_r <= nrows - 1)

    w = np.where(ok, c1 - c0 + 1, 0)
    h = np.where(ok, r1 - r0 + 1, 0)
    n = (w * h).astype(np.int64)
    live = np.nonzero(n > 0)[0]
    if len(live) == 0:
        return zmax.reshape(shape), (zmin.reshape(shape) if want_min else None)

    # chunk over triangles so the candidate array stays bounded, never letting a chunk be
    # empty (one triangle may on its own exceed `chunk` -- a domain-spanning terrain face).
    cum = np.cumsum(n[live])
    starts, i = [], 0
    while i < len(live):
        j = int(np.searchsorted(cum, cum[i - 1] + chunk if i else chunk, side="right"))
        j = max(j, i + 1)
        starts.append((i, min(j, len(live))))
        i = j

    for s, e in starts:
        t = live[s:e]
        nt = n[t]
        tri = np.repeat(t, nt)
        # offset of each candidate within its own triangle's block
        base = np.concatenate([[0], np.cumsum(nt)[:-1]])
        k = np.arange(int(nt.sum())) - np.repeat(base, nt)
        wt = np.repeat(w[t], nt)
        col = np.repeat(c0[t], nt) + (k % wt)
        row = np.repeat(r0[t], nt) + (k // wt)

        px_, py_ = col + 0.5, row + 0.5
        d = det[tri]
        l1 = ((by[tri] - cy[tri]) * (px_ - cx[tri]) + (cx[tri] - bx[tri]) * (py_ - cy[tri])) / d
        l2 = ((cy[tri] - ay[tri]) * (px_ - cx[tri]) + (ax[tri] - cx[tri]) * (py_ - cy[tri])) / d
        l3 = 1.0 - l1 - l2
        eps = -1e-9
        inside = (l1 >= eps) & (l2 >= eps) & (l3 >= eps)
        if not inside.any():
            continue
        z = l1 * za[tri] + l2 * zb[tri] + l3 * zc[tri]
        flat = row * ncols + col
        np.maximum.at(zmax, flat[inside], z[inside])
        if want_min:
            np.minimum.at(zmin, flat[inside], z[inside])

    return zmax.reshape(shape), (zmin.reshape(shape) if want_min else None)


def _reduce(sub, k, agg, row_block=256):
    """Collapse a (nrows*k, ncols*k) subgrid to (nrows, ncols) by percentile or mean.

    Blocked over rows: the intermediate is (rows, ncols, k*k), which at k=4 on a large
    domain would otherwise be a few hundred MB in one allocation.
    """
    nrows, ncols = sub.shape[0] // k, sub.shape[1] // k
    out = np.empty((nrows, ncols), dtype=np.float64)
    pct = None if agg == "mean" else _AGG_PCT[agg]
    for a in range(0, nrows, row_block):
        b = min(nrows, a + row_block)
        blk = (sub[a * k:b * k, :]
               .reshape(b - a, k, ncols, k).transpose(0, 2, 1, 3).reshape(b - a, ncols, k * k))
        if agg == "mean":
            out[a:b] = blk.mean(axis=2)
        elif pct == 100.0:
            out[a:b] = blk.max(axis=2)
        elif pct == 0.0:
            out[a:b] = blk.min(axis=2)
        else:
            # method="nearest", NOT the default linear interpolation. A cell that is exactly
            # half roof and half ground would otherwise average to a height that exists
            # nowhere -- mid-air, halfway up the wall -- which is precisely the `mean`
            # pathology this reduction exists to avoid. Every output height must be a height
            # something in the mesh actually has.
            out[a:b] = np.percentile(blk, pct, axis=2, method="nearest")
    return out


def grid_for(bounds, px, snap=True):
    """north-up affine + (nrows, ncols) covering `bounds`, origin snapped to a px multiple.

    Snapping means two runs over different extents share one grid lattice, and the tie point
    is a round number. Only meaningful in metres, so callers pass snap=False for degrees.
    """
    xmin, ymin, xmax, ymax = bounds
    x0 = np.floor(xmin / px) * px if snap else xmin
    y1 = np.ceil(ymax / px) * px if snap else ymax
    ncols = max(1, int(np.ceil((xmax - x0) / px)))
    nrows = max(1, int(np.ceil((y1 - ymin) / px)))
    return Affine(px, 0.0, float(x0), 0.0, -px, float(y1)), (nrows, ncols)


# --------------------------------------------------------------------------------------
# the DEM itself
# --------------------------------------------------------------------------------------

def build_dem(terr_v, terr_f, bld_v, bld_f, domain_polygon, dtm, *, px_m=4.0, crs=3414,
              agg="median", overhang="keep", supersample=4, source="surface", log=print):
    """Sample the scene into a north-up heightfield. Returns (grid, affine, meta).

    `dtm` is a terrain.DtmSampler over the bare island DTM, in EPSG:3414 always -- it fills
    pixels no mesh covers (the corners a north-up raster necessarily has over a rotated or
    non-rectangular domain), so the raster has no holes for a reader to mishandle. That is
    honest: it IS real terrain, just outside the built CFD domain. No buildings there.
    """
    if agg not in AGGS:
        raise ValueError(f"dem_agg must be one of {AGGS}, got {agg!r}")
    if overhang not in OVERHANGS:
        raise ValueError(f"dem_overhang must be one of {OVERHANGS}, got {overhang!r}")
    if int(crs) not in CRS_CHOICES:
        raise ValueError(f"dem_crs must be one of {CRS_CHOICES}, got {crs!r}")
    k = max(1, int(supersample))
    crs = int(crs)

    terr_v = np.asarray(terr_v, dtype=np.float64).reshape(-1, 3)
    bld_v = np.asarray(bld_v, dtype=np.float64).reshape(-1, 3)
    to_svy = None

    if crs == 4326:
        # Reproject the VERTICES and rasterise in degree space, rather than warping a
        # finished metric raster: a second resample would either smear building edges
        # (bilinear) or alias them (nearest), and a DSM is a step function.
        from pyproj import Transformer
        fwd = Transformer.from_crs("EPSG:3414", "EPSG:4326", always_xy=True)
        to_svy = Transformer.from_crs("EPSG:4326", "EPSG:3414", always_xy=True)
        if len(terr_v):
            terr_v = terr_v.copy()
            terr_v[:, 0], terr_v[:, 1] = fwd.transform(terr_v[:, 0], terr_v[:, 1])
        if len(bld_v):
            bld_v = bld_v.copy()
            bld_v[:, 0], bld_v[:, 1] = fwd.transform(bld_v[:, 0], bld_v[:, 1])
        ring = np.asarray(domain_polygon.exterior.coords, dtype=np.float64)[:, :2]
        rx, ry = fwd.transform(ring[:, 0], ring[:, 1])
        bounds = (float(np.min(rx)), float(np.min(ry)), float(np.max(rx)), float(np.max(ry)))
        # degrees per metre, measured rather than assumed, at the domain centre
        cx, cy = domain_polygon.centroid.x, domain_polygon.centroid.y
        lon0, lat0 = fwd.transform(cx, cy)
        lon1, _ = fwd.transform(cx + 1000.0, cy)
        _, lat1 = fwd.transform(cx, cy + 1000.0)
        # anisotropic on purpose, so BOTH axes are exactly px_m on the ground (a 0.03%
        # difference at Singapore's latitude, but free to get right)
        px = (abs(lon1 - lon0) / 1000.0 * px_m, abs(lat1 - lat0) / 1000.0 * px_m)
        affine, shape = grid_for(bounds, px[0], snap=False)
        affine = Affine(px[0], 0.0, affine.c, 0.0, -px[1], affine.f)
        shape = (max(1, int(np.ceil((bounds[3] - bounds[1]) / px[1]))), shape[1])
    else:
        affine, shape = grid_for(domain_polygon.bounds, px_m)

    nrows, ncols = shape
    sub_shape = (nrows * k, ncols * k)
    sub_affine = Affine(affine.a / k, 0.0, affine.c, 0.0, affine.e / k, affine.f)

    terr_top, _ = rasterize_z(terr_v, terr_f, sub_affine, sub_shape)
    bld_top, bld_bot = rasterize_z(bld_v, bld_f, sub_affine, sub_shape, want_min=True)

    # fill everything the terrain mesh does not cover from the island DTM
    miss = ~np.isfinite(terr_top)
    n_missing = int(miss.sum())
    if n_missing:
        mr, mc = np.nonzero(miss)
        wx = sub_affine.c + (mc + 0.5) * sub_affine.a
        wy = sub_affine.f + (mr + 0.5) * sub_affine.e
        if to_svy is not None:
            wx, wy = to_svy.transform(wx, wy)
        terr_top[mr, mc] = dtm(np.asarray(wx), np.asarray(wy))

    bld_cov = np.isfinite(bld_top)
    overh = bld_cov & ((bld_bot - terr_top) > OVERHANG_M)
    n_over = int(overh.sum())

    if source == "terrain":
        surface = terr_top
    else:
        surface = terr_top.copy()
        use = bld_cov & (~overh) if overhang == "drop" else bld_cov
        surface[use] = np.maximum(bld_top[use], terr_top[use])

    out = _reduce(surface, k, agg)
    ground = _reduce(terr_top, k, agg)

    # The number that settles whether `agg` matters here: implied building volume under the
    # chosen reduction vs under max. If they agree, the default is irrelevant for this
    # domain; if max is far higher, that is the half-pixel dilation, measured.
    cell = px_m * px_m
    vol = float(np.clip(out - ground, 0, None).sum() * cell)
    vol_max = float(np.clip(_reduce(surface, k, "max") - ground, 0, None).sum() * cell)
    over_frac = n_over / max(1, bld_cov.sum())

    log(f"[dem] {ncols}x{nrows} @ {px_m:g} m, EPSG:{crs}, agg={agg}, {k}x{k} subsamples")
    # Only meaningful when there ARE buildings: with none (dem_source=terrain, or the
    # standalone CLI, which has no terrain/building split) this compares terrain to itself
    # and both numbers are 0, which reads as a bug rather than as "not applicable".
    if int(bld_cov.sum()) and source == "surface":
        log(f"[dem] implied building volume {vol:,.0f} m^3 (agg={agg}) vs {vol_max:,.0f} "
            f"m^3 (agg=max, {(vol_max / vol - 1) * 100 if vol > 0 else 0:+.1f}%)")
        log(f"[dem] overhang columns {n_over:,} of {int(bld_cov.sum()):,} building "
            f"subpixels ({over_frac * 100:.2f}%) -- "
            f"{'dropped to terrain' if overhang == 'drop' else 'kept'}")
    else:
        log(f"[dem] no separate building geometry, so no volume or overhang check "
            f"(source={source})")
    if n_missing:
        log(f"[dem] {n_missing / terr_top.size * 100:.1f}% of the raster is outside the "
            f"built domain and was filled from the island DTM (bare terrain, no buildings)")

    meta = {
        "crs": f"EPSG:{crs}", "pixel_size_m": float(px_m),
        "pixel_size_crs_units": [float(affine.a), float(-affine.e)],
        "width": ncols, "height": nrows,
        "tiepoint": {"pixel": [0, 0], "world": [float(affine.c), float(affine.f)]},
        "agg": agg, "overhang": overhang, "supersample": k, "source": source,
        "nodata": NODATA,
        "z_min": float(np.nanmin(out)), "z_max": float(np.nanmax(out)),
        "implied_building_volume_m3": vol,
        "implied_building_volume_m3_agg_max": vol_max,
        "overhang_subpixel_fraction": float(over_frac),
        "dtm_filled_fraction": float(n_missing / terr_top.size),
        "note": ("Each pixel is a height (a DSM: terrain with buildings baked in). North-up, "
                 "so the georeferencing is ModelPixelScale + ModelTiepoint, not a rotated "
                 "ModelTransformation. Sampled from the un-fused captured mesh, so it carries "
                 "no voxel-remesh staircase or decimation error."),
    }
    return out.astype(np.float32), affine, meta


def write_dem(grid, affine, meta, tif_path, log=print):
    """Write the GeoTIFF plus its `.dem.json` sidecar, and return both paths."""
    import json
    from pathlib import Path
    from sbg.topo.dtm import write_geotiff

    tif_path = Path(tif_path)
    write_geotiff(grid, affine, path=tif_path, crs=meta["crs"], nodata=NODATA)

    doc = {"version": 1, "kind": "sbg.dem", "units": "m", "dem_tif": tif_path.name, **meta}
    if meta["crs"] == "EPSG:3414":                 # tie point in lon/lat too, for convenience
        try:
            from pyproj import Transformer
            t = Transformer.from_crs("EPSG:3414", "EPSG:4326", always_xy=True)
            lon, lat = t.transform(*meta["tiepoint"]["world"])
            doc["tiepoint"]["lonlat"] = [float(lon), float(lat)]
        except Exception:
            pass
    side = tif_path.with_suffix(".json")
    side.write_text(json.dumps(doc, indent=2))
    log(f"[dem] {tif_path.name} ({tif_path.stat().st_size / 1e6:.1f} MB) + {side.name}")
    return tif_path, side


# --------------------------------------------------------------------------------------
# standalone CLI: turn an STL already on disk into a DEM, no rebuild
# --------------------------------------------------------------------------------------

def main():
    import argparse
    from pathlib import Path
    from shapely.geometry import box
    from sbg.onemap_native.terrain import build_domain_dtm, DtmSampler

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("mesh", help="STL/OBJ/PLY to sample (prefer the .raw.stl -- full detail)")
    ap.add_argument("-o", "--output", required=True, help="output .tif")
    ap.add_argument("--px", type=float, default=4.0, help="pixel size in metres (default 4)")
    ap.add_argument("--crs", type=int, default=3414, choices=CRS_CHOICES)
    ap.add_argument("--agg", default="median", choices=AGGS)
    ap.add_argument("--supersample", type=int, default=4)
    args = ap.parse_args()

    import trimesh
    m = trimesh.load(args.mesh, force="mesh", process=False)
    v, f = np.asarray(m.vertices, dtype=np.float64), np.asarray(m.faces)
    xmin, ymin = v[:, 0].min(), v[:, 1].min()
    xmax, ymax = v[:, 0].max(), v[:, 1].max()
    poly = box(xmin, ymin, xmax, ymax)
    grid_z, aff = build_domain_dtm((xmin, ymin, xmax, ymax))

    # A bare mesh file carries no terrain/building split, so everything is one set: no
    # overhang detection is possible here (that needs the building faces alone). Rebuild
    # with `--dem` instead if the overhang rule matters.
    grid, affine, meta = build_dem(v, f, np.empty((0, 3)), np.empty((0, 3), dtype=np.int64),
                                   poly, DtmSampler(grid_z, aff), px_m=args.px, crs=args.crs,
                                   agg=args.agg, supersample=args.supersample)
    meta["source_mesh"] = str(Path(args.mesh).name)
    meta["overhang"] = "n/a (single mesh, no terrain/building split)"
    write_dem(grid, affine, meta, args.output)


if __name__ == "__main__":
    main()
