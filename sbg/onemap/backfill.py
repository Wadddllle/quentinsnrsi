"""Backfill SBG building heights from OneMap's real measured heights.

Nearest-centroid join between SBG building footprint centroids and OneMap
batch-table records (crawl_tiles.py output). On match, overwrites height with
OneMap's ground truth and tags height_source="onemap"; unmatched buildings
keep whatever they had (osm/levels/estimated_default from build_sbg.py).

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
    remap_vertex_indices,
    walk_vertex_indices,
)
from sbg.onemap.crawl_tiles import OUTPUT_PATH as ONEMAP_RECORDS_PATH

MATCH_TOLERANCE_M = 40.0
HEIGHT_CHANGE_TOLERANCE_M = 0.01


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

    matched = 0
    unmatched = 0
    geometry_regenerated = 0
    height_source_counts = {}
    for obj_id, obj in cm["CityObjects"].items():
        centroid = building_footprint_centroid(cm, obj)
        if centroid is None:
            unmatched += 1
            continue
        dist, idx = tree.query(centroid, k=1)
        if dist <= MATCH_TOLERANCE_M:
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
    print(f"Unmatched (kept prior height_source): {unmatched}", file=sys.stderr)
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
