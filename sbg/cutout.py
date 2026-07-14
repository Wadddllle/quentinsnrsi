"""Deliverable 3: cutout of SBG within a domain boundary.

Whole-building removal semantics (confirmed with user): keep only buildings
whose footprint lies fully inside the domain polygon; drop any that cross the
boundary or lie entirely outside. No geometric clipping of partial buildings.

Usage:
  .venv/bin/python -m sbg.cutout --bbox 28000,29000,31000,32000 -o data/cutout.city.json
  .venv/bin/python -m sbg.cutout --domain-geojson domain.geojson -o data/cutout.city.json
"""
import argparse
import json
import sys

from shapely.geometry import Polygon, box, shape
from shapely.prepared import prep

from sbg.config import SBG_OUTPUT, SOURCE_CRS, TARGET_CRS
from sbg.io_cityjson import building_footprint_polygon, subset_cityjson


def cutout(cm, domain_polygon):
    """domain_polygon: shapely Polygon/MultiPolygon in TARGET_CRS.
    Returns a new CityJSON dict with a compacted vertex list.
    """
    prepared = prep(domain_polygon)

    kept_ids = []
    dropped_no_geom = 0
    dropped_outside = 0
    for obj_id, obj in cm["CityObjects"].items():
        footprint = building_footprint_polygon(cm, obj)
        if footprint is None:
            dropped_no_geom += 1
            continue
        if prepared.contains(footprint):
            kept_ids.append(obj_id)
        else:
            dropped_outside += 1

    new_cm = subset_cityjson(cm, kept_ids)

    stats = {
        "kept": len(kept_ids),
        "dropped_outside_or_crossing": dropped_outside,
        "dropped_no_geometry": dropped_no_geom,
        "vertices": len(new_cm["vertices"]),
    }
    return new_cm, stats


def load_domain_polygon(bbox=None, domain_geojson=None, domain_crs="4326"):
    """Shared by cutout.py and topo/conforming_mesh.py's CLI: a domain boundary
    as either an EPSG:3414 bbox string or a GeoJSON Polygon (reprojected from
    WGS84 by default).
    """
    if bbox:
        xmin, ymin, xmax, ymax = (float(v) for v in bbox.split(","))
        return box(xmin, ymin, xmax, ymax)

    if domain_geojson:
        with open(domain_geojson) as f:
            gj = json.load(f)
        geom = gj["features"][0]["geometry"] if gj.get("type") == "FeatureCollection" else gj.get("geometry", gj)
        poly = shape(geom)
        if domain_crs == "4326":
            from pyproj import Transformer

            t = Transformer.from_crs(SOURCE_CRS, TARGET_CRS, always_xy=True)
            if poly.geom_type == "Polygon":
                ext = [t.transform(x, y) for x, y in poly.exterior.coords]
                ints = [[t.transform(x, y) for x, y in ring.coords] for ring in poly.interiors]
                poly = Polygon(ext, ints)
            else:
                raise ValueError("Only Polygon domain geometry is supported")
        return poly

    raise ValueError("Must specify --bbox or --domain-geojson")


def _load_domain_polygon(args):
    return load_domain_polygon(args.bbox, args.domain_geojson, args.domain_crs)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bbox", help="xmin,ymin,xmax,ymax in EPSG:3414 meters")
    ap.add_argument("--domain-geojson", help="Path to a GeoJSON Polygon domain boundary")
    ap.add_argument("--domain-crs", choices=["3414", "4326"], default="4326", help="CRS of --domain-geojson (default: 4326)")
    ap.add_argument("--input", default=str(SBG_OUTPUT), help="Input SBG CityJSON path")
    ap.add_argument("-o", "--output", required=True, help="Output cutout CityJSON path")
    args = ap.parse_args()

    domain_polygon = _load_domain_polygon(args)

    print(f"Loading {args.input}...", file=sys.stderr)
    cm = json.load(open(args.input))

    new_cm, stats = cutout(cm, domain_polygon)
    print(f"Kept: {stats['kept']}", file=sys.stderr)
    print(f"Dropped (outside or crossing boundary): {stats['dropped_outside_or_crossing']}", file=sys.stderr)
    print(f"Dropped (no usable geometry): {stats['dropped_no_geometry']}", file=sys.stderr)
    print(f"Vertices (compacted): {stats['vertices']}", file=sys.stderr)

    with open(args.output, "w") as f:
        json.dump(new_cm, f)
    print(f"Wrote {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
