"""Deliverable 1: build the SBG CityJSON dataset from sg_buildings_v5.geojson.

Usage: .venv/bin/python -m sbg.build_sbg
"""
import json
import sys
import time

from pyproj import Transformer

from sbg.config import DEFAULT_HEIGHT, LEVEL_HEIGHT, SBG_OUTPUT, SG_BUILDINGS_GEOJSON, SOURCE_CRS, TARGET_CRS
from sbg.extrude import geometry_from_rings
from sbg.io_cityjson import VertexPool, finalize_and_save, new_cityjson

_transformer = Transformer.from_crs(SOURCE_CRS, TARGET_CRS, always_xy=True)


def resolve_height(props):
    height = props.get("height")
    if height not in (None, 0):
        return float(height), "osm"

    levels = props.get("building_levels")
    try:
        levels_f = float(levels)
    except (TypeError, ValueError):
        levels_f = 0.0
    if levels_f > 0:
        return levels_f * LEVEL_HEIGHT, "levels"

    return DEFAULT_HEIGHT, "estimated_default"


def reproject_polygon_rings(rings):
    """rings: GeoJSON Polygon coordinates (list of rings of [lon, lat]).
    Returns the same structure reprojected to TARGET_CRS, as (x, y) tuples.
    """
    out = []
    for ring in rings:
        lons = [pt[0] for pt in ring]
        lats = [pt[1] for pt in ring]
        xs, ys = _transformer.transform(lons, lats)
        out.append(list(zip(xs, ys)))
    return out


def iter_features(path):
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line.startswith('{"type": "Feature"'):
                continue
            try:
                yield json.loads(line.rstrip(","))
            except json.JSONDecodeError:
                continue


def build_attributes(props, height, height_source):
    return {
        "id": props.get("id"),
        "height": height,
        "height_source": height_source,
        "building_archetype": props.get("building_archetype"),
        "building_levels": props.get("building_levels"),
        "addr_housenumber": props.get("addr_housenumber"),
        "addr_street": props.get("addr_street"),
        "addr_postcode": props.get("addr_postcode"),
        "gross_floor_area": props.get("gross_floor_area"),
        "built_year": props.get("built_year"),
    }


def main():
    cm = new_cityjson()
    pool = VertexPool()

    total = 0
    skipped_degenerate = 0
    skipped_no_id = 0
    duplicate_ids = 0
    height_source_counts = {"osm": 0, "levels": 0, "estimated_default": 0}
    seen_ids = set()

    t0 = time.time()
    for feat in iter_features(SG_BUILDINGS_GEOJSON):
        total += 1
        if total % 20000 == 0:
            print(f"  ...{total} features processed ({time.time() - t0:.0f}s)", file=sys.stderr)

        props = feat.get("properties", {})
        obj_id = props.get("id")
        if not obj_id:
            skipped_no_id += 1
            continue
        if obj_id in seen_ids:
            duplicate_ids += 1
            obj_id = f"{obj_id}#{duplicate_ids}"
        seen_ids.add(obj_id)

        geom = feat.get("geometry") or {}
        gtype = geom.get("type")
        coords = geom.get("coordinates")
        if gtype == "Polygon":
            polygons = [coords]
        elif gtype == "MultiPolygon":
            polygons = coords
        else:
            skipped_degenerate += 1
            continue

        height, height_source = resolve_height(props)

        polygons_xy = [reproject_polygon_rings(rings) for rings in polygons]
        geometry = geometry_from_rings(pool, polygons_xy, base_z=0.0, top_z=height)
        if geometry is None:
            skipped_degenerate += 1
            continue

        height_source_counts[height_source] += 1
        cm["CityObjects"][obj_id] = {
            "type": "Building",
            "attributes": build_attributes(props, height, height_source),
            "geometry": [geometry],
        }

    print(f"Parsed {total} features in {time.time() - t0:.0f}s", file=sys.stderr)
    print(f"  Buildings written: {len(cm['CityObjects'])}", file=sys.stderr)
    print(f"  Skipped (degenerate geometry): {skipped_degenerate}", file=sys.stderr)
    print(f"  Skipped (no id): {skipped_no_id}", file=sys.stderr)
    print(f"  Duplicate ids suffixed: {duplicate_ids}", file=sys.stderr)
    print(f"  height_source counts: {height_source_counts}", file=sys.stderr)
    print(f"  Unique vertices: {len(pool.vertices)}", file=sys.stderr)

    out_path = finalize_and_save(cm, pool, SBG_OUTPUT)
    print(f"Wrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
