"""Prototype: a coarse whole-island terrain mesh for the 3D view, for visual
orientation/context only -- NOT part of the SBG dataset itself and not used
by any CFD-facing pipeline (Phases 3/5 already have their own real terrain
handling: conforming_mesh.py's constrained triangulation for local CFD
domains, this is unrelated).

Why binary, not CityJSON: a naive full-island 20m-step terrain triangulated
into CityJSON boundaries was measured (see project plan) at ~539MB / ~70s to
build -- prohibitively expensive to route through the same JSON pipeline
that already makes full-island building loads slow. The DTM is a *regular
grid*, so the client can reconstruct vertex positions and triangle topology
itself from just the raw heights + a small affine-transform header -- no
JSON.parse, no structured-clone, just an ArrayBuffer read directly into a
Float32Array. Payload size matches the source GeoTIFF almost exactly
(~30MB), not the ~540MB a triangulated JSON representation would need.

Why masked: data/dtm.tif (built by sbg.topo.dtm's whole-island path) is a
FULLY-FILLED rectangle covering the contour data's bounding box (confirmed:
0 NaN cells, 89.1km x 34.4km) -- griddata's nearest-neighbor gap-fill
extrapolates flat "terrain" far out over open ocean/empty space, since
there's no coastline mask anywhere in this pipeline. Rendering that as-is
would show a fake rectangular land shelf extending well past Singapore's
real coastline.

No real coastline data exists in this project's inputs (NationalMapLine.geojson
only has Contour_250K/roads/expressways, no land-boundary layer -- checked
directly). Two masking approaches were tried:
  - Reusing dtm.build_dtm's own max_gap (distance from each grid cell to the
    nearest real CONTOUR point): fails badly at whole-island scale, same
    finding already documented for dtm.py's own max_gap -- 91-95% of the
    grid comes back NaN even at a 150m threshold, because contour lines are
    genuinely sparse over flat interior land (no contour crossings for
    kilometers), not because that land is off-island. This mask can't tell
    "far from a contour line" apart from "not on land."
  - Distance from each grid cell to the nearest BUILDING centroid (this
    module): buildings exist all over the real island and nowhere over
    ocean, so this is a much better land/sea discriminator even though it's
    a proxy, not real coastline data. Visually confirmed (500-1000m
    thresholds) to produce a recognizable Singapore silhouette. Will
    under-mask large building-free-but-real-land areas (central catchment
    nature reserve, big parks, reservoirs, airport runways) -- acceptable
    given this is explicitly for visual orientation only, not measurement.
"""
import struct

import numpy as np
import rasterio
from scipy.spatial import cKDTree

from sbg.io_cityjson import building_footprint_centroid
from sbg.topo.dtm import DTM_PATH

DEFAULT_LAND_DIST_THRESHOLD = 500.0  # meters -- see module docstring


def build_masked_island_grid(cm, dtm_path=DTM_PATH, land_dist_threshold=DEFAULT_LAND_DIST_THRESHOLD):
    """Loads the whole-island DTM (already built by `python -m sbg.topo.dtm`,
    not rebuilt here -- that's a ~20-30s griddata interpolation, no reason to
    pay it again when data/dtm.tif already exists) and NaNs out any cell
    farther than land_dist_threshold from the nearest building centroid.
    Returns (grid_z, transform), grid_z float32 with NaN for masked cells.
    """
    with rasterio.open(dtm_path) as src:
        grid_z = src.read(1).astype("float32")
        transform = src.transform

    centroids = []
    for obj in cm["CityObjects"].values():
        if obj["type"] != "Building":
            continue
        c = building_footprint_centroid(cm, obj)
        if c is not None:
            centroids.append(c)
    tree = cKDTree(np.array(centroids))

    nrows, ncols = grid_z.shape
    cols, rows = np.meshgrid(np.arange(ncols), np.arange(nrows))
    gx = transform.a * cols + transform.c
    gy = transform.e * rows + transform.f
    grid_pts = np.column_stack([gx.ravel(), gy.ravel()])

    dist, _ = tree.query(grid_pts, k=1, workers=-1)
    far_mask = dist.reshape(grid_z.shape) > land_dist_threshold
    grid_z[far_mask] = np.nan

    return grid_z, transform


def pack_terrain_binary(grid_z, transform):
    """Packs (grid_z, transform) into a small binary payload the client
    reconstructs a heightfield mesh from directly:

      header (32 bytes): ncols:i4, nrows:i4, xmin:f8, ymax:f8, step:f8
      body: nrows*ncols float32 heights, row-major, row 0 = north edge
            (matches rasterio's own convention) -- NaN cells (masked/no
            data) pass through as real IEEE754 NaN bit patterns, which
            JS's Float32Array reads back as NaN natively, no sentinel
            encoding needed.

    step assumes square cells (true here: sbg.topo.dtm always builds a
    single `step` for both axes) -- transform.a is +step (east), transform.e
    is -step (south), so plain `transform.a` recovers it directly.
    """
    nrows, ncols = grid_z.shape
    header = struct.pack("<iiddd", ncols, nrows, transform.c, transform.f, transform.a)
    return header + grid_z.astype("<f4").tobytes()
