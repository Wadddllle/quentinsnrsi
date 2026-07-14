"""Streamed elevation point-cloud extraction from a national map-line style
GeoJSON (mixed layers selected via a FOLDERPATH-like property). Generic across
similarly-structured "user-specified topography data" inputs — the folder
filter, elevation field, and CRS are all parameters, not hardcoded to one file.

Usage: .venv/bin/python -m sbg.topo.contours
"""
import sys

import ijson
import numpy as np
from pyproj import Transformer

from sbg.config import DATA_DIR, NATIONAL_MAP_LINE_GEOJSON, SOURCE_CRS, TARGET_CRS

DEFAULT_FOLDER_FILTER = "Layers/Contour_250K"
DEFAULT_ELEVATION_FIELD = "NAME"
DEFAULT_FOLDER_PROPERTY = "FOLDERPATH"

OUTPUT_PATH = DATA_DIR / "contour_points.npz"


def iter_elevation_points(
    path=NATIONAL_MAP_LINE_GEOJSON,
    folder_property=DEFAULT_FOLDER_PROPERTY,
    folder_filter=DEFAULT_FOLDER_FILTER,
    elevation_field=DEFAULT_ELEVATION_FIELD,
    source_crs=SOURCE_CRS,
    target_crs=TARGET_CRS,
):
    """Streams (x, y, z) points in target_crs from elevation-bearing line
    features whose properties[folder_property] == folder_filter, treating
    properties[elevation_field] as that feature's elevation (meters) for
    every vertex on its line(s).
    """
    transformer = Transformer.from_crs(source_crs, target_crs, always_xy=True)

    with open(path, "rb") as f:
        for feature in ijson.items(f, "features.item"):
            props = feature.get("properties", {})
            if props.get(folder_property) != folder_filter:
                continue
            try:
                elevation = float(props.get(elevation_field))
            except (TypeError, ValueError):
                continue

            geom = feature.get("geometry") or {}
            gtype = geom.get("type")
            coords = geom.get("coordinates")
            if gtype == "LineString":
                lines = [coords]
            elif gtype == "MultiLineString":
                lines = coords
            else:
                continue

            for line in lines:
                if len(line) < 1:
                    continue
                lons = [pt[0] for pt in line]
                lats = [pt[1] for pt in line]
                xs, ys = transformer.transform(lons, lats)
                for x, y in zip(xs, ys):
                    yield (x, y, elevation)


def main():
    xs, ys, zs = [], [], []
    count = 0
    for x, y, z in iter_elevation_points():
        xs.append(x)
        ys.append(y)
        zs.append(z)
        count += 1
        if count % 200000 == 0:
            print(f"  ...{count} points", file=sys.stderr)

    print(f"Total points: {count}", file=sys.stderr)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    np.savez(OUTPUT_PATH, x=np.array(xs), y=np.array(ys), z=np.array(zs))
    print(f"Wrote {OUTPUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
