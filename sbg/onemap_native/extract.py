"""Whole-tile mesh extraction for a domain -> per-building pieces in EPSG:3414.

Decodes every Draco/_BATCHID primitive in each leaf tile, applies the correct
3D-Tiles transform (transform.local_to_svy21), splits into per-_BATCHID pieces
(so callers can place each building individually -- footprint, base elevation,
skirts), drops flat ground plates, and clips to the domain by piece centroid.

Deliberately NO shift-search, NO cluster contamination filter, NO height
self-validation: the whole-tile approach never isolates a single building from
a wrong batch id, so the entire class of bugs those band-aids existed for
cannot occur here (see the project plan's tile-native investigation). _BATCHID
is used only to group faces into buildings for placement, never to pick "the"
building's geometry out of a combined mesh.
"""
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import DracoPy
from scipy.spatial import ConvexHull, cKDTree
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from shapely import contains_xy
from shapely.geometry import Polygon

from sbg.onemap_native.tiles import feature_table, fetch_tile, load_gltf
from sbg.onemap_native.transform import local_to_svy21, node_transform

FLAT_PLATE_Z = 0.5  # per-batch-id z-extent below this is a ground plate/decal, skip
# Tiles are read+Draco-decoded in parallel. Draco's C decoder and the tile I/O
# both release the GIL, so threads scale well; kept modest to bound peak memory
# (this project has OOM'd at high worker counts decoding many big tiles at once).
DEFAULT_WORKERS = 6


def _draco_prims(gltf, blob):
    """Yield (points, faces, batch_ids, T, R) for every Draco/_BATCHID primitive,
    grouped by the mesh's own node transform."""
    node_for_mesh = {n.mesh: n for n in (gltf.nodes or []) if n.mesh is not None}
    for mi, mesh in enumerate(gltf.meshes):
        T, R = node_transform(node_for_mesh.get(mi))
        for prim in mesh.primitives:
            ext = (prim.extensions or {}).get("KHR_draco_mesh_compression")
            if ext is None or "_BATCHID" not in ext.get("attributes", {}):
                continue
            bv = gltf.bufferViews[ext["bufferView"]]
            start = bv.byteOffset or 0
            dec = DracoPy.decode(blob[start:start + bv.byteLength])
            pts = np.asarray(dec.points)
            faces = np.asarray(dec.faces)
            if pts.size == 0 or faces.size == 0:
                continue
            bid = np.asarray(
                dec.get_attribute_by_unique_id(ext["attributes"]["_BATCHID"])["data"]
            ).reshape(-1).astype(np.int64)
            yield pts, faces, bid, T, R


# Base points within this are ONE foundation cluster. This separates genuinely
# distant foundations (the case below, 50-150m apart); it must NOT be small
# enough to split one building along its own width. At 8.0 it did exactly that:
# a 25x9.6m shophouse has its front- and back-wall base vertices 9.6m apart, so
# each wall became its own cluster, each wall is a COLLINEAR line of points, and
# a collinear hull is zero-area/QhullError -> every ring discarded -> footprint
# None -> place_on_terrain_conforming skips the piece -> the building silently
# vanishes from the STL. Measured over a 400x400m Duxton/Tanjong Pagar box (370
# buildings): only 47.6% survived at 8m, 80.3% at 30m -- and the loss is worst
# exactly where buildings are small, dense and elongated (shophouse rows).
_FOOTPRINT_CLUSTER_M = 30.0
_MIN_RING_AREA = 8.0        # m^2 -- drop tiny/degenerate clusters (thin-spike garbage)


def _hull_ring(cp):
    """Convex-hull ring for one cluster of XY points, or None if degenerate.

    Falls back to the axis-aligned bbox when the hull fails, which it does for
    exactly-collinear points (a single wall's base vertices). The bbox of a
    truly collinear set is zero-area and still fails _MIN_RING_AREA below, so
    this recovers real footprints without letting thin-spike garbage through."""
    if len(cp) < 3:
        return None
    try:
        ring = cp[ConvexHull(cp).vertices]
    except Exception:
        lo, hi = cp.min(axis=0), cp.max(axis=0)
        ring = np.array([[lo[0], lo[1]], [hi[0], lo[1]],
                         [hi[0], hi[1]], [lo[0], hi[1]]])
    return ring if Polygon(ring).area >= _MIN_RING_AREA else None


def _footprint_rings(base_pts_xy, cluster_dist=_FOOTPRINT_CLUSTER_M):
    """Split a piece's near-base points into proximity clusters and return ONE
    convex-hull ring per real cluster (list of (M, 2) arrays), or None.

    A OneMap `_BATCHID` piece is often a single mesh that spans several separate
    foundations 50-150m apart (blocks OneMap welded together, or extraction
    garbage) -- and that mesh is genuinely un-splittable (mesh connected-components,
    even after welding, keeps it whole; verified). But its BASE POINTS cluster
    cleanly by proximity. Taking one convex hull PER CLUSTER (instead of one giant
    hull over all of them) keeps each plunge skirt tight around an actual
    foundation and leaves the empty gaps between blocks with no footprint at all --
    so no skirt 'fin' is drawn across open air, and distant blocks stop merging
    (via overlapping spiky hulls) into one flat sunk compound.

    Returning None DELETES the building downstream, so clustering never gets the
    last word: if it yields nothing, fall back to a single hull over every base
    point. An approximate footprint is always better than a dropped building."""
    pts = np.asarray(base_pts_xy, dtype=float)
    if len(pts) < 3:
        return None
    tree = cKDTree(pts)
    pairs = tree.query_pairs(cluster_dist, output_type="ndarray")
    if len(pairs) == 0:
        labels = np.arange(len(pts))
    else:
        m = coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])),
                       shape=(len(pts), len(pts)))
        _, labels = connected_components(m, directed=False)
    rings = [r for r in (_hull_ring(pts[labels == c]) for c in np.unique(labels))
             if r is not None]
    if not rings:
        whole = _hull_ring(pts)
        if whole is None:
            return None
        rings = [whole]
    return [r.astype(np.float32) for r in rings]


def _extract_tile(uri, domain_polygon, archive, require_full=True):
    """Decode one leaf tile -> list of per-_BATCHID building-piece dicts. If
    domain_polygon is given, keep only pieces whose centroid falls inside it
    (domain_polygon=None keeps every piece -- used by the whole-island
    precompute). Runs on a worker thread.

    Fails gracefully: some tiles are permanently 403/missing on OneMap's own
    server (confirmed elsewhere in this project -- a real, small, unfixable
    subset), and others may simply not exist in the local archive/live tileset.
    Any fetch/decode error for this one tile is logged and skipped (returns
    []) rather than raising and aborting the whole domain extraction."""
    import sys
    try:
        data = fetch_tile(uri) if archive is None else fetch_tile(uri, archive=archive)
    except Exception as e:
        print(f"  [skip] tile fetch failed: {uri} ({e})", file=sys.stderr)
        return []
    try:
        ft = feature_table(data)
    except Exception as e:
        print(f"  [skip] tile decode failed: {uri} ({e})", file=sys.stderr)
        return []
    rtc = ft.get("RTC_CENTER")
    if rtc is None:
        return []
    rtc = np.array(rtc)
    try:
        gltf, blob = load_gltf(data)
    except Exception as e:
        print(f"  [skip] tile glTF parse failed: {uri} ({e})", file=sys.stderr)
        return []
    out = []
    try:
        for pts, faces, bid, T, R in _draco_prims(gltf, blob):
            svy = local_to_svy21(pts, T, R, rtc)
            for c in np.unique(bid):
                fmask = np.all(bid[faces] == c, axis=1)
                if not np.any(fmask):
                    continue
                sub_faces = faces[fmask]
                used = np.unique(sub_faces)
                v = svy[used]
                if np.ptp(v[:, 2]) < FLAT_PLATE_Z:
                    continue  # ground plate, not real volume
                # `require_full` decides what happens to a building straddling the
                # boundary, and the right answer depends on whether this polygon is
                # also the CLIP line.
                #
                #   True  (no buffer): the ROI *is* where the mesh gets cut, so a
                #         straddler would be SLICED flat by the meshlib box clip --
                #         the "every building half in, half out" bug. Dropping it is
                #         Deliverable 3's semantics and matches /api/domain/preview's
                #         "kept" (query_contained).
                #   False (buffer on): the clip is 250-750 m away at the wind
                #         rectangle, so nothing slices a straddler. Dropping it would
                #         instead leave a one-building-deep ring of bare ground around
                #         the ROI -- exactly where the urban roughness transition and
                #         the near-boundary shielding matter most.
                if domain_polygon is not None:
                    inside = contains_xy(domain_polygon, v[:, 0], v[:, 1])
                    if not (np.all(inside) if require_full else np.any(inside)):
                        continue
                remap = np.zeros(len(svy), dtype=np.int64)
                remap[used] = np.arange(len(used))
                base_z = float(v[:, 2].min())
                near_base = v[v[:, 2] <= base_z + 2.0][:, :2]
                out.append({
                    "verts": v.astype(np.float32).copy(),
                    "faces": remap[sub_faces].astype(np.int32),
                    "base_z": base_z,
                    "footprint": _footprint_rings(near_base),  # list of rings, or None
                })
    except Exception as e:
        print(f"  [skip] tile mesh decode failed: {uri} ({e})", file=sys.stderr)
        return []
    return out


def pieces_from_store(store_dir, tile_uri, domain_polygon=None,
                      require_full=True):
    """Load precomputed building pieces for one tile from the store, filtering
    to domain_polygon by centroid if given. Returns [] if the tile isn't in the
    store (caller can fall back to a live decode)."""
    from sbg.onemap_native.tiles import _tile_uri_to_local_path
    from pathlib import Path
    p = _tile_uri_to_local_path(tile_uri, Path(store_dir)).with_suffix(".npy")
    if not p.exists():
        return []
    pieces = list(np.load(p, allow_pickle=True))
    if domain_polygon is None:
        return pieces
    out = []
    for pc in pieces:
        v = pc["verts"]
        inside = contains_xy(domain_polygon, v[:, 0], v[:, 1])
        if np.all(inside) if require_full else np.any(inside):   # see _extract_tile
            out.append(pc)
    return out


def extract_domain_buildings(leaf_uris, domain_polygon, archive=None,
                             workers=DEFAULT_WORKERS, progress=None, store_dir=None,
                             require_full=True):
    """Extract every building piece whose centroid falls in domain_polygon.

    If store_dir is given, pieces are loaded from the precomputed store (fast
    local numpy read -- no b3dm fetch/decode/transform); any tile missing from
    the store falls back to a live parallel decode. Otherwise every tile is
    decoded live, in parallel.

    Returns a list of dicts, one per (tile, _BATCHID) piece:
        {"verts": (N,3) EPSG:3414, "faces": (M,3) local indices,
         "base_z": float, "footprint": (K,2) hull ring or None}
    Vertices are in OneMap's native z=0-base datum; the caller shifts each
    piece onto real terrain (see terrain.place_on_terrain). `progress`, if
    given, is called with (n_done, n_total) after each tile.
    """
    total = len(leaf_uris)
    if store_dir is not None:
        pieces, misses = [], []
        for i, u in enumerate(leaf_uris, 1):
            got = pieces_from_store(store_dir, u, domain_polygon, require_full)
            if got:
                pieces.extend(got)
            elif not (_store_has_tile(store_dir, u)):
                misses.append(u)  # genuinely absent -> live-decode fallback
            if progress is not None:
                progress(i, total)
        if misses:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(_extract_tile, u, domain_polygon, archive, require_full): u for u in misses}
                for fut in as_completed(futs):
                    pieces.extend(fut.result())
        return pieces

    pieces = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_extract_tile, u, domain_polygon, archive, require_full): u for u in leaf_uris}
        for i, fut in enumerate(as_completed(futs), 1):
            pieces.extend(fut.result())
            if progress is not None:
                progress(i, total)
    return pieces


def _store_has_tile(store_dir, tile_uri):
    from sbg.onemap_native.tiles import _tile_uri_to_local_path
    from pathlib import Path
    return _tile_uri_to_local_path(tile_uri, Path(store_dir)).with_suffix(".npy").exists()


def seal_piece(verts, faces):
    """Turn one raw OneMap piece into a closed, watertight solid at EXACTLY its
    original size. Returns (verts, faces), or the input unchanged on failure.

    This replaces the whole solidify-by-offset approach and is the single most
    consequential fix in this pipeline's history -- see the plan's root-cause
    section. OneMap meshes are DOUBLE-SIDED TRIANGLE SOUP: every real face stored
    twice with opposite winding, no shared vertices. A signed distance field over
    that has no coherent sign anywhere, so ANY isosurface extraction at level 0
    shatters it -- Blender's voxel remesh (130-903 bodies, ~1-6k m3 of a ~500k m3
    building) and meshlib's offsetMesh at small radius (207 bodies, 3,900 m3) fail
    identically. The old `offsetMesh(+1.5)` "worked" only by dilating far enough
    that the doubled surfaces merged into one blob, which is why its output was
    hollow, ~3m thick, and 7-16% too large in frontal area.

    Dedup the doubled faces and weld, and the mesh turns out to be essentially
    closed already: 1-15 real holes, which mr.fillHole closes exactly. Measured
    over 49 real pieces: 46 fully clean (holes=0, 1 body, strict watertight), 2
    genuinely disjoint structures sharing a _BATCHID, 1 with a residual
    non-manifold edge; max bbox error vs the raw capture 0.0000m; 0.32s total.

    Known caveat, not yet solved: fillHole caps the open base, and where the base
    spans a real opening (the void under an elevated bridge, a ground-level
    courtyard) it bridges it -- measured +17% plan-view silhouette on MD1. Frontal
    (blockage-relevant) silhouettes are unaffected: +0.00%.
    """
    import trimesh
    import meshlib.mrmeshpy as mr
    import meshlib.mrmeshnumpy as mn

    try:
        tm = trimesh.Trimesh(np.asarray(verts).copy(), np.asarray(faces).copy(),
                             process=True)
        tm.merge_vertices()
        key = np.sort(tm.faces, axis=1)
        _, uniq = np.unique(key, axis=0, return_index=True)
        ded = trimesh.Trimesh(tm.vertices, tm.faces[np.sort(uniq)], process=False)
        ded.update_faces(ded.nondegenerate_faces())
        ded.remove_unreferenced_vertices()
        trimesh.repair.fix_winding(ded)
        if len(ded.faces) < 4:
            return verts, faces
        m = mn.meshFromFacesVerts(np.asarray(ded.faces, dtype=np.int32),
                                  np.asarray(ded.vertices, dtype=np.float64))
        for _ in range(3):  # filling one hole can expose another
            edges = m.topology.findHoleRepresentiveEdges()
            if not len(edges):
                break
            for e in edges:
                try:
                    mr.fillHole(m, e, mr.FillHoleParams())
                except Exception:
                    pass
        return mn.getNumpyVerts(m), mn.getNumpyFaces(m.topology)
    except Exception:
        return verts, faces  # never let repair break extraction


def split_piece_components(piece):
    """Split one _BATCHID piece into its PHYSICALLY SEPARATE parts.

    SLA's _BATCHID does not mean "one building". Measured on the Kent Ridge
    hillside: piece 59 covers two structures 100m apart -- an 18x20m block on 60m
    ground and a 14x9m block on 40m ground, with no geometry joining them. Placed
    as one rigid unit they must share a level, so flattening the footprint digs a
    ~20m pit around whichever block sits high, and the un-flattened hillside
    around it then towers over the building -- user-reported as "the building is
    inside the mountain".

    A piece must sit at one level because it is one rigid SOLID -- and that is
    true of a connected component, not of a batch id. Splitting therefore removes
    a false constraint rather than adding an assumption.

    MUST run on SEALED geometry. OneMap meshes are double-sided soup whose glTF
    primitives never share vertices, so connectivity on the raw arrays is
    meaningless -- piece 59 reports 108 components of 2 faces each. After
    seal_piece (dedup + weld + winding + fillHole) it correctly reports 2.

    Returns a list of piece dicts (already sealed). Falls back to [piece] on any
    failure, so this can never lose a building.
    """
    import trimesh
    try:
        sv, sf = seal_piece(piece["verts"], piece["faces"])
        m = trimesh.Trimesh(np.asarray(sv), np.asarray(sf), process=True)
        m.merge_vertices()
        comps = m.split(only_watertight=False)
        if len(comps) < 2:
            # Recompute the footprint rather than carrying the piece's own: a
            # piece loaded from the precomputed store has its footprint BAKED IN
            # at store-build time, so a store built before a _footprint_rings fix
            # would keep silently dropping buildings until the whole store was
            # rebuilt. Recomputing here makes the stored footprint advisory only.
            v = np.asarray(sv, dtype=np.float32)
            base_z = float(v[:, 2].min())
            rings = _footprint_rings(v[v[:, 2] <= base_z + 2.0][:, :2])
            return [{**piece, "verts": v, "faces": np.asarray(sf, dtype=np.int32),
                     "base_z": base_z,
                     "footprint": rings if rings is not None else piece["footprint"]}]
        out = []
        for c in comps:
            v = np.asarray(c.vertices, dtype=np.float32)
            f = np.asarray(c.faces, dtype=np.int32)
            if len(f) < 4:      # provably cannot be a closed volume
                continue
            base_z = float(v[:, 2].min())
            near_base = v[v[:, 2] <= base_z + 2.0][:, :2]
            rings = _footprint_rings(near_base)
            if rings is None:
                continue
            out.append({"verts": v, "faces": f, "base_z": base_z,
                        "footprint": rings})
        return out or [piece]
    except Exception:
        return [piece]
