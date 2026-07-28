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
from scipy.spatial import ConvexHull
from shapely import contains_xy

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


def _footprint_hull(base_pts_xy):
    """Convex hull of a piece's near-base XY points, as an (M, 2) ring, or None."""
    if len(base_pts_xy) < 3:
        return None
    try:
        return base_pts_xy[ConvexHull(base_pts_xy).vertices]
    except Exception:
        return None


def _extract_tile(uri, domain_polygon, archive):
    """Decode one leaf tile -> list of per-_BATCHID building-piece dicts whose
    centroid falls in domain_polygon. Runs on a worker thread."""
    data = fetch_tile(uri) if archive is None else fetch_tile(uri, archive=archive)
    ft = feature_table(data)
    rtc = ft.get("RTC_CENTER")
    if rtc is None:
        return []
    rtc = np.array(rtc)
    gltf, blob = load_gltf(data)
    out = []
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
            cx, cy = v[:, 0].mean(), v[:, 1].mean()
            if not contains_xy(domain_polygon, cx, cy):
                continue
            remap = np.zeros(len(svy), dtype=np.int64)
            remap[used] = np.arange(len(used))
            base_z = float(v[:, 2].min())
            near_base = v[v[:, 2] <= base_z + 2.0][:, :2]
            out.append({
                "verts": v.copy(),
                "faces": remap[sub_faces],
                "base_z": base_z,
                "footprint": _footprint_hull(near_base),
            })
    return out


def extract_domain_buildings(leaf_uris, domain_polygon, archive=None,
                             workers=DEFAULT_WORKERS, progress=None):
    """Extract every building piece whose centroid falls in domain_polygon,
    decoding tiles in parallel.

    Returns a list of dicts, one per (tile, _BATCHID) piece:
        {"verts": (N,3) EPSG:3414, "faces": (M,3) local indices,
         "base_z": float, "footprint": (K,2) hull ring or None}
    Vertices are in OneMap's native z=0-base datum; the caller shifts each
    piece onto real terrain (see terrain.place_on_terrain). `progress`, if
    given, is called with (n_done, n_total) after each tile.
    """
    pieces = []
    total = len(leaf_uris)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_extract_tile, u, domain_polygon, archive): u for u in leaf_uris}
        for i, fut in enumerate(as_completed(futs), 1):
            pieces.extend(fut.result())
            if progress is not None:
                progress(i, total)
    return pieces
