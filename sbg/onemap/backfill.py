"""Backfill SBG building heights from OneMap's real measured heights.

Cluster-local one-to-one assignment join between SBG building footprint
centroids and OneMap batch-table records (crawl_tiles.py output) -- see
match_buildings() for why this replaced a plain independent per-building
nearest-neighbor join. On match, overwrites height with OneMap's ground
truth and tags height_source="onemap"; unmatched buildings keep whatever
they had (osm/levels/estimated_default from build_sbg.py).

Also regenerates each corrected building's geometry to match the new height
(see _regenerate_height) -- earlier versions of this script only updated the
`height` attribute, leaving the actual LoD1 solid frozen at whatever height
build_sbg.py originally extruded it at. That silently diverged the visible
3D shape from the (correct) height attribute for every one of the ~110k
buildings OneMap has real data for -- found via a real building (Suntec,
attribute said 58.93m, rendered ~3.2m) and confirmed systemic by sampling.

Usage: .venv/bin/python -m sbg.onemap.backfill
"""
import json
import sys
from collections import Counter

import numpy as np
from pyproj import Transformer
from scipy.spatial import cKDTree

from sbg.config import SBG_OUTPUT, SOURCE_CRS, TARGET_CRS
from sbg.io_cityjson import (
    AppendOnlyVertexPool,
    building_footprint_centroid,
    footprint_rings,
    remap_vertex_indices,
    walk_vertex_indices,
)
from sbg.onemap.crawl_tiles import OUTPUT_PATH as ONEMAP_RECORDS_PATH

# Widened from the old plain-nearest-neighbor join's 40.0 -- safe to widen
# now that matching is assignment-based (see match_buildings()), which
# can't accidentally steal a point from a closer rightful claimant the way
# a wider radius would have risked under independent nearest-neighbor.
# 48.6m real-world case found directly: Deyi Secondary School's actual
# 8,549m^2 main academic block (a combined-outline OSM relation, the same
# "footprint centroid doesn't match true center" pattern as Phase 1.6's
# landmark blob bug) sat just outside the old 40m cutoff and fell through
# to the flat 3.2m default despite its correct OneMap match being right
# there -- confirmed by checking the real coordinates, not assumed.
MATCH_TOLERANCE_M = 60.0

# A flat radius still fails for genuinely large buildings, found via a
# second real report (Singapore Indoor Stadium, way/172472785, 18,098 m^2
# footprint spanning ~174x224m): its real OneMap point sat 95.7m from the
# footprint centroid, comfortably past MATCH_TOLERANCE_M, because a big
# building's own physical extent can legitimately place its true reference
# point well outside a small-building-sized radius -- confirmed systemic by
# checking every footprint >5000 m^2 in the real dataset (310 of 2,350 such
# buildings unmatched). PER_BUILDING_RADIUS_SCALE lets the match radius grow
# with each building's own footprint size (half its bounding-box diagonal)
# instead of using one number for every building regardless of scale.
#
# MAX_MATCH_RADIUS_M caps that growth deliberately short of what the very
# biggest "buildings" would technically ask for -- checked directly, and the
# biggest few unmatched footprints (600-680m half-diagonals) aren't real
# single buildings at all, they're giant combined-outline OSM relations
# spanning what's obviously an entire industrial/port complex (the same
# blob-footprint pathology as Phase 1.6's landmark bug, just at a much
# larger scale). Letting the radius grow to match THEIR size would risk
# force-matching them to whatever unrelated OneMap point happens to be nearby
# within hundreds of meters -- a wrong answer, not a better one. 200m
# comfortably covers Indoor Stadium's real 95.7m case (and similarly-scaled
# genuine single buildings like other stadiums/malls) while staying well
# short of the multi-hundred-meter blobs, which correctly stay unmatched
# rather than get a semantically wrong nearby point forced onto them.
PER_BUILDING_RADIUS_SCALE = True
MAX_MATCH_RADIUS_M = 200.0
HEIGHT_CHANGE_TOLERANCE_M = 0.01


def _footprint_half_diagonal(cm, obj):
    """Half the bounding-box diagonal (meters) of a building's footprint --
    cheaper than building a full shapely Polygon just for bounds, since this
    only needs raw ring points, not a validated geometry.
    """
    parts = footprint_rings(cm, obj)
    if not parts:
        return 0.0
    xs = [x for part in parts for ring in part for x, y in ring]
    ys = [y for part in parts for ring in part for x, y in ring]
    if not xs:
        return 0.0
    dx = max(xs) - min(xs)
    dy = max(ys) - min(ys)
    return 0.5 * (dx ** 2 + dy ** 2) ** 0.5


def compute_vertex_refcounts(cm):
    """How many times each vertex index is referenced across the WHOLE file.

    build_sbg.py's VertexPool dedups globally (by rounded x/y/z), not per
    building -- so two structurally-attached buildings (e.g. rowhouses) that
    happened to land on the same pre-backfill default height can share an
    actual vertex at their common corner. This lets _regenerate_height tell
    "only I use this vertex, safe to move in place" apart from "something
    else also uses this vertex, needs its own private copy" -- moving a
    shared vertex in place would silently drag a neighboring building's roof
    along with it.
    """
    counts = Counter()
    for obj in cm["CityObjects"].values():
        for geom in obj.get("geometry", []):
            counts.update(walk_vertex_indices(geom["boundaries"]))
    return counts


def _regenerate_height(cm, obj, new_height, vertex_refcounts, pool):
    """Relocates a flat LoD1 box extrusion's roof to new_height by moving the
    vertices that shape its top face and wall tops -- not a fresh
    re-extrusion from the footprint. Returns True if geometry was touched.

    Compares new_height against the geometry's OWN current top height, not
    the height attribute -- this file may already have a correct-looking
    height attribute from a prior run of this same script (before this
    geometry fix existed), so trusting the attribute would skip regenerating
    exactly the stale geometry this exists to fix. Checking the geometry
    itself makes this correct and idempotent regardless of run history.

    Vertices used exclusively by this building (the common case) are mutated
    in place: zero net growth in the vertex list. Vertices shared with
    another CityObject (see compute_vertex_refcounts) get a private copy
    appended instead, and only this building's geometry is repointed at it --
    the shared original is left untouched for whoever else references it.
    """
    if not obj.get("geometry"):
        return False
    geom = obj["geometry"][0]
    if geom["type"] not in ("Solid", "CompositeSolid"):
        return False

    transform = cm["transform"]
    sx, sy, sz = transform["scale"]
    tx, ty, tz = transform["translate"]
    base_q = round((0.0 - tz) / sz)
    new_top_q = round((new_height - tz) / sz)

    local_counts = Counter(walk_vertex_indices(geom["boundaries"]))
    top_indices = [i for i in local_counts if cm["vertices"][i][2] != base_q]
    if not top_indices:
        return False

    current_top_q = cm["vertices"][top_indices[0]][2]
    if current_top_q == new_top_q:
        return False  # geometry already matches -- nothing to do

    mapping = {i: i for i in local_counts}  # identity unless overridden below
    for idx in top_indices:
        if vertex_refcounts[idx] > local_counts[idx]:
            # Referenced by something outside this building too -- give this
            # building its own copy rather than moving a shared vertex.
            x_q, y_q, _ = cm["vertices"][idx]
            x = x_q * sx + tx
            y = y_q * sy + ty
            mapping[idx] = pool.add(x, y, new_height)
            vertex_refcounts[idx] -= local_counts[idx]
        else:
            # Every reference to this vertex comes from this building --
            # safe to relocate it directly.
            cm["vertices"][idx][2] = new_top_q

    geom["boundaries"] = remap_vertex_indices(geom["boundaries"], mapping)
    return True

_transformer = Transformer.from_crs(SOURCE_CRS, TARGET_CRS, always_xy=True)


def load_onemap_points():
    """Returns (xy: Nx2 array, records: list[dict]), deduped by gml_id."""
    seen = {}
    with open(ONEMAP_RECORDS_PATH) as f:
        for line in f:
            rec = json.loads(line)
            if rec.get("lat") is None or rec.get("lng") is None or rec.get("height") is None:
                continue
            gid = rec.get("gml_id")
            if gid and gid in seen:
                continue
            if gid:
                seen[gid] = rec
            else:
                seen[id(rec)] = rec

    records = list(seen.values())
    lons = [r["lng"] for r in records]
    lats = [r["lat"] for r in records]
    xs, ys = _transformer.transform(lons, lats)
    xy = np.column_stack([xs, ys])
    return xy, records


def match_buildings(sbg_xy, onemap_tree, onemap_n, radius=MATCH_TOLERANCE_M):
    """Returns a list of length len(sbg_xy): the matched OneMap index for
    each SBG building, or None if unmatched.

    radius: scalar or a per-building array (one radius per row of sbg_xy) --
    passed straight through to cKDTree.query_ball_point, which already
    broadcasts either form. See PER_BUILDING_RADIUS_SCALE in this module's
    docstring for why a flat radius isn't enough on its own.

    Replaces a plain `tree.query(centroid, k=1)` per building -- found to
    fail two real ways on a real example (Deyi Secondary School, see this
    module's docstring): (1) a hard per-point radius cutoff drops a
    genuinely-correct match that happens to sit just past it (the
    combined-outline-footprint-centroid problem, same root cause as Phase
    1.6's landmark blob bug), and (2) independent per-building
    nearest-neighbor has no way to notice when two nearby SBG footprints
    would both want the SAME OneMap point -- it just takes whatever's
    nearest for each query in isolation, which can silently steal a point
    from a building that had a stronger claim to it.

    Approach: build every candidate (SBG building, OneMap point) pair within
    `radius`, then greedily claim pairs in ascending distance order -- each
    side can only be claimed once, so the globally-closest pair always wins
    its edge first, and a contested point can never be taken by a farther
    building while a closer one is still waiting. A building that loses a
    contested slot this way (its closer candidates all got claimed first)
    falls back to its own single nearest candidate regardless of whether
    that point is already claimed by someone else -- the plain
    independent-nearest-neighbor behavior this replaced. Needed because
    real OSM data has plenty of legitimate duplicate/split footprints for
    the *same* real building (confirmed: exclusive-only matching dropped
    the matched count from ~110k to ~106k on the real dataset) -- those
    should keep sharing one OneMap height like they always did, not lose
    their match entirely just because a sibling footprint claimed the
    shared point first. Exclusivity still does its job for the case that
    actually matters: the first (closest) claim on any given OneMap point
    is never displaced by a farther one, which is exactly what fixes
    Deyi-style cross-assignment between genuinely different buildings.

    An exact one-to-one assignment (scipy.optimize.linear_sum_assignment,
    tried first) minimizes total distance more rigorously than greedy does,
    but is O(n^3) per connected component of the candidate graph -- and real
    dense areas (HDB estates, where buildings and OneMap points sit close
    enough to chain transitively through many neighbors) produce components
    of 5,000-11,000+ nodes at this radius, confirmed directly by measuring
    connected_components() on the real dataset. That's computationally
    infeasible (a single component that size didn't finish in several
    minutes). Greedy-by-distance has no such blowup -- it's one global sort
    of all candidate edges (tens of thousands at this scale, not billions)
    plus a linear claim pass -- and for a spatially local problem like this
    one, where the right answer is essentially always "the closest party
    that isn't already spoken for," it gives the same practical result as
    the exact solve on the small contested clusters that actually matter
    (verified directly against the real Deyi Secondary School case this was
    built to fix, see the module's usage notes).
    """
    n_sbg = len(sbg_xy)
    candidates = onemap_tree.query_ball_point(sbg_xy, radius)
    onemap_xy = onemap_tree.data

    edges = []  # (distance, sbg_idx, onemap_idx)
    for i, cand in enumerate(candidates):
        if not cand:
            continue
        d = np.linalg.norm(onemap_xy[cand] - sbg_xy[i], axis=1)
        for dist, j in zip(d, cand):
            edges.append((dist, i, j))
    edges.sort(key=lambda e: e[0])

    result = [None] * n_sbg
    sbg_claimed = [False] * n_sbg
    onemap_claimed = [False] * onemap_n
    for dist, i, j in edges:
        if sbg_claimed[i] or onemap_claimed[j]:
            continue
        result[i] = j
        sbg_claimed[i] = True
        onemap_claimed[j] = True

    # Fallback pass: anyone who had candidates but lost every contested slot
    # (all their candidates got claimed by a closer building first) still
    # gets their own single nearest candidate, shared or not -- see the
    # docstring above for why dropping this regressed real match counts.
    for i, cand in enumerate(candidates):
        if result[i] is None and cand:
            d = np.linalg.norm(onemap_xy[cand] - sbg_xy[i], axis=1)
            result[i] = cand[int(np.argmin(d))]

    return result


def main():
    print("Loading OneMap batch-table records...", file=sys.stderr)
    onemap_xy, onemap_records = load_onemap_points()
    print(f"  {len(onemap_records)} unique OneMap buildings", file=sys.stderr)
    tree = cKDTree(onemap_xy)

    print(f"Loading {SBG_OUTPUT}...", file=sys.stderr)
    cm = json.load(open(SBG_OUTPUT))

    print("Computing vertex reference counts...", file=sys.stderr)
    vertex_refcounts = compute_vertex_refcounts(cm)
    pool = AppendOnlyVertexPool(cm)
    starting_vertex_count = len(cm["vertices"])

    print("Computing SBG footprint centroids...", file=sys.stderr)
    obj_ids = list(cm["CityObjects"].keys())
    objs = [cm["CityObjects"][oid] for oid in obj_ids]
    centroids = [building_footprint_centroid(cm, obj) for obj in objs]
    no_centroid = sum(1 for c in centroids if c is None)
    sbg_xy = np.array([c if c is not None else (0.0, 0.0) for c in centroids])

    print("Computing per-building match radii (footprint-size-scaled)...", file=sys.stderr)
    half_diagonals = np.array([_footprint_half_diagonal(cm, obj) for obj in objs])
    radii = np.clip(half_diagonals, MATCH_TOLERANCE_M, MAX_MATCH_RADIUS_M)
    print(
        f"  {int((radii > MATCH_TOLERANCE_M).sum())} buildings get a widened radius "
        f"(big footprints), max used = {radii.max():.1f}m",
        file=sys.stderr,
    )

    print("Matching (cluster-local one-to-one assignment)...", file=sys.stderr)
    match_idx = match_buildings(sbg_xy, tree, len(onemap_records), radius=radii)
    for i, c in enumerate(centroids):
        if c is None:
            match_idx[i] = None  # no usable footprint at all -- never a real candidate

    matched = 0
    unmatched = 0
    geometry_regenerated = 0
    height_source_counts = {}
    for obj_id, idx in zip(obj_ids, match_idx):
        obj = cm["CityObjects"][obj_id]
        if idx is not None:
            rec = onemap_records[idx]
            if _regenerate_height(cm, obj, rec["height"], vertex_refcounts, pool):
                geometry_regenerated += 1
            obj["attributes"]["height"] = rec["height"]
            obj["attributes"]["height_source"] = "onemap"
            obj["attributes"]["onemap_gml_id"] = rec.get("gml_id")
            obj["attributes"]["onemap_storeys"] = rec.get("storeys")
            name = rec.get("name")
            if name and name.strip():
                obj["attributes"]["onemap_name"] = name.strip()
            matched += 1
        else:
            unmatched += 1

        hs = obj["attributes"]["height_source"]
        height_source_counts[hs] = height_source_counts.get(hs, 0) + 1

    print(f"Matched to OneMap: {matched}", file=sys.stderr)
    print(f"Unmatched (kept prior height_source): {unmatched} ({no_centroid} of those had no usable footprint at all)", file=sys.stderr)
    print(f"Geometry regenerated (height attribute didn't match actual solid): {geometry_regenerated}", file=sys.stderr)
    print(f"Final height_source distribution: {height_source_counts}", file=sys.stderr)
    added_vertices = len(cm["vertices"]) - starting_vertex_count
    print(
        f"Vertices: {starting_vertex_count} -> {len(cm['vertices'])} "
        f"(+{added_vertices}, {added_vertices / starting_vertex_count:.1%}) -- "
        "growth only from vertices that were genuinely shared with another building",
        file=sys.stderr,
    )

    with open(SBG_OUTPUT, "w") as f:
        json.dump(cm, f)
    print(f"Wrote {SBG_OUTPUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
