"""Deliverable 2: overlay user-specified topography onto SBG as one CityJSON
containing a TINRelief CityObject (the terrain) alongside the existing
Building CityObjects — not a Blender-fused mesh. Keeps buildings individually
addressable and the file spec-valid; Blender's deep-extrude+boolean-union is
the right tool specifically for Deliverable 5's watertight STL, not this.

Usage:
  .venv/bin/python -m sbg.topo.overlay_cityjson --dtm data/dtm.tif -o data/overlay.city.json
  .venv/bin/python -m sbg.topo.overlay_cityjson --dtm data/dtm_local.tif --sbg data/cutout.city.json -o data/overlay_local.city.json
"""
import argparse
import json
import sys

import numpy as np
import rasterio

from sbg.config import SBG_OUTPUT
from sbg.io_cityjson import VertexPool, building_footprint_centroid, finalize_and_save, new_cityjson
from sbg.topo.dtm import ElevationLookup


def triangulate_grid(pool, grid_z, transform):
    """Builds a CompositeSurface boundaries list (one ring per triangle) from
    a regular elevation grid, skipping any 2x2 cell touching a NaN corner
    (nodata/gap). Vectorized index-grid construction; only the final
    pool.add()/face-append steps are plain Python loops (unavoidable — pool
    dedup is dict-based, and CityJSON boundaries are plain nested lists).
    """
    nrows, ncols = grid_z.shape
    cols, rows = np.meshgrid(np.arange(ncols), np.arange(nrows))
    xs = transform.a * cols + transform.b * rows + transform.c
    ys = transform.d * cols + transform.e * rows + transform.f

    valid = ~np.isnan(grid_z)
    vertex_idx = np.full((nrows, ncols), -1, dtype=np.int64)
    valid_rc = np.argwhere(valid)
    for r, c in valid_rc:
        vertex_idx[r, c] = pool.add(float(xs[r, c]), float(ys[r, c]), float(grid_z[r, c]))

    v00 = vertex_idx[:-1, :-1]
    v01 = vertex_idx[:-1, 1:]
    v10 = vertex_idx[1:, :-1]
    v11 = vertex_idx[1:, 1:]
    cell_valid = (v00 >= 0) & (v01 >= 0) & (v10 >= 0) & (v11 >= 0)

    i00 = v00[cell_valid]
    i01 = v01[cell_valid]
    i10 = v10[cell_valid]
    i11 = v11[cell_valid]

    faces = []
    for a, b, c, d in zip(i00.tolist(), i01.tolist(), i11.tolist(), i10.tolist()):
        faces.append([[a, b, c]])
        faces.append([[a, c, d]])
    return faces


def _remap_boundaries(boundaries, lookup, pool, z_offset=0.0):
    if isinstance(boundaries, int):
        x, y, z = lookup(boundaries)
        return pool.add(x, y, z + z_offset)
    return [_remap_boundaries(item, lookup, pool, z_offset) for item in boundaries]


def add_sbg_buildings(pool, cm, sbg, elevation_lookup=None):
    """elevation_lookup, if given, drapes each building onto the terrain:
    SBG buildings were extruded with base_z=0 (built before terrain existed),
    but real ground elevation is non-zero almost everywhere (checked: 1-25m
    across a small sample) — without draping, buildings would render buried
    under or floating disconnected from the terrain surface instead of
    sitting on it. Offsets every vertex by the ground elevation at that
    building's own footprint centroid.
    """
    verts = sbg["vertices"]
    transform = sbg["transform"]
    sx, sy, sz = transform["scale"]
    tx, ty, tz = transform["translate"]

    def lookup(idx):
        x, y, z = verts[idx]
        return (x * sx + tx, y * sy + ty, z * sz + tz)

    for obj_id, obj in sbg["CityObjects"].items():
        z_offset = 0.0
        if elevation_lookup is not None:
            centroid = building_footprint_centroid(sbg, obj)
            if centroid is not None:
                z_offset = elevation_lookup(*centroid)

        new_geoms = []
        for geom in obj["geometry"]:
            new_geom = dict(geom)
            new_geom["boundaries"] = _remap_boundaries(geom["boundaries"], lookup, pool, z_offset)
            new_geoms.append(new_geom)
        cm["CityObjects"][obj_id] = {
            "type": obj["type"],
            "attributes": obj["attributes"],
            "geometry": new_geoms,
        }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dtm", required=True, help="Input DTM GeoTIFF")
    ap.add_argument("--sbg", default=str(SBG_OUTPUT), help="Input SBG CityJSON (buildings to include)")
    ap.add_argument("-o", "--output", required=True, help="Output overlay CityJSON path")
    args = ap.parse_args()

    print(f"Loading DTM {args.dtm}...", file=sys.stderr)
    with rasterio.open(args.dtm) as src:
        grid_z = src.read(1)
        transform = src.transform

    print(f"Loading SBG {args.sbg}...", file=sys.stderr)
    sbg = json.load(open(args.sbg))

    pool = VertexPool()
    cm = new_cityjson()

    print("Triangulating terrain...", file=sys.stderr)
    terrain_faces = triangulate_grid(pool, grid_z, transform)
    cm["CityObjects"]["terrain"] = {
        "type": "TINRelief",
        "attributes": {},
        "geometry": [{"type": "CompositeSurface", "lod": "1", "boundaries": terrain_faces}],
    }
    print(f"  {len(terrain_faces)} terrain triangles, {len(pool.vertices)} vertices so far", file=sys.stderr)

    print("Adding SBG buildings (draped onto terrain)...", file=sys.stderr)
    elevation_lookup = ElevationLookup(args.dtm)
    add_sbg_buildings(pool, cm, sbg, elevation_lookup=elevation_lookup)
    print(f"  {len(sbg['CityObjects'])} buildings added", file=sys.stderr)

    finalize_and_save(cm, pool, args.output)
    print(f"Wrote {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
