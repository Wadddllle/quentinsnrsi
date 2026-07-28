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
import numpy as np

from sbg.topo.dtm import build_dtm, load_points

PLUNGE_M = 30.0       # how far below pad_z each building skirt reaches (into the slab)
SOLIDIFY_M = 60.0     # terrain slab thickness (> PLUNGE_M so buildings stay embedded)
DEFAULT_STEP = 5.0


class DtmSampler:
    """Bilinear elevation sampler over an in-memory DTM grid + affine."""

    def __init__(self, grid_z, affine):
        self.grid_z = grid_z
        self.affine = affine
        self.inv = ~affine
        self.nrows, self.ncols = grid_z.shape

    def __call__(self, x, y):
        col, row = self.inv * (x, y)
        col = min(max(col, 0), self.ncols - 1.001)
        row = min(max(row, 0), self.nrows - 1.001)
        c0, r0 = int(col), int(row)
        tx, ty = col - c0, row - r0
        z0 = self.grid_z[r0, c0] * (1 - tx) + self.grid_z[r0, c0 + 1] * tx
        z1 = self.grid_z[r0 + 1, c0] * (1 - tx) + self.grid_z[r0 + 1, c0 + 1] * tx
        return float(z0 * (1 - ty) + z1 * ty)


def build_domain_dtm(domain_bbox, step=DEFAULT_STEP, margin=150.0):
    """domain_bbox = (xmin, ymin, xmax, ymax) EPSG:3414. Returns (grid_z, affine)."""
    xmin, ymin, xmax, ymax = domain_bbox
    bbox = (xmin - margin, ymin - margin, xmax + margin, ymax + margin)
    xs, ys, zs = load_points(bbox=bbox)
    grid_z, affine = build_dtm(xs, ys, zs, step=step, bounds_override=bbox)
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
        pad_z = min(dtm(px, py) for px, py in hull)

        v = p["verts"].copy()
        v[:, 2] += pad_z - base_z  # base sits at terrain pad_z
        all_v.append(v)
        all_f.append(p["faces"] + voff)
        voff += len(v)

        nh = len(hull)
        top = np.column_stack([hull, np.full(nh, pad_z + 0.3)])
        bot = np.column_stack([hull, np.full(nh, pad_z - PLUNGE_M)])
        sv = np.vstack([top, bot])
        sf = []
        for i in range(nh):
            j = (i + 1) % nh
            sf.append([i, j, nh + j])
            sf.append([i, nh + j, nh + i])
        for i in range(1, nh - 1):
            sf.append([nh, nh + i + 1, nh + i])  # bottom cap
        all_v.append(sv)
        all_f.append(np.array(sf) + voff)
        voff += len(sv)

        footprints.append((hull, pad_z))

    if not all_v:
        return np.empty((0, 3)), np.empty((0, 3), dtype=np.int64), []
    return np.concatenate(all_v), np.concatenate(all_f), footprints


def flatten_pads(grid_z, affine, footprints):
    """Return a copy of grid_z with cells inside each footprint set to its pad_z."""
    from matplotlib.path import Path as MplPath

    nrows, ncols = grid_z.shape
    cx = affine.c + affine.a * (np.arange(ncols) + 0.5)
    cy = affine.f + affine.e * (np.arange(nrows) + 0.5)
    GX, GY = np.meshgrid(cx, cy)
    grid_pts = np.column_stack([GX.ravel(), GY.ravel()])
    out = grid_z.copy().ravel()
    for hull, pad_z in footprints:
        inside = MplPath(hull).contains_points(grid_pts)
        out[inside] = pad_z
    return out.reshape(grid_z.shape)


def terrain_surface_mesh(grid_z, affine):
    """Grid -> (verts (V,3), faces (F,3)) triangulated surface."""
    nrows, ncols = grid_z.shape
    cx = affine.c + affine.a * (np.arange(ncols) + 0.5)
    cy = affine.f + affine.e * (np.arange(nrows) + 0.5)
    GX, GY = np.meshgrid(cx, cy)
    verts = np.column_stack([GX.ravel(), GY.ravel(), grid_z.ravel()])
    faces = []
    for r in range(nrows - 1):
        base = r * ncols
        for c in range(ncols - 1):
            i00, i01 = base + c, base + c + 1
            i10, i11 = base + ncols + c, base + ncols + c + 1
            faces.append([i00, i10, i11])
            faces.append([i00, i11, i01])
    return verts, np.array(faces)
