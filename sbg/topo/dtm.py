"""Builds a regular-grid DTM from a contour/point elevation cloud via
scipy.interpolate.griddata, writes a GeoTIFF, and exposes a bilinear
elevation_at(x, y) lookup for later phases (Deliverable 4's terrain-aware
add_building calls this instead of assuming flat z=0).

Two intended uses, since a fine grid over all of Singapore is impractical
(~149M cells at 3m) while a fine grid over a specific bounded CFD domain is
cheap (a 2km x 2km box at 3m is ~445K cells):
  - Whole-island overview: coarse step (matches the source's real ~20m
    contour-interval resolution), point cloud decimated (contour lines are
    traced at ~0.17m median vertex spacing per direct measurement — far
    denser than any output grid needs, so decimating loses no real shape
    information, just redundant duplicate points along each line).
  - Local CFD domain: bbox-filtered points first (naturally limits point
    count to the region), finer step (e.g. 3m to match a CFD mesh), little
    or no decimation needed since the bbox filter already bounds the input.

Usage:
  .venv/bin/python -m sbg.topo.dtm                          # whole-island, coarse
  .venv/bin/python -m sbg.topo.dtm --bbox 29000,29500,31500,32000 --step 3 -o data/dtm_local.tif
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin
from scipy.interpolate import griddata

from sbg.config import DATA_DIR, TARGET_CRS
from sbg.topo.contours import OUTPUT_PATH as CONTOUR_POINTS_PATH

ISLAND_GRID_STEP = 20.0  # meters — matches the source's real contour-interval resolution
ISLAND_STRIDE = 50  # ~8.5m avg point spacing after decimation, plenty for a 20m grid
DTM_PATH = DATA_DIR / "dtm.tif"


def load_points(path=CONTOUR_POINTS_PATH, stride=1, bbox=None):
    """bbox=(xmin, ymin, xmax, ymax) filters BEFORE decimation, so a bounded
    local domain naturally keeps native point density without needing
    aggressive stride decimation.
    """
    data = np.load(path)
    xs, ys, zs = data["x"], data["y"], data["z"]

    if bbox is not None:
        xmin, ymin, xmax, ymax = bbox
        mask = (xs >= xmin) & (xs <= xmax) & (ys >= ymin) & (ys <= ymax)
        xs, ys, zs = xs[mask], ys[mask], zs[mask]

    return xs[::stride], ys[::stride], zs[::stride]


def build_dtm(xs, ys, zs, step, max_gap=None, bounds_override=None):
    """Returns (grid_z, transform). grid_z is a 2D array in raster convention
    (row 0 = north edge, increasing row = south); transform is the affine
    georeferencing a GeoTIFF expects.

    bounds_override: optional (xmin, ymin, xmax, ymax) the output grid must
    cover *at least* -- real bug, user report (island_terrain.py's 3D
    overlay): the whole-island grid's extent was always just the CONTOUR
    points' own bounding box, so any real building sitting even slightly
    beyond wherever the source contour data happens to reach (confirmed: 29
    real buildings, just north of the DTM's own top edge -- the contour
    source simply doesn't extend that far) got no terrain at all, rendering
    as floating/clipped. Only ever widens the grid, never shrinks it below
    the contour points' natural extent; the extra margin gets filled by the
    same nearest-neighbor gap-fill already used for interior NaN cells.

    max_gap: if set, any output cell farther than this distance (meters) from
    the nearest real input point is set to NaN instead of an interpolated
    value. griddata's Delaunay triangulation of the scattered input points
    will happily bridge real gaps in contour coverage (e.g. mainland to a
    distant offshore island, or areas with no nearby contour crossings at
    all) with long, fake, "flat wedge" triangles — those aren't real terrain,
    just linear interpolation across empty space. This suppresses them by
    distance-to-data instead (the output grid's own triangulation, if built
    later for a TIN, always has uniform small edges by construction, so
    filtering has to happen here against the *scattered input* density, not
    the regular output grid). Empirically: 99% of input points (after
    decimation) have a neighbor within ~15m; the single largest observed gap
    was ~37km (an isolated point far from any other data).
    """
    xmin, xmax = xs.min(), xs.max()
    ymin, ymax = ys.min(), ys.max()
    if bounds_override is not None:
        oxmin, oymin, oxmax, oymax = bounds_override
        xmin, ymin = min(xmin, oxmin), min(ymin, oymin)
        xmax, ymax = max(xmax, oxmax), max(ymax, oymax)

    ncols = int(np.ceil((xmax - xmin) / step)) + 1
    nrows = int(np.ceil((ymax - ymin) / step)) + 1
    col_x = xmin + np.arange(ncols) * step
    row_y = ymax - np.arange(nrows) * step  # row 0 = north (ymax)

    gx, gy = np.meshgrid(col_x, row_y)
    points = np.column_stack([xs, ys])
    grid_z = griddata(points, zs, (gx, gy), method="linear")

    nan_mask = np.isnan(grid_z)
    if nan_mask.any():
        grid_z_nearest = griddata(points, zs, (gx, gy), method="nearest")
        grid_z[nan_mask] = grid_z_nearest[nan_mask]

    if max_gap is not None:
        from scipy.spatial import cKDTree

        tree = cKDTree(points)
        grid_pts = np.column_stack([gx.ravel(), gy.ravel()])
        dist, _ = tree.query(grid_pts, k=1)
        gap_mask = (dist > max_gap).reshape(gx.shape)
        grid_z[gap_mask] = np.nan

    transform = from_origin(xmin, ymax, step, step)
    return grid_z.astype("float32"), transform


def write_geotiff(grid_z, transform, path=DTM_PATH, crs=TARGET_CRS, nodata=np.nan):
    """Write a single-band north-up GeoTIFF.

    `nodata` is a parameter rather than hardcoded NaN because not every consumer copes
    with NaN -- a Fortran-ish reader will happily treat it as a number. Callers shipping
    to an external code should pass an explicit sentinel (e.g. -9999.0).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=grid_z.shape[0],
        width=grid_z.shape[1],
        count=1,
        dtype=grid_z.dtype,
        crs=crs,
        transform=transform,
        nodata=nodata,
    ) as dst:
        dst.write(grid_z, 1)


class ElevationLookup:
    """Bilinear elevation_at(x, y) sampling of a DTM GeoTIFF."""

    def __init__(self, path=DTM_PATH):
        with rasterio.open(path) as src:
            self.grid_z = src.read(1)
            self.transform = src.transform
        self.inv_transform = ~self.transform
        self.nrows, self.ncols = self.grid_z.shape

    def __call__(self, x, y):
        col, row = self.inv_transform * (x, y)
        col = min(max(col, 0), self.ncols - 1.001)
        row = min(max(row, 0), self.nrows - 1.001)
        c0, r0 = int(col), int(row)
        c1, r1 = c0 + 1, r0 + 1
        tx, ty = col - c0, row - r0
        z00 = self.grid_z[r0, c0]
        z10 = self.grid_z[r0, c1]
        z01 = self.grid_z[r1, c0]
        z11 = self.grid_z[r1, c1]
        z0 = z00 * (1 - tx) + z10 * tx
        z1 = z01 * (1 - tx) + z11 * tx
        return float(z0 * (1 - ty) + z1 * ty)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bbox", help="xmin,ymin,xmax,ymax in EPSG:3414 meters (default: whole island)")
    ap.add_argument("--step", type=float, help="Grid step in meters (default: 20 whole-island, 3 if --bbox given)")
    ap.add_argument("--stride", type=int, help="Point-cloud decimation stride (default: 50 whole-island, 1 if --bbox given)")
    ap.add_argument(
        "--max-gap",
        type=float,
        default=None,
        help=(
            "NaN out cells farther than this (meters) from real data. Off by default — "
            "tried as a fix for griddata's fake 'flat wedge' triangles spanning real gaps "
            "in contour coverage, but a single global distance threshold can't tell that "
            "apart from normal sparse interpolation across flat terrain (most of the "
            "island got NaN'd out in testing). Needs a real coastline mask or an alpha-shape "
            "approach to do properly — not implemented. Opt in explicitly if you want to see it."
        ),
    )
    ap.add_argument("-o", "--output", default=str(DTM_PATH), help="Output GeoTIFF path")
    args = ap.parse_args()

    bbox = tuple(float(v) for v in args.bbox.split(",")) if args.bbox else None
    step = args.step if args.step is not None else (3.0 if bbox else ISLAND_GRID_STEP)
    stride = args.stride if args.stride is not None else (1 if bbox else ISLAND_STRIDE)
    max_gap = args.max_gap

    # Whole-island mode only: widen the grid to cover every real building,
    # not just wherever the source contour data happens to reach -- see
    # build_dtm's own bounds_override docstring for the real bug this
    # fixes (29 real buildings north of the contour data's own extent had
    # no terrain at all).
    bounds_override = None
    if bbox is None:
        import json

        from sbg.config import SBG_OUTPUT

        print("Loading SBG dataset to compute full building extent...", file=sys.stderr)
        with open(SBG_OUTPUT) as f:
            sbg_cm = json.load(f)
        # Vectorized dequantization -- millions of vertices through
        # io_cityjson.vertex_lookup's per-index Python closure would be
        # needlessly slow for a plain bbox computation.
        verts = np.array(sbg_cm["vertices"], dtype=np.float64)
        sx, sy, _sz = sbg_cm["transform"]["scale"]
        tx, ty, _tz = sbg_cm["transform"]["translate"]
        real_x = verts[:, 0] * sx + tx
        real_y = verts[:, 1] * sy + ty
        bounds_override = (float(real_x.min()), float(real_y.min()), float(real_x.max()), float(real_y.max()))
        print(f"  building extent: {bounds_override}", file=sys.stderr)

    print("Loading contour points...", file=sys.stderr)
    xs, ys, zs = load_points(stride=stride, bbox=bbox)
    print(f"  {len(xs)} points (stride={stride}, bbox={bbox})", file=sys.stderr)

    print(f"Interpolating grid (step={step}m, max_gap={max_gap})...", file=sys.stderr)
    grid_z, transform = build_dtm(xs, ys, zs, step=step, max_gap=max_gap, bounds_override=bounds_override)
    print(f"  grid shape: {grid_z.shape}, NaN cells: {np.isnan(grid_z).sum()}", file=sys.stderr)

    from pathlib import Path

    out_path = Path(args.output)
    write_geotiff(grid_z, transform, path=out_path)
    print(f"Wrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
