"""DTM + graded pads for sitting OneMap building meshes on real terrain.

OneMap models every building base at a single z=0 datum (no per-building ground
elevation). To place them on real terrain we:
  1. Build a DTM for the domain from the SLA contour points (sbg.topo.dtm).
  2. For each building, take pad_z = the MIN DTM elevation under its footprint
     (min, so a building on a slope is never buried on its uphill side), and
     shift the whole building up by pad_z so its flat base sits at pad_z.
  3. Flatten a graded PAD in the terrain grid to pad_z within the footprint --
     the physically-correct model for a flat-bottomed building (real buildings
     sit on level graded platforms, not raked to the hillside). Barely differs
     from draping on flat terrain, matters on genuine slope (Bukit Timah etc.).
  4. Give each building a downward skirt (footprint prism) plunging PLUNGE_M
     below pad_z, into the solidified terrain slab, so it fuses during the
     whole-scene voxel remesh -- also fixes podium-tower developments whose
     tower mesh starts above ground (e.g. CityLights, base ~19m).
"""
from pathlib import Path

import numpy as np

from sbg.topo.dtm import build_dtm, load_points

PLUNGE_M = 30.0       # how far below pad_z each building skirt reaches (into the slab)
SOLIDIFY_M = 60.0     # terrain slab thickness (> PLUNGE_M so buildings stay embedded)
DEFAULT_STEP = 5.0
# Whole-island DTM cache (20m, EPSG:3414, covers all Singapore). Cropping+resampling
# this per domain skips the ~3.5s scipy.griddata Delaunay interp entirely -- topo data
# changes far less often than buildings (which we already cache in the store). A fresh
# 5m griddata was measured to agree with this 20m cache to within the source's own
# 20m-contour uncertainty. Set CACHED_DTM=None (or delete the file) to force griddata.
CACHED_DTM = "data/dtm.tif"


class DtmSampler:
    """Bilinear elevation sampler over an in-memory DTM grid + affine."""

    def __init__(self, grid_z, affine):
        self.grid_z = grid_z
        self.affine = affine
        self.inv = ~affine
        self.nrows, self.ncols = grid_z.shape

    def __call__(self, x, y):
        # Array-capable bilinear sampler: pass scalars or equal-length arrays.
        # (Vectorized so place_on_terrain can sample a whole footprint at once.)
        x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
        col, row = self.inv * (x, y)
        col = np.clip(col, 0, self.ncols - 1.001)
        row = np.clip(row, 0, self.nrows - 1.001)
        c0, r0 = col.astype(int), row.astype(int)
        tx, ty = col - c0, row - r0
        z0 = self.grid_z[r0, c0] * (1 - tx) + self.grid_z[r0, c0 + 1] * tx
        z1 = self.grid_z[r0 + 1, c0] * (1 - tx) + self.grid_z[r0 + 1, c0 + 1] * tx
        return z0 * (1 - ty) + z1 * ty


def build_domain_dtm(domain_bbox, step=DEFAULT_STEP, margin=150.0, cache=CACHED_DTM):
    """domain_bbox = (xmin, ymin, xmax, ymax) EPSG:3414. Returns (grid_z, affine).

    Crops the whole-island cache (fast, ~50ms) when available; otherwise builds a
    fresh DTM from the raw contour points via griddata (~3.5s).
    """
    xmin, ymin, xmax, ymax = domain_bbox
    bbox = (xmin - margin, ymin - margin, xmax + margin, ymax + margin)
    if cache and Path(cache).exists():
        return _dtm_from_cache(cache, bbox, step)
    xs, ys, zs = load_points(bbox=bbox)
    return build_dtm(xs, ys, zs, step=step, bounds_override=bbox)


def _dtm_from_cache(cache, bbox, step):
    """Read the cache window over bbox and bilinearly resample to a `step` grid.
    Returns (grid_z, affine) in the same north-up convention build_dtm produces."""
    import rasterio
    from rasterio.windows import from_bounds
    from affine import Affine

    xmin, ymin, xmax, ymax = bbox
    with rasterio.open(cache) as ds:
        # +1-cell pad so bilinear sampling at the bbox edge still has neighbours
        px = ds.res[0]
        win = from_bounds(xmin - px, ymin - px, xmax + px, ymax + px, ds.transform)
        coarse = ds.read(1, window=win, boundless=True, fill_value=np.nan)
        coarse_affine = ds.window_transform(win)
    coarse = np.nan_to_num(coarse, nan=float(np.nanmin(coarse)))  # edge-only, tif fills SG
    sampler = DtmSampler(coarse, coarse_affine)

    ncols = max(2, int(np.ceil((xmax - xmin) / step)))
    nrows = max(2, int(np.ceil((ymax - ymin) / step)))
    affine = Affine(step, 0, xmin, 0, -step, ymax)  # north-up: row 0 == ymax
    cx = affine.c + affine.a * (np.arange(ncols) + 0.5)
    cy = affine.f + affine.e * (np.arange(nrows) + 0.5)
    GX, GY = np.meshgrid(cx, cy)
    grid_z = sampler(GX.ravel(), GY.ravel()).reshape(nrows, ncols)
    return grid_z, affine


def place_on_terrain(pieces, dtm):
    """Shift each building piece onto the terrain and add a plunge skirt.

    Returns (verts (V,3), faces (F,3), footprints) where footprints is a list of
    (hull_xy, pad_z) for pad-flattening. Pieces with no usable footprint hull
    are skipped.
    """
    all_v, all_f, footprints = [], [], []
    voff = 0
    for p in pieces:
        hull = p["footprint"]
        if hull is None or len(hull) < 3:
            continue
        base_z = p["base_z"]
        pad_z = float(np.min(dtm(hull[:, 0], hull[:, 1])))  # min elev under footprint

        v = p["verts"].copy()
        v[:, 2] += pad_z - base_z  # base sits at terrain pad_z
        all_v.append(v)
        all_f.append(p["faces"] + voff)
        voff += len(v)

        nh = len(hull)
        top = np.column_stack([hull, np.full(nh, pad_z + 0.3)])
        bot = np.column_stack([hull, np.full(nh, pad_z - PLUNGE_M)])
        sv = np.vstack([top, bot])
        # Vectorized skirt: wall quad (2 tris) per edge + a bottom-cap fan.
        i = np.arange(nh)
        j = (i + 1) % nh
        wall = np.concatenate([np.column_stack([i, j, nh + j]),
                               np.column_stack([i, nh + j, nh + i])])
        k = np.arange(1, nh - 1)
        cap = np.column_stack([np.full(nh - 2, nh), nh + k + 1, nh + k])
        sf = np.concatenate([wall, cap])
        all_v.append(sv)
        all_f.append(sf + voff)
        voff += len(sv)

        footprints.append((hull, pad_z))

    if not all_v:
        return np.empty((0, 3)), np.empty((0, 3), dtype=np.int64), []
    return np.concatenate(all_v), np.concatenate(all_f), footprints


try:
    import cv2 as _cv2
except ImportError:  # optional -- flatten_pads falls back to matplotlib without it
    _cv2 = None


def flatten_pads(grid_z, affine, footprints):
    """Return a copy of grid_z with cells inside each footprint set to its pad_z.

    Uses OpenCV scan-line polygon fill (~4x faster than matplotlib's per-cell
    point-in-polygon test) when cv2 is installed, else the matplotlib fallback.
    The two differ only on ~0.4% of cells, all at footprint boundaries (half-cell
    rasterization vs cell-center test) -- negligible under a 30m skirt + 2m remesh.
    """
    inv = ~affine  # world (x, y) -> (col, row)
    out = grid_z.copy()
    if _cv2 is not None:
        for hull, pad_z in footprints:
            cols, rows = inv * (hull[:, 0], hull[:, 1])
            pts = np.round(np.column_stack([cols, rows])).astype(np.int32)
            _cv2.fillPoly(out, [pts], color=float(pad_z))
        return out

    from matplotlib.path import Path as MplPath
    nrows, ncols = grid_z.shape
    cx = affine.c + affine.a * (np.arange(ncols) + 0.5)
    cy = affine.f + affine.e * (np.arange(nrows) + 0.5)
    for hull, pad_z in footprints:
        xs, ys = hull[:, 0], hull[:, 1]
        cols, rows = zip(*(inv * (x, y) for x, y in
                           [(xs.min(), ys.min()), (xs.max(), ys.max()),
                            (xs.min(), ys.max()), (xs.max(), ys.min())]))
        c0, c1 = max(0, int(min(cols)) - 1), min(ncols, int(max(cols)) + 2)
        r0, r1 = max(0, int(min(rows)) - 1), min(nrows, int(max(rows)) + 2)
        if c1 <= c0 or r1 <= r0:
            continue
        SX, SY = np.meshgrid(cx[c0:c1], cy[r0:r1])
        inside = MplPath(hull).contains_points(
            np.column_stack([SX.ravel(), SY.ravel()])).reshape(r1 - r0, c1 - c0)
        sub = out[r0:r1, c0:c1]
        sub[inside] = pad_z
        out[r0:r1, c0:c1] = sub
    return out


def terrain_surface_mesh(grid_z, affine):
    """Grid -> (verts (V,3), faces (F,3)) triangulated surface."""
    nrows, ncols = grid_z.shape
    cx = affine.c + affine.a * (np.arange(ncols) + 0.5)
    cy = affine.f + affine.e * (np.arange(nrows) + 0.5)
    GX, GY = np.meshgrid(cx, cy)
    verts = np.column_stack([GX.ravel(), GY.ravel(), grid_z.ravel()])
    # Vectorized: two triangles per grid quad, same winding as the old loop.
    r, c = np.meshgrid(np.arange(nrows - 1), np.arange(ncols - 1), indexing="ij")
    i00 = (r * ncols + c).ravel()
    i01, i10, i11 = i00 + 1, i00 + ncols, i00 + ncols + 1
    faces = np.empty((2 * len(i00), 3), dtype=np.int64)
    faces[0::2] = np.column_stack([i00, i10, i11])
    faces[1::2] = np.column_stack([i00, i11, i01])
    return verts, faces
