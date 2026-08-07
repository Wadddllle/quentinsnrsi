"""DTM + graded pads for sitting OneMap building meshes on real terrain.

OneMap models every building base at a single z=0 datum (no per-building ground
elevation). To place them on real terrain we:
  1. Build a DTM for the domain from the SLA contour points (sbg.topo.dtm).
  2. For each building, take pad_z = a robust LOW PERCENTILE (25th) of the DTM
     under its footprint, and shift the whole building up by pad_z so its flat
     base sits at pad_z. NOT the min: the DTM is griddata-interpolated from sparse
     20m contours, so it's built of flat Delaunay facets whose creases give
     spurious local dips -- `min` chases the single deepest crease and sinks the
     whole building into a pit below the surrounding grade (visible as buildings
     "sunk on all 4 sides" with a 1-2m terrain ridge between neighbours). A low
     percentile ignores that one dip and sits the building near true grade.
  3. Flatten a PAD in the terrain grid to pad_z within the footprint. (A raster
     APRON that grades pad_z back to terrain was tried and turned OFF -- on slopes
     it excavates a moat around each building; see flatten_pads. Clean grading
     needs breakline/CDT terrain, not a raster tweak.)
  4. Give each building a downward skirt (footprint prism) plunging PLUNGE_M
     below pad_z, into the solidified terrain slab, so it fuses during the
     whole-scene voxel remesh -- also fixes podium-tower developments whose
     tower mesh starts above ground (e.g. CityLights, base ~19m).
"""
from pathlib import Path

import numpy as np
import mapbox_earcut as earcut
import triangle as _triangle
from shapely import contains, points as shp_points
from shapely.geometry import Polygon
from shapely.ops import unary_union
from shapely.strtree import STRtree
from scipy.spatial import cKDTree

from sbg.onemap_native.extract import seal_piece, split_piece_components
from sbg.topo.dtm import build_dtm, load_points

PLUNGE_M = 30.0       # how far below pad_z each building skirt reaches (into the slab)
SOLIDIFY_M = 60.0     # terrain slab thickness (> PLUNGE_M so buildings stay embedded)
DEFAULT_STEP = 5.0
# Drape threshold, in metres of DTM ground spread across a CONNECTIVITY GROUP.
# A group whose ground spans more than this is NOT flattened to one level: each
# member sits at its own local ground and the terrain under it follows the real
# DTM. Below the threshold the group keeps one flat pad (which is what keeps
# bridge-linked structures coplanar -- see the min() rationale below).
#
# History, because the rationale changed twice and both versions are wrong now:
#   1. Originally added at COMPOUND level to stop big spiky-hull footprints sinking
#      into a hill.
#   2. Then DISABLED (set to inf) because draping dropped the ground out from under
#      a BOTTOMLESS building mesh, leaving a thin shell the 2m voxel remesh eroded
#      to fragments. That blocker is GONE: seal_piece makes every piece a closed
#      solid, so it no longer depends on the terrain slab for backing.
#   3. Now re-enabled at GROUP level, which is where the flattening actually happens
#      (a group can span several compounds, so a compound-level test could not see
#      the real spread).
#
# There is no threshold that is right for every case, and this is a data limit, not
# a tuning problem -- see TERRAIN.md section 3: two real groups (NUH and the Prince
# George's Park hostels) both measure exactly 20.0m of ground spread and want
# OPPOSITE answers (NUH is genuinely one bridge-linked structure on one level; PGP
# is a string of separate blocks terraced up a hill). Raising this above 20 keeps
# NUH correct and terraces nothing; lowering it below 20 fixes PGP and shears NUH.
# inf restores the old always-flat behaviour.
DRAPE_SPREAD_M = float("inf")
# Pieces whose meshes come within this distance in 3D are one physical structure
# (bridge-linked, shared podium, party wall) and MUST share a pad level -- see
# _connectivity_groups. Empirical: inter-piece gap distribution breaks cleanly at
# p25=0.66m / p50=7.74m on a real NUS/NUH domain -- but note that distribution was
# computed with the OLD (wrong) vertex metric; with real surface distance the
# touching pairs sit at ~0.07m. Kept tight deliberately: a loose eps chains
# unrelated buildings into one group (eps=1.0 -> largest group 29 pieces, vs 24 at
# 0.25). 0.5 leaves headroom for float32 coordinate noise without chaining.
CONNECT_EPS_M = 0.5
# Default lambda for mode="laplacian" (see place_on_terrain_conforming). Measured
# on the Kent Ridge domain: at 10 the bridge-linked NUH complex holds to 0.96m
# adjacent shear (1.81m across the whole complex) while the PGP hillside string
# terraces over 8.69m -- i.e. both cases resolve correctly at the same value,
# which no spread threshold can achieve. Higher -> flatter (100 -> NUH 0.10m /
# PGP 1.32m); lower -> more terracing (3 -> NUH 2.77m / PGP 15.11m).
COUPLING_LAMBDA = 10.0
GROUND_CAP = "ring"   # "ring" | "extent" | "off" -- see the cap block below
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
        # 25th-percentile (not min) of the DTM around the footprint: sits the pad
        # near true grade instead of the deepest griddata-facet crease (see module
        # docstring -- min sinks buildings into a spurious pit).
        pad_z = float(np.percentile(dtm(hull[:, 0], hull[:, 1]), 25))

        v = p["verts"].copy()
        v[:, 2] += pad_z - base_z  # base sits at terrain pad_z
        all_v.append(v)
        all_f.append(p["faces"] + voff)
        voff += len(v)

        nh = len(hull)
        top = np.column_stack([hull, np.full(nh, pad_z + 0.3)])
        bot = np.column_stack([hull, np.full(nh, pad_z - PLUNGE_M)])
        sv = np.vstack([top, bot])
        # wall: one quad (2 tris) per footprint edge -- follows the outline exactly,
        # concave or convex.
        i = np.arange(nh)
        j = (i + 1) % nh
        wall = np.concatenate([np.column_stack([i, j, nh + j]),
                               np.column_stack([i, nh + j, nh + i])])
        # bottom cap: PROPER concave triangulation (earcut ear-clipping), NOT a
        # triangle fan. A fan only triangulates CONVEX polygons; on a concave
        # footprint its triangles bridge the notches and fill the concavity, so the
        # capped base renders as the convex hull even though the footprint is
        # concave (the real "everything's a straight line / convex" bug). earcut
        # respects the concave outline.
        tri = earcut.triangulate_float32(
            np.ascontiguousarray(hull, dtype=np.float32),
            np.array([nh], dtype=np.uint32)).reshape(-1, 3)
        cap = (tri + nh)[:, ::-1]  # index the bottom ring; flip winding to face down
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


def _rasterize_pads(grid_z, affine, footprints):
    """Rasterize each footprint's pad_z into a (nrows, ncols) float grid (NaN where
    no footprint). cv2 scan-line fill when available, else a matplotlib fallback."""
    inv = ~affine  # world (x, y) -> (col, row)
    nrows, ncols = grid_z.shape
    padlevel = np.full((nrows, ncols), np.nan)
    if _cv2 is not None:
        for hull, pad_z in footprints:
            cols, rows = inv * (hull[:, 0], hull[:, 1])
            pts = np.round(np.column_stack([cols, rows])).astype(np.int32)
            _cv2.fillPoly(padlevel, [pts], color=float(pad_z))
        return padlevel

    from matplotlib.path import Path as MplPath
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
        sub = padlevel[r0:r1, c0:c1]
        sub[inside] = pad_z
        padlevel[r0:r1, c0:c1] = sub
    return padlevel


def flatten_pads(grid_z, affine, footprints, apron_m=0.0):
    """Return a copy of grid_z with each footprint flattened to its pad_z.

    apron_m>0 additionally blends pad_z back to real terrain over ~apron_m outside
    each footprint -- OFF BY DEFAULT because it's verified NET-HARMFUL on slopes:
    it blends the natural (higher) hillside DOWN toward the low 25th-percentile
    pad, EXCAVATING a moat around every building (measured up to 2.0m below natural
    grade). Cluster a few small buildings on a steep hill and those moats merge
    into a ravine. A raster heightfield fundamentally can't grade a flat pad into a
    slope without either a one-cell edge step (apron off) or a moat (apron on) --
    the clean fix is breakline/CDT terrain (footprints as triangulation
    constraints, sloping naturally from the flat pad edge to the real terrain
    vertices), i.e. the v1 sbg.topo.conforming_mesh approach, not a raster tweak.
    Kept parameterised for experimentation.
    """
    padlevel = _rasterize_pads(grid_z, affine, footprints)
    padmask = ~np.isnan(padlevel)
    out = grid_z.copy()
    if not padmask.any():
        return out
    out[padmask] = padlevel[padmask]  # footprints exact

    if apron_m and apron_m > 0:
        from scipy.ndimage import distance_transform_edt
        px = abs(affine.a)  # square cell size in metres
        # distance (m) from each non-pad cell to the nearest pad cell, + the index
        # of that nearest pad cell so we blend toward ITS level (handles neighbours).
        dist, (ir, ic) = distance_transform_edt(
            ~padmask, sampling=px, return_indices=True)
        apron = (~padmask) & (dist < apron_m)
        nearest_pad = padlevel[ir[apron], ic[apron]]
        w = np.clip(dist[apron] / apron_m, 0.0, 1.0)  # 0 at pad edge -> 1 at apron_m
        out[apron] = (1.0 - w) * nearest_pad + w * grid_z[apron]
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


# ---------------------------------------------------------------------------
# Conforming-triangulation terrain (replaces the raster flatten_pads +
# terrain_surface_mesh path). Footprints are inserted as constraints in a
# constrained Delaunay triangulation (Shewchuk's `triangle`): the terrain has
# vertices EXACTLY on each footprint edge, a flat pad inside, and clean sloped
# triangles connecting the pad edge straight to the surrounding real-DTM
# vertices -- no coarse raster grid, so none of the raster path's artifacts
# (blocky steps, convex-hull sliver TRENCHES cut among buildings, 20m-contour
# facet creases sampled per cell). Touching footprints are merged into one flat
# COMPOUND pad (a building compound sits on one graded platform, not a
# stair-step of clashing per-building pads -- also what makes the terrain
# between them flat instead of a ravine).
# ---------------------------------------------------------------------------

def _skirt(hull, pad_z):
    """(verts, faces) for a plunge skirt under a footprint ring: a CLOSED prism
    from pad_z+0.3 down to pad_z-PLUNGE_M -- per-edge wall quads plus EARCUT caps
    at BOTH ends (earcut, not a triangle fan: a fan bridges a concave footprint's
    notches and fills space that should stay open -- a real bug this project
    already shipped once).

    The top cap is what makes this a closed solid. It used to be omitted (the
    skirt was an open shell, which the voxel remesh tolerates fine since it only
    resamples inside/outside). But an open shell is illegal input for exact CSG
    and leaves the raw --voxel-size 0 export non-watertight: a real 2km CBD raw
    export had 3,759 components of which 1,917 were OPEN, almost all skirts.
    Closing it costs a handful of triangles, changes nothing for the remesh path
    (a solid resamples the same as a shell that bounds the same volume), and makes
    every component of the raw export a genuine closed solid."""
    nh = len(hull)
    top = np.column_stack([hull, np.full(nh, pad_z + 0.3)])
    bot = np.column_stack([hull, np.full(nh, pad_z - PLUNGE_M)])
    sv = np.vstack([top, bot])
    i = np.arange(nh)
    j = (i + 1) % nh
    wall = np.concatenate([np.column_stack([i, j, nh + j]),
                           np.column_stack([i, nh + j, nh + i])])
    tri = earcut.triangulate_float32(
        np.ascontiguousarray(hull, dtype=np.float32),
        np.array([nh], dtype=np.uint32)).reshape(-1, 3)
    bot_cap = (tri + nh)[:, ::-1]
    top_cap = tri
    return sv, np.concatenate([wall, bot_cap, top_cap])


def _piece_rings(piece):
    """The footprint of a piece as a list of (M,2) rings. Accepts the new
    clustered format (a list of rings) or the old single-ring format (one array),
    so precomputed stores still work."""
    h = piece.get("footprint")
    if h is None:
        return []
    if isinstance(h, list):
        return [np.asarray(r) for r in h if r is not None and len(r) >= 3]
    return [np.asarray(h)] if len(h) >= 3 else []


def _piece_polygons(pieces):
    """One valid shapely geometry per piece footprint (union of its cluster rings),
    or None where unusable."""
    out = []
    for p in pieces:
        polys = []
        for r in _piece_rings(p):
            poly = Polygon(r)
            if not poly.is_valid:
                poly = poly.buffer(0)
            if poly.geom_type == "Polygon" and not poly.is_empty and poly.area > 0:
                polys.append(poly)
        if not polys:
            out.append(None)
        elif len(polys) == 1:
            out.append(polys[0])
        else:
            out.append(unary_union(polys))
    return out


def _surface_gap(va, fa, vb, fb, eps, max_work=4_000_000, bba=None, bbb=None):
    """Minimum SURFACE-to-surface distance between two meshes.

    Vertex-to-vertex distance is NOT usable here and using it was a real bug:
    these OneMap meshes average 7-12m per triangle edge (max 98m), so two
    surfaces flush against each other can have their nearest vertices metres
    apart. Measured on the real NUH pair the user reported: vertex gap 5.735m,
    true surface gap 0.066m -- an 87x overestimate that left two visibly
    bridge-linked hospital blocks 20m apart in the output.

    Exact point-to-triangle, no sampling. Sampling the contact patch was tried
    first and measured at 134.6s for one domain -- 160x slower than the entire
    placement step -- because it built Trimesh objects and ran sample_surface per
    candidate pair. This version restricts to the CONTACT REGION (faces whose
    bbox lies in the overlap of the two eps-expanded bounding boxes) and then
    does a vectorized point-to-triangle distance, which is both exact and cheap
    because contact regions are tens of faces, not whole buildings.
    """
    lo = np.maximum(va.min(0), vb.min(0)) - eps
    hi = np.minimum(va.max(0), vb.max(0)) + eps
    if np.any(lo > hi):
        return np.inf

    def patch(v, f, bb):
        # bb = (per-face min, per-face max), precomputed ONCE per piece by
        # _connectivity_groups -- a piece appears in many candidate pairs and
        # recomputing v[f] and its bounds for each was a real cost.
        fmin, fmax = bb if bb is not None else (v[f].min(axis=1), v[f].max(axis=1))
        keep = np.all(fmax >= lo, axis=1) & np.all(fmin <= hi, axis=1)
        return v[f][keep] if keep.any() else None

    ta, tb = patch(va, fa, bba), patch(vb, fb, bbb)
    if ta is None or tb is None:
        return np.inf

    def pt_tri_min(tris, pts):
        """Min distance from any point to any triangle (vectorized, exact)."""
        if len(tris) * len(pts) > max_work:  # guard a pathological pair
            step = max(1, int(len(pts) * len(tris) / max_work))
            pts = pts[::step]
        a, b, c = tris[:, 0][:, None], tris[:, 1][:, None], tris[:, 2][:, None]
        p = pts[None, :]
        ab, ac, ap = b - a, c - a, p - a
        d1 = (ab * ap).sum(-1); d2 = (ac * ap).sum(-1)
        bp = p - b
        d3 = (ab * bp).sum(-1); d4 = (ac * bp).sum(-1)
        cp = p - c
        d5 = (ab * cp).sum(-1); d6 = (ac * cp).sum(-1)
        va_ = d3 * d6 - d5 * d4
        vb_ = d5 * d2 - d1 * d6
        vc_ = d1 * d4 - d3 * d2
        denom = np.where((va_ + vb_ + vc_) == 0, 1.0, va_ + vb_ + vc_)
        v = vb_ / denom
        w = vc_ / denom
        # clamp barycentric coords into the triangle (covers edge/vertex regions)
        v = np.clip(v, 0, 1); w = np.clip(w, 0, 1)
        over = v + w > 1
        s = np.where(over, v + w, 1.0)
        v = np.where(over, v / s, v); w = np.where(over, w / s, w)
        closest = a + v[..., None] * ab + w[..., None] * ac
        return float(np.linalg.norm(closest - p, axis=-1).min())

    pa = np.unique(ta.reshape(-1, 3), axis=0)
    pb = np.unique(tb.reshape(-1, 3), axis=0)
    return min(pt_tri_min(ta, pb), pt_tri_min(tb, pa))


def _connectivity_groups(pieces, keep_idx, eps=None):
    """Group pieces that are PHYSICALLY CONNECTED in 3D (min vertex-to-vertex
    distance <= eps) so they can be given one shared pad level.

    Why this is needed, and why the 2D footprint union isn't enough: every piece
    arrives from OneMap with base_z == 0 -- SLA models all building bases on one
    common datum -- so in the source data connected structures are exactly
    coplanar. Any per-piece pad_z shift is therefore the ONLY thing that can
    break a real connection. Measured on a real NUS/NUH domain: pieces whose
    meshes are within 0.25m of each other were being shifted up to 19.8m apart,
    shearing the bridge that physically joins them (user-caught, visible in a
    render).

    The existing compound mechanism (unary_union of footprints) misses exactly
    this case because it is 2D and ground-level: a bridge spans a gap, so the two
    footprints never touch, and the bridge's own ground contact is either absent
    or a cluster below _MIN_RING_AREA. The connection is real but invisible in
    plan view -- so test proximity on the meshes, in 3D.

    eps is empirically supported rather than guessed: the inter-piece gap
    distribution on that domain has a clean break (p25=0.66m, p50=7.74m).

    Returns a label array over keep_idx (same order), one int per kept piece.
    """
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components

    n = len(keep_idx)
    edges = _connectivity_edges(pieces, keep_idx, eps)
    if not edges:
        return np.arange(n, dtype=np.int64)
    rows = [a for a, _ in edges]
    cols = [b for _, b in edges]
    m = coo_matrix((np.ones(len(rows)), (rows, cols)), shape=(n, n))
    _, labels = connected_components(m, directed=False)
    return labels


def _connectivity_edges(pieces, keep_idx, eps=None):
    """The adjacency EDGES behind _connectivity_groups, as (a, b) index pairs into
    keep_idx. Kept separate because the soft-coupling ("laplacian") placement mode
    needs the graph itself, not just its connected components -- a chain and a
    compact blob have the same component labels but behave completely differently
    under Laplacian smoothing, which is the whole point of that mode."""
    # read the module constant at CALL time, not as a default-argument value --
    # binding it in the signature silently freezes it at import, which made an
    # eps sweep return byte-identical results for every eps (real bug, caught).
    if eps is None:
        eps = CONNECT_EPS_M
    n = len(keep_idx)
    if n < 2:
        return []
    verts = [pieces[i]["verts"] for i in keep_idx]
    faces = [pieces[i]["faces"] for i in keep_idx]
    bounds = [(v.min(0), v.max(0)) for v in verts]
    # candidate pairs via an STRtree over XY bboxes -- an O(n^2) Python double loop
    # is fine at ~100 pieces but not at the thousands a real domain has.
    from shapely.geometry import box as _box
    boxes = [_box(lo[0] - eps, lo[1] - eps, hi[0] + eps, hi[1] + eps)
             for lo, hi in bounds]
    btree = STRtree(boxes)
    cand = set()
    for a in range(n):
        for b in np.atleast_1d(btree.query(boxes[a])):
            b = int(b)
            if b > a:
                cand.add((a, b))
    cand = sorted(cand)

    # Precompute per-piece face bboxes and vertex KD-trees ONCE (each piece takes
    # part in many pairs).
    fbb = [(v[f].min(axis=1), v[f].max(axis=1)) for v, f in zip(verts, faces)]
    vtrees = [cKDTree(v) for v in verts]

    def _connected(ab):
        a, b = ab
        # Cheap ACCEPT: vertex-vertex distance is an UPPER bound on surface
        # distance, so a hit here means connected without the exact test.
        # (The converse is NOT true -- vertex distance overestimates badly on
        # these 7-12m triangles, which is the bug this whole function was
        # rewritten to fix -- so a miss must still fall through.)
        d, _ = vtrees[a].query(verts[b], k=1, distance_upper_bound=eps)
        if np.isfinite(d).any():
            return True
        return _surface_gap(verts[a], faces[a], verts[b], faces[b], eps,
                            bba=fbb[a], bbb=fbb[b]) <= eps

    # 4 threads measured at 2.0x; 8 and 12 REGRESS (1.5x, 1.4x) on GIL contention.
    if len(cand) > 64:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as ex:
            hits = list(ex.map(_connected, cand))
    else:
        hits = [_connected(ab) for ab in cand]
    return [ab for ab, ok in zip(cand, hits) if ok]


def _densify_ring(coords, max_len):
    """Subdivide a ring's edges so no segment exceeds max_len -- real fix for a
    real bug (found from a render, confirmed numerically: along one domain edge
    spanning a 77m/20m corner-height difference, the true DTM profile deviated
    up to 36m from a straight corner-to-corner line). `triangle`'s 'Y' flag
    forbids adding Steiner points ON constrained boundary segments, so if the
    domain polygon's own ring is fed in as-is (just its corners, for a simple
    bbox), the triangulated terrain's boundary is FORCED to linearly ramp
    between corners, ignoring the real elevation profile in between -- visible
    as a diagonal "ramp" wall instead of one that hangs off the true, varying
    boundary height. Densifying supplies the extra points ourselves so the
    boundary can actually track DTM."""
    coords = list(coords)
    pts = coords[:-1] if coords[0] == coords[-1] else coords
    pts = np.asarray(pts, dtype=float)
    out = []
    n = len(pts)
    for i in range(n):
        p0, p1 = pts[i], pts[(i + 1) % n]
        seg_len = float(np.linalg.norm(p1 - p0))
        nseg = max(1, int(np.ceil(seg_len / max_len)))
        for k in range(nseg):
            out.append(tuple(p0 + (k / nseg) * (p1 - p0)))
    return out


def conforming_terrain(domain_polygon, dtm, comp_polys, comp_padz,
                       terrain_step=10.0, collar=None):
    """Constrained-Delaunay terrain (verts (V,3), faces (F,3)).

    comp_polys/comp_padz: building COMPOUND polygons and their flat pad z. Each
    compound ring is a triangulation constraint whose vertices are pinned to
    pad_z (a flat pad); domain-boundary + background grid vertices take the real
    DTM elevation; the triangles between slope cleanly. A background grid gives
    terrain shape away from buildings; grid points within `collar` of a compound
    are dropped so the pad edge connects straight to real terrain.
    """
    if collar is None:
        collar = max(terrain_step / 2, 1.0)
    union = unary_union(comp_polys) if comp_polys else None

    xmin, ymin, xmax, ymax = domain_polygon.bounds
    gx, gy = np.meshgrid(np.arange(xmin, xmax + terrain_step, terrain_step),
                         np.arange(ymin, ymax + terrain_step, terrain_step))
    pts = np.column_stack([gx.ravel(), gy.ravel()])
    geoms = shp_points(pts[:, 0], pts[:, 1])
    keep = contains(domain_polygon, geoms)
    if union is not None and not union.is_empty:
        keep &= ~contains(union.buffer(collar), geoms)
    grid = pts[keep]

    verts, zs, vidx = [], [], {}

    def add_v(x, y, z):
        k = (round(x, 3), round(y, 3))
        i = vidx.get(k)
        if i is None:
            i = len(verts)
            vidx[k] = i
            verts.append((x, y))
            zs.append(z)
        return i

    segs = []

    def add_ring(coords, pad):
        ring = coords[:-1] if coords[0] == coords[-1] else coords
        ids = [add_v(x, y, pad if pad is not None else float(dtm(x, y)))
               for x, y in ring]
        n = len(ids)
        for i in range(n):
            segs.append((ids[i], ids[(i + 1) % n]))

    # Domain boundary densified to terrain_step (see _densify_ring) so it can
    # track the real DTM profile between corners, not linearly ramp between
    # them. Compound (building pad) rings are NOT densified -- every point on
    # one gets the SAME fixed pad_z regardless, so subdividing them would add
    # triangulation cost for zero benefit.
    add_ring(_densify_ring(domain_polygon.exterior.coords, terrain_step), None)
    for interior in domain_polygon.interiors:
        add_ring(_densify_ring(interior.coords, terrain_step), None)
    for part, pad in zip(comp_polys, comp_padz):
        add_ring(list(part.exterior.coords), pad)
        for interior in part.interiors:
            add_ring(list(interior.coords), pad)
    for x, y in grid:
        add_v(float(x), float(y), float(dtm(x, y)))

    verts = np.array(verts, dtype=float)
    zs = np.array(zs, dtype=float)
    # triangle misbehaves at large absolute EPSG:3414 coords (~30000m) -- shift to
    # a local origin, triangulate, shift back (same fix as v1 conforming_mesh).
    origin = verts.min(axis=0)
    d = {"vertices": verts - origin, "segments": np.array(segs, dtype=int)}
    out = _triangle.triangulate(d, "pY")  # p=respect segments, Y=no boundary Steiner
    if "triangles" not in out:
        raise RuntimeError("triangle.triangulate produced no triangles for terrain PSLG")
    tv = out["vertices"] + origin
    tt = out["triangles"]
    if len(tv) > len(verts):  # any interior Steiner points -> real DTM elevation
        ez = np.atleast_1d(dtm(tv[len(verts):, 0], tv[len(verts):, 1]))
        allz = np.concatenate([zs, ez])
    else:
        allz = zs
    return np.column_stack([tv, allz]), tt.astype(np.int64)


def terrain_flat_base_solid(terr_v, terr_f, domain_polygon, base_z=0.0, terrain_step=10.0):
    """Extrude the conforming terrain surface down to a FLAT plane at base_z --
    NOT a fixed-thickness offset (Blender's SOLIDIFY modifier only supports a
    uniform per-vertex thickness, which can't express "extrude to an absolute Z
    plane"). A terrain point at elevation 30m gets a 30m column; a point at 5m
    gets a 5m column; the bottom is one flat, fully-filled cap at base_z.

    Relies on conforming_terrain's 'pY' triangulation ('Y' = no Steiner points on
    constrained boundary segments), which guarantees every domain_polygon ring
    coordinate is EXACTLY a vertex already present in terr_v -- found here via a
    KDTree nearest-neighbour match so the new wall faces attach to the real top
    mesh boundary, not a disconnected duplicate seam.

    Caller's responsibility: a building's plunge skirt reaches PLUNGE_M below its
    pad_z, which can go below base_z on low-lying ground (measured: -10m on a
    real domain) -- if base_z is fixed at 0 regardless, those skirts poke
    through this solid's own sealed bottom cap (a real, visible hole; confirmed
    both numerically and in a raw render). Callers should derive base_z from
    the actual building geometry's own minimum z (with a small margin), not
    hardcode 0 -- see build.py's `build_domain_stl` for the real derivation.

    terrain_step MUST match the value conforming_terrain was called with (both
    default 10.0). The wall is built from the SAME densified domain-boundary
    ring conforming_terrain used (see _densify_ring) -- using the raw
    domain_polygon corners instead (as an earlier version of this function did)
    made the wall a straight diagonal ramp between corners, ignoring the real
    DTM profile between them (a real bug, found from a render: up to 36m off
    along one edge on a real domain -- confirmed numerically before fixing).
    """
    from scipy.spatial import cKDTree
    tree = cKDTree(terr_v[:, :2])

    rings = [(np.asarray(_densify_ring(domain_polygon.exterior.coords, terrain_step)), False)]
    for interior in domain_polygon.interiors:
        rings.append((np.asarray(_densify_ring(interior.coords, terrain_step)), True))

    verts_out = [terr_v]
    faces_out = [terr_f]
    voff = len(terr_v)
    bot_idx_all, cap_pts, ring_lens = [], [], []

    for pts, is_hole in rings:
        dist, top_idx = tree.query(pts)
        bad = dist > 0.5  # sanity: should be near-exact matches
        if np.any(bad):
            raise RuntimeError(
                f"{int(np.count_nonzero(bad))} domain-boundary points didn't match a "
                f"terrain vertex (max {dist.max():.2f}m) -- conforming_terrain's "
                f"no-Steiner-on-boundary assumption broke")
        n = len(pts)
        bot_idx = voff + np.arange(n)
        verts_out.append(np.column_stack([pts, np.full(n, base_z)]))
        voff += n

        i = np.arange(n)
        j = (i + 1) % n
        wall = np.concatenate([
            np.column_stack([top_idx[i], top_idx[j], bot_idx[j]]),
            np.column_stack([top_idx[i], bot_idx[j], bot_idx[i]]),
        ])
        if is_hole:  # interior ring -> opposite winding for an outward-facing wall
            wall = wall[:, ::-1]
        faces_out.append(wall)

        bot_idx_all.append(bot_idx)
        cap_pts.append(pts)
        ring_lens.append(n)

    # Flat bottom cap: earcut handles exterior+holes directly in one call (ring
    # order: exterior first, then holes, matching domain_polygon's own order).
    cap_all = np.concatenate(cap_pts).astype(np.float32)
    ring_ends = np.cumsum(ring_lens).astype(np.uint32)
    tri_local = earcut.triangulate_float32(
        np.ascontiguousarray(cap_all), ring_ends).reshape(-1, 3)
    bot_concat = np.concatenate(bot_idx_all)
    cap = bot_concat[tri_local][:, ::-1]  # flip winding to face downward
    faces_out.append(cap)

    # NOTE: do NOT "repair" this mesh with fillHole. meshlib's meshFromFacesVerts
    # reports holes=4 on this output, but that is an artifact of how it splits
    # vertices at winding seams -- a STRICT trimesh weld shows the solid is already
    # closed and fully manifold (every edge used exactly twice, watertight=True).
    # Running fillHole on it was tried and made things strictly WORSE: 91,856 ->
    # 95,050 faces and 2,292 non-manifold (4-face) edges, i.e. it fabricated
    # geometry over seams that were never open. Verified by A/B on a real 2km CBD
    # domain. Trust a strict re-weld over meshlib's hole count for this mesh.
    return np.concatenate(verts_out), np.concatenate(faces_out).astype(np.int64)


def place_on_terrain_conforming(pieces, dtm, domain_polygon, terrain_step=10.0,
                                collar=None, seal=True, skirt=True,
                                mode="drape", coupling_lambda=None):
    """Place building meshes on a CONFORMING terrain and return
    (bld_v, bld_f, terr_v, terr_f) -- the drop-in replacement for
    place_on_terrain + flatten_pads + terrain_surface_mesh.

    Touching footprints are merged into compounds; every building in a compound
    is shifted to that compound's single flat pad_z (25th-percentile DTM over the
    compound, robust to griddata facet creases) and gets a plunge skirt. The
    terrain is then triangulated with the compound rings as flat-pad constraints.

    `mode` decides how a physically-connected structure spanning real relief is
    handled. There is no universally right answer -- see TERRAIN.md section 3:
    two real groups (NUH, and the Prince George's Park hostels) measure exactly
    the same 20.0m ground spread and want OPPOSITE treatment, so this is a
    genuine choice, not a default waiting to be tuned.

      "group"     3D-connected pieces share ONE flat level (min over
                  the group). Bridges/podium links stay coplanar the way the
                  source data had them; a chain of separate blocks up a hill gets
                  carved into the hillside to hold that one level.
      "drape"     (DEFAULT) no 3D grouping at all -- every piece sits on its own
                  own ground. Hillside strings terrace correctly; anything joined
                  by a bridge shears (measured: 19.8m on the real NUH pair).
                  This is what the pipeline did before connectivity grouping.
      "laplacian" soft coupling: minimise
                      S (z_i - ground_i)^2  +  lambda * S_(i,j) in E (z_i - z_j)^2
                  over the connectivity graph. lambda -> inf reproduces "group",
                  lambda = 0 reproduces "drape". In between, a compact densely
                  linked complex pulls itself flat while a long chain ramps
                  gently -- the two cases resolve differently WITHOUT having to
                  classify them, which no single threshold can do. Measured on
                  the Kent Ridge domain at lambda=10: NUH holds to 0.96m adjacent
                  shear (1.81m across the whole complex) while the PGP string
                  terraces over 8.69m.

    Note the solve is over COMPOUNDS, not pieces: a compound is one triangulation
    constraint ring with one pad z, so pieces whose footprints touch in plan
    physically cannot take different levels. Contracting them is therefore a hard
    constraint, not an approximation.

    The Laplacian solve is plain least squares, so a group settles near its MEAN
    ground and some members float above local grade (measured max +6.5m at
    lambda=10) where "group" mode's min() never floats. Anchoring each group down
    so nothing floats was measured and is NOT free -- it rigidly shifts the whole
    group, putting max sink back to -18.9m. Left unanchored deliberately: the
    error is smaller and split both ways instead of all in cut.
    """
    if coupling_lambda is None:
        coupling_lambda = COUPLING_LAMBDA
    if mode not in ("group", "drape", "laplacian"):
        raise ValueError(f"unknown placement mode {mode!r} "
                         "(expected 'group', 'drape' or 'laplacian')")
    if collar is None:
        collar = max(terrain_step / 2, 1.0)
    if seal:
        # Seal + split each _BATCHID into physically separate parts BEFORE any
        # placement decision -- a batch id is not a building, a connected
        # component is. See extract.split_piece_components. The returned pieces
        # are already sealed, so the per-piece seal below becomes a no-op.
        split = []
        for p in pieces:
            split.extend(split_piece_components(p))
        pieces, seal = split, False
    piece_polys = _piece_polygons(pieces)
    valid = [p for p in piece_polys if p is not None]
    if not valid:  # no buildings -> plain conforming terrain (just the DTM)
        tv, tf = conforming_terrain(domain_polygon, dtm, [], [], terrain_step, collar)
        return np.empty((0, 3)), np.empty((0, 3), dtype=np.int64), tv, tf

    union = unary_union(valid)
    comp_polys = list(union.geoms) if union.geom_type == "MultiPolygon" else [union]
    # per-compound pad level -- but a compound whose ground spans a lot of elevation
    # (a big OneMap complex / spiky-hull merge straddling a hillside) must NOT be
    # flattened to one level, or its uphill half sinks into the hill. pad_z=None
    # flags such compounds for DRAPING (terrain follows the real DTM through them).
    # (the drape decision itself is made per CONNECTIVITY GROUP below, not here --
    # a group can span several compounds, so a compound-level spread test cannot
    # see the real elevation range the flattening is actually applied over.)
    comp_padz = []
    for part in comp_polys:
        gz = dtm(*np.asarray(part.exterior.coords).T[:2])
        comp_padz.append(float(np.percentile(gz, 25)))
    tree = STRtree(comp_polys)

    keep_idx = [i for i, poly in enumerate(piece_polys) if poly is not None]
    # Which 2D compound(s) each piece sits in, and its own preferred level. A
    # piece with several disjoint footprint rings can legitimately touch more
    # than one compound, so this is a list, not a single index.
    comps_of, own_pad, comp_members = {}, {}, {c: [] for c in range(len(comp_polys))}
    for i in keep_idx:
        poly = piece_polys[i]
        cs = [int(j) for j in np.atleast_1d(tree.query(poly))
              if comp_polys[int(j)].intersects(poly)]
        comps_of[i] = cs
        for c in cs:
            comp_members[c].append(i)
        # NOTE: the compound whose interior holds the piece's representative point,
        # NOT min() over every compound it touches. A multi-ring piece can straddle
        # compounds and min() would be defensible, but it is a silent behaviour
        # change to the default mode (measured: shifts real geometry and re-exposed
        # a latent clip-wall artifact), so the original semantics are kept.
        own_pad[i] = float(dtm(poly.centroid.x, poly.centroid.y))
        for j in np.atleast_1d(tree.query(poly.representative_point())):
            if comp_polys[int(j)].intersects(poly):
                own_pad[i] = comp_padz[int(j)]
                break

    # GROUND-CLEARANCE CAP -- a building must never sit ABOVE the ground beneath
    # it. Without this, any level above local grade leaves the building
    # overhanging air, with only the flat pad poking up to meet it: user-reported
    # from a render as "floating buildings with pyramids under them", and that
    # pyramid IS the pad, because the pad ring is the tight foundation cluster
    # (measured: median 7% of the building's base bbox, p10 1%) while the
    # building above it is much wider.
    #
    # Sampled over the piece's OWN VERTEX XYs, i.e. its whole real extent, not
    # just the pad ring -- for 7 of 94 pieces on the Kent Ridge domain the ring
    # sits >1m above ground the building overhangs (max 7.04m), so a ring-only
    # cap would not have caught them. Subsampled to ~2000 points per piece; these
    # are 7-12m triangles, so a denser sample buys nothing.
    #
    # It is close to free: it only ever lowers a piece that would otherwise
    # float, so the median extra cut over the domain is 0.00-0.01m.
    cap = {}
    for i in keep_idx:
        if GROUND_CAP == "off":
            cap[i] = np.inf
            continue
        if GROUND_CAP == "extent":
            v = pieces[i]["verts"]
            xy = v[::max(1, len(v) // 2000), :2]
        else:  # "ring" -- the piece's own footprint ring only
            q = piece_polys[i]
            parts = list(q.geoms) if q.geom_type == "MultiPolygon" else [q]
            xy = np.concatenate([np.asarray(r.exterior.coords) for r in parts])
        cap[i] = float(np.min(np.atleast_1d(dtm(xy[:, 0], xy[:, 1]))))

    # Contract compounds that a single multi-ring piece straddles. That piece is
    # rigid -- one level -- so those compounds cannot hold different pad z or it
    # can only ever be flush over one of them (53 of 94 pieces here span >1
    # compound, one spans 10). "group" mode hid this by forcing every compound a
    # connectivity group touches to one level; drape had no such reconciliation
    # at all, which is the second half of the reported floating.
    parent = list(range(len(comp_polys)))

    def _find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def _union(a, b):
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[rb] = ra

    for i in keep_idx:
        for c in comps_of[i][1:]:
            _union(comps_of[i][0], c)
    roots = sorted({_find(c) for c in range(len(comp_polys))})
    nidx = {r: k for k, r in enumerate(roots)}
    node_of_comp = {c: nidx[_find(c)] for c in range(len(comp_polys))}
    node_of_piece = {i: node_of_comp[comps_of[i][0]] for i in keep_idx if comps_of[i]}
    n_nodes = len(roots)
    node_pieces = {k: [] for k in range(n_nodes)}
    for i, k in node_of_piece.items():
        node_pieces[k].append(i)
    # each node's preferred level, already capped so it can never float
    node_target = np.full(n_nodes, np.inf)
    for c, k in node_of_comp.items():
        node_target[k] = min(node_target[k], comp_padz[c])
    node_cap = np.full(n_nodes, np.inf)
    for k, ms in node_pieces.items():
        for i in ms:
            node_cap[k] = min(node_cap[k], cap[i], own_pad[i])
    node_cap = np.where(np.isfinite(node_cap), node_cap, node_target)
    node_target = np.minimum(node_target, node_cap)

    draped_nodes = set()
    if mode == "drape":
        # No 3D grouping at all: every piece takes its own (capped) ground level.
        z_node = node_target.copy()
    elif mode == "group":
        # 3D-connectivity groups: physically-joined pieces (bridges, shared
        # podiums) share ONE pad level or the link shears -- the 2D compound
        # union cannot see an elevated connection. See _connectivity_groups.
        labels = _connectivity_groups(pieces, keep_idx)
        group_of = {i: int(l) for i, l in zip(keep_idx, labels)}
        # min() across the group -- measured tradeoff, not a guess. A connected
        # structure sits at one level while the ground moves under it, so whatever
        # we pick, members whose own ground differs get an error equal to the
        # group's ground spread. min() puts that error entirely into "cut" (uphill
        # members sit below their local grade, and conforming_terrain grades their
        # pad down to meet them); max() would put it into "fill", leaving buildings
        # floating with air flowing underneath, the worse artifact for CFD.
        # Measured on a real NUS/NUH domain (94 pieces, 53 groups): min -> 23
        # pieces >2m below local grade and 0 floating; max -> 9 below but 32
        # floating; median -> 18/5.
        # NOTE the residual is dominated by ONE group spanning 20.00m of DTM
        # ground (median group spread is 7.20m), and 20.00m is exactly the
        # Contour_250K interval -- largely a terrain-data artifact, not relief.
        #
        # ...unless the group's own ground spans more than DRAPE_SPREAD_M, in
        # which case forcing one level is the WORSE error. Such a group is DRAPED
        # (group_pad=None -> members sit at their own ground, terrain follows the
        # DTM). Default is inf: swept on the Kent Ridge domain and NO threshold
        # works, because NUH and the PGP string both measure exactly 20.0m spread
        # and want opposite answers -- use mode="laplacian" for that, not a
        # threshold. Kept as the lambda=0 end of the same spectrum.
        group_ground = {}
        for i in keep_idx:
            p = piece_polys[i]
            parts = list(p.geoms) if p.geom_type == "MultiPolygon" else [p]
            gz = np.concatenate([np.atleast_1d(dtm(*np.asarray(q.exterior.coords).T[:2]))
                                 for q in parts])
            group_ground.setdefault(group_of[i], []).append(gz)
        draped = {g for g, zs in group_ground.items()
                  if float(np.ptp(np.concatenate(zs))) > DRAPE_SPREAD_M}
        # One level per group, taken over NODES so the ground-clearance cap and the
        # multi-compound contraction both apply: a rigid downward shift, so the
        # group stays coplanar (the whole point of this mode) and no member floats.
        z_node = node_target.copy()
        group_level = {}
        for i in keep_idx:
            k = node_of_piece.get(i)
            if k is None:
                continue
            g = group_of[i]
            group_level[g] = min(group_level.get(g, np.inf), node_target[k])
        for i in keep_idx:
            k = node_of_piece.get(i)
            if k is not None and group_of[i] not in draped:
                z_node[k] = min(z_node[k], group_level[group_of[i]])
        draped_nodes = {node_of_piece[i] for i in keep_idx
                        if i in node_of_piece and group_of[i] in draped}
    else:  # "laplacian" -- soft coupling, solved over the contracted NODES
        from scipy.sparse import coo_matrix, identity
        from scipy.sparse.linalg import spsolve

        g = node_target
        # smoothness term: the real 3D adjacency, lifted from pieces to nodes
        edges = _connectivity_edges(pieces, keep_idx)
        pairs = set()
        for a, b in edges:
            pa = node_of_piece.get(keep_idx[a])
            pb = node_of_piece.get(keep_idx[b])
            if pa is not None and pb is not None and pa != pb:
                pairs.add((min(pa, pb), max(pa, pb)))
        if pairs and coupling_lambda > 0:
            r = np.array([p[0] for p in pairs] + [p[1] for p in pairs])
            c_ = np.array([p[1] for p in pairs] + [p[0] for p in pairs])
            A = coo_matrix((np.ones(len(r)), (r, c_)), shape=(n_nodes, n_nodes))
            L = coo_matrix((np.asarray(A.sum(1)).ravel(),
                            (np.arange(n_nodes), np.arange(n_nodes))),
                           shape=(n_nodes, n_nodes)) - A
            M = (identity(n_nodes) + coupling_lambda * L).tocsr()
            z = np.asarray(spsolve(M.tocsc(), g)).ravel()
            # ACTIVE SET: smoothing pulls a node toward its neighbours, which can
            # lift it back above its ground-clearance cap -- exactly the floating
            # the user caught in a render. Pin every violator AT its cap and
            # re-solve the rest against it, repeat. This beats a plain post-hoc
            # clamp (which would shear a pinned node away from its neighbours)
            # and a rigid per-group shift (measured: preserves the ramp but drags
            # the whole group down, putting max cut back to ~-19m).
            fixed = np.zeros(n_nodes, dtype=bool)
            for _ in range(8):
                viol = (z > node_cap + 1e-9) & ~fixed
                if not viol.any():
                    break
                fixed |= viol
                z[fixed] = node_cap[fixed]
                free = ~fixed
                if not free.any():
                    break
                rhs = g[free] - M[free][:, fixed] @ z[fixed]
                z[free] = np.asarray(spsolve(M[free][:, free].tocsc(), rhs)).ravel()
            z = np.minimum(z, node_cap)
        else:
            z = g
        z_node = z

    # One place where node levels become piece levels and terrain pad levels, for
    # every mode -- so a building and the pad under it can never disagree.
    pad_of = {i: (float(z_node[node_of_piece[i]]) if i in node_of_piece
                  else own_pad[i]) for i in keep_idx}
    for c in range(len(comp_polys)):
        k = node_of_comp[c]
        comp_padz[c] = None if k in draped_nodes else float(z_node[k])

    all_v, all_f = [], []
    voff = 0
    for i, (p, poly) in enumerate(zip(pieces, piece_polys)):
        if poly is None:
            continue
        # level decided by `mode` above (see its docstring) -- for the default
        # "group" mode this is one level per 3D-connected group, never per piece,
        # which is what keeps a bridge-linked pair coplanar the way the source
        # data had them.
        pad_z = pad_of[i]
        pv, pf = (seal_piece(p["verts"], p["faces"]) if seal
                  else (p["verts"], p["faces"]))
        v = np.asarray(pv).copy()
        v[:, 2] += pad_z - p["base_z"]
        all_v.append(v)
        all_f.append(np.asarray(pf) + voff)
        voff += len(v)
        # One tight skirt per foundation cluster (not one giant hull spanning gaps).
        # skirt=False is for the NO-REMESH (--voxel-size 0) export: the skirt exists
        # purely to guarantee VOLUME OVERLAP into the slab so the voxel remesh fuses
        # building and terrain. With no remesh there is nothing to fuse, and the
        # building already sits exactly on its pad by construction (base at pad_z,
        # pad flattened to pad_z), so the skirt only adds overlapping geometry.
        for r in (_piece_rings(p) if skirt else []):
            sv, sf = _skirt(r.astype(np.float32), pad_z)
            all_v.append(sv)
            all_f.append(sf + voff)
            voff += len(sv)

    bld_v = np.concatenate(all_v)
    bld_f = np.concatenate(all_f)
    terr_v, terr_f = conforming_terrain(domain_polygon, dtm, comp_polys, comp_padz,
                                        terrain_step, collar)
    return bld_v, bld_f, terr_v, terr_f
