"""Phase 3 redesign: constrained-triangulation terrain/building conforming
mesh. Building footprints are inserted as PSLG constraints (holes) into the
terrain triangulation itself, so the terrain mesh has vertices exactly on
each footprint boundary — elevation-matched via the same ElevationLookup
call used for each building's own draped base. The shared VertexPool then
dedupes identical (x, y, z) into the same index, closing the seam by
construction rather than approximating it with a per-building z-nudge.

Local-CFD-domain scale only (hundreds-to-low-thousands of buildings in a
bbox) — NOT for whole-island (118,782 buildings); see overlay_cityjson.py's
--mode drape path (coarse grid + centroid-offset drape) for that.

Usage:
  .venv/bin/python -m sbg.topo.conforming_mesh --dtm data/dtm_local.tif \\
      --sbg data/cutout.city.json --bbox 29000,29500,31500,32000 \\
      -o data/conforming.city.json
"""
import argparse
import json
import sys
import time

import numpy as np
import triangle
from shapely import contains, points as shp_points
from shapely.ops import unary_union

from sbg.config import DEFAULT_HEIGHT, SBG_OUTPUT
from sbg.cutout import load_domain_polygon
from sbg.extrude import geometry_from_rings_draped
from sbg.io_cityjson import VertexPool, building_footprint_polygon, finalize_and_save, new_cityjson
from sbg.topo.dtm import ElevationLookup

DEFAULT_TERRAIN_STEP = 10.0  # coarser than the whole-island path's local-fine grids —
# constraints already inject detail exactly where it matters (around buildings),
# so a fine background grid over flat interiors just bloats triangle count.
DEFAULT_MIN_FOOTPRINT_AREA = 1.0  # m^2 — drop slivers/degenerate parts


def extract_building_footprints(cm, min_area=DEFAULT_MIN_FOOTPRINT_AREA):
    """Returns [(obj_id, polygon), ...] for every Building with a usable,
    valid footprint above min_area; logs and skips the rest.
    """
    out = []
    skipped = 0
    for obj_id, obj in cm["CityObjects"].items():
        poly = building_footprint_polygon(cm, obj)
        if poly is None or poly.area < min_area:
            skipped += 1
            continue
        out.append((obj_id, poly))
    if skipped:
        print(f"  skipped {skipped} buildings (no usable/valid/too-small footprint)", file=sys.stderr)
    return out


def _polygon_rings(poly):
    """shapely Polygon -> [exterior, hole1, ...] as lists of (x, y) tuples."""
    rings = [list(poly.exterior.coords)]
    rings += [list(interior.coords) for interior in poly.interiors]
    return rings


def _polygon_parts_rings(poly):
    """shapely Polygon/MultiPolygon -> [[exterior, hole...], ...], one entry per
    part, matching geometry_from_rings_draped's expected `polygons_rings_xy`.
    """
    if poly.geom_type == "MultiPolygon":
        return [_polygon_rings(p) for p in poly.geoms]
    return [_polygon_rings(poly)]


def _generate_grid_points(domain_polygon, step, exclude_union, collar):
    """Regular grid over domain_polygon's bbox, kept only if actually inside
    domain_polygon (matters for non-rectangular domains) and excluding points
    within `collar` of the footprint union (inside buildings and a thin
    buffer around their edges — the buffer avoids Steiner-point-adjacent
    slivers).
    """
    xmin, ymin, xmax, ymax = domain_polygon.bounds
    xs = np.arange(xmin, xmax + step, step)
    ys = np.arange(ymin, ymax + step, step)
    gx, gy = np.meshgrid(xs, ys)
    pts = np.column_stack([gx.ravel(), gy.ravel()])
    geoms = shp_points(pts[:, 0], pts[:, 1])

    in_domain = contains(domain_polygon, geoms)
    exclusion_zone = exclude_union.buffer(collar)
    excluded = contains(exclusion_zone, geoms)
    return pts[in_domain & ~excluded]


def build_terrain_pslg(footprint_union, grid_points, domain_polygon):
    """Assembles the {vertices, segments, holes} dict for triangle.triangulate.
    footprint_union is used only for terrain constraint edges/holes — each
    building is still extruded from its OWN (non-unioned) polygon later.

    domain_polygon's own boundary MUST be added as explicit segments too —
    confirmed empirically (not an assumption): without an explicit outer
    boundary loop, `triangle`'s 'p' mode does not reliably use the grid
    points' convex hull as an implicit outer boundary when inner hole
    segments are also present; it silently triangulates almost nothing (2
    triangles for a lone 4-vertex hole plus 3000+ untouched grid points, in
    one measured case) rather than raising an error. Adding the domain's own
    ring as a segment loop fixed this in every test case.
    """
    vertices = []
    vertex_index = {}

    def add_vertex(x, y):
        key = (round(x, 3), round(y, 3))
        idx = vertex_index.get(key)
        if idx is None:
            idx = len(vertices)
            vertex_index[key] = idx
            vertices.append(key)
        return idx

    segments = []

    def add_ring_segments(coords):
        pts = coords[:-1] if coords[0] == coords[-1] else coords
        idxs = [add_vertex(x, y) for x, y in pts]
        n = len(idxs)
        for i in range(n):
            segments.append((idxs[i], idxs[(i + 1) % n]))

    add_ring_segments(list(domain_polygon.exterior.coords))
    for interior in domain_polygon.interiors:
        add_ring_segments(list(interior.coords))

    parts = footprint_union.geoms if footprint_union.geom_type == "MultiPolygon" else [footprint_union]
    holes = []
    for part in parts:
        add_ring_segments(list(part.exterior.coords))
        for interior in part.interiors:
            add_ring_segments(list(interior.coords))
        rp = part.representative_point()
        holes.append((rp.x, rp.y))

    for x, y in grid_points:
        add_vertex(float(x), float(y))

    d = {"vertices": np.array(vertices, dtype=float)}
    if segments:
        d["segments"] = np.array(segments, dtype=int)
    if holes:
        d["holes"] = np.array(holes, dtype=float)
    return d


def emit_terrain_faces(pool, tri_out, elevation_fn):
    tri_verts = tri_out["vertices"]
    tri_tris = tri_out["triangles"]
    idx_cache = {}

    def pool_idx(vi):
        idx = idx_cache.get(vi)
        if idx is None:
            x, y = tri_verts[vi]
            z = elevation_fn(float(x), float(y))
            idx = pool.add(float(x), float(y), z)
            idx_cache[vi] = idx
        return idx

    faces = []
    for tri in tri_tris:
        a, b, c = (pool_idx(int(vi)) for vi in tri)
        faces.append([[a, b, c]])
    return faces


def conforming_overlay(
    sbg_cm, elevation_lookup, domain_polygon,
    terrain_step=DEFAULT_TERRAIN_STEP, min_footprint_area=DEFAULT_MIN_FOOTPRINT_AREA, collar=None,
    spatial_index=None, log_fn=None,
):
    """spatial_index: optional sbg.ui.spatial_index.SpatialIndex, already
    built over sbg_cm. When given, uses its query_contained() +
    polygon_by_id to get in-domain footprints directly instead of
    extract_building_footprints() scanning and reconstructing every
    Building's polygon in the whole file -- measured at ~15.6s of every STL
    pipeline job regardless of domain size (a 4.4km^2/1,743-building run:
    15.6s of an 18.8s total stage) before this was wired in, versus ~2.2s
    for everything else this function does combined. Falls back to the full
    scan when not given (this function's own CLI entrypoint, main() below,
    has no spatial index available -- same result, just slower).

    log_fn: optional callable(str) for progress lines, e.g. a Job's
    log_line -- defaults to print(..., file=sys.stderr) for CLI use. Added
    because this function's own print() calls, running in-process rather
    than through a subprocess, never reached sbg/ui/jobs.py's Job.log
    (they only ever showed up in the server's own stderr) -- made every STL
    pipeline job's "conforming_overlay" stage an opaque single number with
    no visibility into which of its several real sub-steps actually cost
    the time.
    """
    if log_fn is None:
        log_fn = lambda msg: print(msg, file=sys.stderr)  # noqa: E731

    # This function used to be one opaque ~57-73s block in job logs (see
    # sbg/ui/pipeline.py's single "conforming_overlay" stage) -- real
    # per-step timing here so slowness can be attributed to an actual step
    # instead of eyeballed/guessed from when successive print() lines
    # happened to appear.
    t0 = time.perf_counter()

    def _lap(label):
        nonlocal t0
        now = time.perf_counter()
        log_fn(f"  [{label}] {now - t0:.1f}s")
        t0 = now

    if collar is None:
        collar = max(terrain_step / 2, 1.0)

    if spatial_index is not None:
        contained_ids = spatial_index.query_contained(domain_polygon)
        footprints = [(obj_id, spatial_index.polygon_by_id[obj_id]) for obj_id in contained_ids]
    else:
        footprints = extract_building_footprints(sbg_cm, min_area=min_footprint_area)
        # extract_building_footprints() itself has no notion of a domain --
        # it returns every Building in sbg_cm. Real bug found via
        # sbg/ui/pipeline.py (Phase 6): calling this against the FULL
        # 118,782-building dataset with a small domain_polygon still
        # extruded and returned every building in the whole file,
        # ~118,780 in one real test, because domain_polygon was only ever
        # used to bound the terrain grid/PSLG below, never to filter which
        # buildings get included. Never caught during this function's own
        # Phase 3 testing because every prior test already pre-cut its
        # --sbg input to roughly the domain, sidestepping the missing
        # filter by construction. Same "keep only if fully inside, drop if
        # crossing" semantics as sbg.cutout.cutout() -- a domain-boundary
        # operation should behave consistently regardless of which
        # pipeline stage does it.
        footprints = [(obj_id, poly) for obj_id, poly in footprints if domain_polygon.contains(poly)]
    log_fn(f"  {len(footprints)} usable building footprints within domain")
    _lap("extract+filter footprints")

    valid_polys = []
    valid_ids = set()
    for obj_id, poly in footprints:
        p = poly if poly.is_valid else poly.buffer(0)
        if p.is_valid and p.area > 0:
            valid_polys.append(p)
            valid_ids.add(obj_id)

    footprint_union = unary_union(valid_polys)
    if footprint_union.is_empty:
        raise ValueError("No valid building footprints in domain")
    _lap("validity-fix + unary_union")

    grid_points = _generate_grid_points(domain_polygon, terrain_step, footprint_union, collar)
    log_fn(f"  {len(grid_points)} terrain grid points (after domain+collar filtering)")
    _lap("generate terrain grid points")

    pslg = build_terrain_pslg(footprint_union, grid_points, domain_polygon)
    log_fn(
        f"  PSLG: {len(pslg['vertices'])} vertices, "
        f"{len(pslg.get('segments', []))} segments, {len(pslg.get('holes', []))} holes"
    )
    _lap("build_terrain_pslg (includes per-vertex VertexPool.add dict lookups)")

    # triangle's internal precision handling misbehaves at large absolute EPSG:3414
    # coordinates (~30000m) -- confirmed empirically: identical PSLG produces 0
    # triangles at real-world coords, 72 triangles after recentering to origin.
    # Shift to local origin for triangulation, then shift back to real-world coords.
    origin = pslg["vertices"].min(axis=0)
    pslg_shifted = dict(pslg)
    pslg_shifted["vertices"] = pslg["vertices"] - origin
    if "holes" in pslg_shifted:
        pslg_shifted["holes"] = pslg["holes"] - origin

    tri_out = triangle.triangulate(pslg_shifted, "pY")
    if "triangles" not in tri_out:
        raise RuntimeError(
            f"triangle.triangulate failed to produce a triangulation "
            f"(got keys {list(tri_out.keys())}) -- check for self-intersecting "
            f"or degenerate footprint geometry in this domain"
        )
    tri_out["vertices"] = tri_out["vertices"] + origin
    log_fn(f"  triangulated: {len(tri_out['triangles'])} triangles")
    _lap("triangle.triangulate (C library)")

    pool = VertexPool()
    cm = new_cityjson()

    terrain_faces = emit_terrain_faces(pool, tri_out, elevation_lookup)
    cm["CityObjects"]["terrain"] = {
        "type": "TINRelief",
        "attributes": {},
        "geometry": [{"type": "CompositeSurface", "lod": "1", "boundaries": terrain_faces}],
    }
    log_fn(f"  {len(terrain_faces)} terrain triangles, {len(pool.vertices)} vertices so far")
    _lap("emit_terrain_faces (one ElevationLookup call per unique triangulation vertex)")

    fallback_count = 0
    added = 0
    for obj_id, poly in footprints:
        if obj_id not in valid_ids:
            fallback_count += 1
            continue
        obj = sbg_cm["CityObjects"][obj_id]
        height = obj["attributes"].get("height") or DEFAULT_HEIGHT
        rings_parts = _polygon_parts_rings(poly)
        geometry = geometry_from_rings_draped(pool, rings_parts, elevation_lookup, height)
        if geometry is None:
            fallback_count += 1
            continue
        cm["CityObjects"][obj_id] = {
            "type": obj["type"],
            "attributes": obj["attributes"],
            "geometry": [geometry],
        }
        added += 1

    log_fn(f"  {added} buildings added (conforming), {fallback_count} skipped/fallback")
    _lap(f"extrude {added} buildings (per-vertex ElevationLookup + VertexPool.add)")
    return cm, pool


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dtm", required=True, help="Input DTM GeoTIFF")
    ap.add_argument("--sbg", default=str(SBG_OUTPUT), help="Input SBG CityJSON (buildings to include)")
    ap.add_argument("--bbox", help="xmin,ymin,xmax,ymax in EPSG:3414 meters")
    ap.add_argument("--domain-geojson", help="Path to a GeoJSON Polygon domain boundary")
    ap.add_argument("--domain-crs", choices=["3414", "4326"], default="4326")
    ap.add_argument("--terrain-step", type=float, default=DEFAULT_TERRAIN_STEP)
    ap.add_argument("--min-footprint-area", type=float, default=DEFAULT_MIN_FOOTPRINT_AREA)
    ap.add_argument("-o", "--output", required=True, help="Output conforming-mesh CityJSON path")
    args = ap.parse_args()

    domain_polygon = load_domain_polygon(args.bbox, args.domain_geojson, args.domain_crs)

    print(f"Loading DTM {args.dtm}...", file=sys.stderr)
    elevation_lookup = ElevationLookup(args.dtm)

    print(f"Loading SBG {args.sbg}...", file=sys.stderr)
    sbg_cm = json.load(open(args.sbg))

    cm, pool = conforming_overlay(sbg_cm, elevation_lookup, domain_polygon, terrain_step=args.terrain_step, min_footprint_area=args.min_footprint_area)

    finalize_and_save(cm, pool, args.output)
    print(f"Wrote {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
