"""Full-mesh extraction from OneMap 3D Tiles: fetch a tile, Draco-decode via
DracoPy directly (bypassing trimesh's auto-decode path, which silently
produced degenerate all-zero vertices when tested against a real tile),
reproject local ENU-at-RTC_CENTER coordinates into EPSG:3414, and repair for
watertightness.

Coordinate pipeline: each glTF mesh's local vertices are reprojected to
EPSG:3414 via the CORRECT 3D-Tiles transform, RTC_CENTER + ZUP@(R@p + T)
(node rotation R + translation T, then the glTF Y-up->Z-up axis correction,
then RTC), then ECEF -> WGS84 (EPSG:4979) -> SVY21. This lives in
sbg.onemap_native.transform.local_to_svy21 and REPLACED an earlier hand-tuned
band-aid (sign-flipped node translation, node rotation ignored) that was
fundamentally wrong -- it happened to work only on tiles whose node rotation
was near the value it implicitly assumed, and silently misplaced geometry by
hundreds of meters (in both position AND orientation) on ~half of all tiles.
See the project plan's tile-native investigation for the full derivation.

Per-building isolation uses the `_BATCHID` per-vertex integer attribute, NOT
`mesh.name` string matching (a real bug this project shipped and then found
via direct user pushback -- see the project plan's "OneMap ground-truth
investigation" writeup for the full story). A OneMap tile's glTF mesh is
usually a COMBINED structure batching together every nearby building's real
geometry under ONE mesh, arbitrarily named after whichever building sits at
batch-table index 0 -- name-matching only ever finds that one "hero"
building and silently returns nothing for the other tens-to-hundreds of
buildings the same tile's batch table lists, even though their real geometry
is sitting right there in the same primitive. `_BATCHID` is what the
reference implementation (onemap-slicer, via a Node.js gltf-transform
decode) actually uses to split a combined mesh apart. Confirmed directly
against real downloaded tiles that DracoPy can decode `_BATCHID` itself, no
Node dependency needed: `ext['attributes']['_BATCHID']` gives the Draco
attribute's unique id, `decoded.get_attribute_by_unique_id(id)['data']` gives
the per-point batch id array. `_BATCHID` value == index into the batch
table's own `gml:id` array (verified 300/300 on real tiles) -- MOST of the
time; see the two real, independently-confirmed failure modes below that
`extract_building_mesh` now corrects for.

**Batch-id/mesh misalignment (the "shift" bug), confirmed 100% deterministic
on a real 80-building tile**: a tile's `gml:id` array order and the mesh's
actual per-vertex `_BATCHID` numbering can be offset by a small, tile-local,
non-constant amount -- verified directly by dumping every batch id's
extracted z-extent against its own recorded height across a whole real tile:
18/18 mismatches were exactly `extracted[i] == recorded[i+1]` (i.e. row i's
real geometry sits one slot away), and 13/13 "no geometry" slots were
explained by the row before them having *taken* their geometry. Root cause
not confirmed (plausibly an upstream indexing inconsistency in how SLA's own
pipeline serializes the batch table vs. assigns per-vertex batch ids), but
the fix is empirical and effective: if the naive `gml_ids.index(gml_id)`
batch id doesn't validate, widen the search outward (±1, ±2, ... up to
MAX_SHIFT_WINDOW) and take the height-matching candidate NEAREST the naive
index. Verified against a real 137-building population: recovers ~74% of
buildings that fail at the naive index alone.

**A structural limit of this fix, found directly, not theorized**: height-only
matching cannot always distinguish two real, physically DIFFERENT, adjacent
buildings that happen to share a near-identical recorded height (confirmed:
a real attempt to "recover" one building's geometry via the nearest
height-matching candidate instead silently stole an adjacent, already-
correctly-labeled *different* building's geometry -- a single, unique,
non-ambiguous match that was nonetheless wrong). The `n_candidates`/
`ambiguous` metadata this function attaches to its result flags only the
*detectable* half of this problem (multiple equally-good candidates); a
confident-but-wrong single match is NOT detectable from within this function
alone and needs external verification (real-world cross-checking, or a
future name/position-aware secondary signal) before being trusted blindly.

**Node-transform position bug (a third, independent failure mode) -- NOW FIXED PROPERLY**: this pipeline originally fed raw Draco `POSITION` data straight into an ENU-at-RTC_CENTER reprojection, ignoring the glTF node transform entirely. A first attempt patched it with a sign-flipped translation (`(-tx, +ty, -tz)`, rotation excluded) found by empirical search -- but that band-aid was still wrong: it converged only to 0.9-15.9m (not ~0), and a later whole-domain check found it misplaced ground by -130m to +266m on ~half of all tiles (visible only in Z, which a top-down render hides). The REAL fix is the standard 3D-Tiles transform now in sbg.onemap_native.transform: `p_ecef = RTC + ZUP@(R@p + T)`, applying the node's full rotation quaternion R AND translation T AND the glTF Y-up->Z-up axis correction (ZUP). Verified across a good tile and several the band-aid placed hundreds of meters wrong: every one lands building bases at exactly z=0 with zero scatter. The earlier "rotation makes it worse" conclusion was an artifact of applying rotation without the Y-up->Z-up correction -- with ZUP included, the node rotation is essential, not harmful.

**Scattered-fragment contamination (a second, independent failure mode)**:
`_BATCHID` isolation occasionally pulls in a small amount of real but
UNRELATED geometry from elsewhere in the tile (confirmed via direct render:
two genuine cases showed a real building's geometry plus a second, tiny,
100-290m-distant fragment merged under the same batch id -- not a Draco
precision fluke, a real spatial mispull). `_keep_largest_spatial_cluster`
groups face centroids by proximity (not mesh topology/vertex-sharing, which
is unusably noisy here -- confirmed directly: this tile format never welds
vertices *across* separate glTF primitives, so even a single genuine
building's own walls+roof+base show up as hundreds of "topologically
disconnected" pieces despite being one real, visually solid structure) and
discards every cluster except the largest. Verified safe against real
single-building extractions before shipping: 4 confirmed-real buildings
(including a 2,378-face one) all stayed a single, unfragmented cluster at
the chosen 50m threshold; 2 confirmed-contaminated extractions correctly
split, keeping 84-95% (matching an independent manual verification of one
of them exactly, 382/402 faces).
"""
import struct

import DracoPy
import numpy as np
import pygltflib
import requests
import trimesh
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree

from sbg.onemap.b3dm import batch_table_end_offset, extract_batch_table
from sbg.onemap.client import HEADERS
from sbg.onemap_native.transform import local_to_svy21, node_transform


# Below this Z-extent (meters), treat a primitive as a flat ground plate/decal,
# not real building volume (confirmed pattern: a companion mesh at z==0 spanning
# the same footprint as the real 3D structure).
_FLAT_PRIMITIVE_Z_TOLERANCE = 0.01

# How far to search outward from the naive batch_id (0, +1, -1, +2, -2, ...)
# for a height-validating candidate before giving up -- see module docstring
# on the shift-misalignment bug. Verified against a real 137-building
# population at this window size (~74% recovery of naive-index failures).
MAX_SHIFT_WINDOW = 10

# Max gap (meters, between face centroids) within which geometry is treated
# as part of the same real structure -- see module docstring on the
# scattered-fragment-contamination fix. Chosen conservatively generous
# (confirmed real garbage fragments were 100-290m away; a real large complex
# building could plausibly have legitimate internal gaps -- e.g. separate
# wings around a courtyard -- up to several tens of meters, so this is
# deliberately well clear of that to avoid false-fragmenting a real building).
CLUSTER_DISTANCE_M = 50.0


def fetch_full_tile(uri, timeout=120):
    r = requests.get(uri, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.content


def _feature_table(data):
    (_v, _bl, ft_json_len, _fb, _bj, _bb) = struct.unpack("<6I", data[4:28])
    import json

    ft_json = data[28:28 + ft_json_len].decode("utf-8").rstrip("\x00")
    return json.loads(ft_json) if ft_json.strip() else {}


def _keep_largest_spatial_cluster(pts, faces, threshold_m=CLUSTER_DISTANCE_M):
    """Groups faces by proximity of their centroids (not mesh topology --
    see module docstring on why vertex-sharing is unusably noisy here) and
    returns only the faces belonging to the largest connected cluster,
    discarding smaller, spatially-separate fragments as contamination."""
    if len(faces) < 2:
        return faces
    centroids = pts[faces].mean(axis=1)
    n = len(centroids)
    tree = cKDTree(centroids)
    pairs = tree.query_pairs(r=threshold_m, output_type="ndarray")
    if len(pairs) == 0:
        # every face is isolated from every other at this threshold -- too
        # sparse/degenerate a mesh to meaningfully cluster, leave unfiltered
        # rather than arbitrarily keep just one face.
        return faces
    adj = coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(n, n))
    _n_components, labels = connected_components(adj, directed=False)
    largest_label = np.bincount(labels).argmax()
    return faces[labels == largest_label]


# NOTE: the old `_node_position_correction` band-aid (`(-tx, +ty, -tz)`, node
# rotation ignored) was REMOVED -- it was fundamentally wrong (misplaced geometry
# by hundreds of meters on ~half of all tiles, both position and orientation) and
# is fully superseded by sbg.onemap_native.transform.local_to_svy21, which applies
# the correct 3D-Tiles transform RTC + ZUP@(R@p + T). See the project plan's
# tile-native investigation for the full derivation.


def _decode_batch_geometry(gltf, binary_blob, candidate_ids, rtc_center):
    """Decodes every Draco/_BATCHID primitive in the tile ONCE (the
    expensive step), accumulating vertex/face data per requested candidate
    batch id in the same pass -- avoids re-decoding per shift-window
    candidate. Each mesh's points are reprojected to EPSG:3414 via the
    CORRECT 3D-Tiles transform (RTC + ZUP@(R@p + T), see
    sbg.onemap_native.transform -- this replaced a wrong hand-tuned band-aid
    that ignored the node rotation and Y-up->Z-up axis correction, silently
    misplacing geometry by hundreds of meters on ~half of all tiles).
    Returns {batch_id: (points_Nx3_svy21, faces_Mx3_local_indices)} for
    whichever candidates have real (non-flat) geometry."""
    candidate_set = set(candidate_ids)
    acc_verts = {c: [] for c in candidate_set}
    acc_faces = {c: [] for c in candidate_set}
    acc_offset = {c: 0 for c in candidate_set}
    node_for_mesh = {n.mesh: n for n in (gltf.nodes or []) if n.mesh is not None}

    for mesh_index, mesh in enumerate(gltf.meshes):
        T, R = node_transform(node_for_mesh.get(mesh_index))
        for prim in mesh.primitives:
            ext = (prim.extensions or {}).get("KHR_draco_mesh_compression")
            if ext is None or "_BATCHID" not in ext.get("attributes", {}):
                continue
            bv = gltf.bufferViews[ext["bufferView"]]
            start = bv.byteOffset or 0
            compressed = binary_blob[start:start + bv.byteLength]
            decoded = DracoPy.decode(compressed)
            pts = np.asarray(decoded.points)
            faces = np.asarray(decoded.faces)
            if pts.size == 0 or faces.size == 0:
                continue

            pts = local_to_svy21(pts, T, R, rtc_center)  # correct transform, per-mesh node TRS

            batchid_attr = decoded.get_attribute_by_unique_id(ext["attributes"]["_BATCHID"])
            batch_ids = np.asarray(batchid_attr["data"]).reshape(-1).astype(np.int64)
            face_batch_ids = batch_ids[faces]

            for c in candidate_set:
                face_mask = np.all(face_batch_ids == c, axis=1)
                if not np.any(face_mask):
                    continue

                sub_faces = faces[face_mask]
                used_vert_idx = np.unique(sub_faces)
                sub_pts = pts[used_vert_idx]

                z_extent = sub_pts[:, 2].max() - sub_pts[:, 2].min()
                if z_extent < _FLAT_PRIMITIVE_Z_TOLERANCE:
                    continue  # flat ground plate / decal, not real volume

                remap = np.zeros(len(pts), dtype=np.int64)
                remap[used_vert_idx] = np.arange(len(used_vert_idx))
                remapped_faces = remap[sub_faces]

                off = acc_offset[c]
                acc_verts[c].append(sub_pts)
                acc_faces[c].append(remapped_faces + off)
                acc_offset[c] += len(sub_pts)

    result = {}
    for c in candidate_set:
        if acc_verts[c]:
            result[c] = (np.concatenate(acc_verts[c], axis=0), np.concatenate(acc_faces[c], axis=0))
    return result


def extract_building_mesh(tile_uri, gml_id, allow_shift_search=True):
    """Fetches a tile, isolates the named building's real (non-flat) mesh via
    the _BATCHID per-vertex attribute (see module docstring for why this,
    not mesh.name, is the correct isolation mechanism), reprojects to
    TARGET_CRS, repairs for watertightness, and returns a trimesh.Trimesh —
    or None if the building/tile has no usable mesh.

    On success, the returned mesh carries `mesh.metadata["onemap_batch_offset"]`
    (0 if the naive batch id worked directly, nonzero if a shift-search
    recovery was needed) and `mesh.metadata["onemap_batch_ambiguous"]` (True
    if multiple equally-near candidates matched — see module docstring on
    why even a *non*-ambiguous shifted match isn't guaranteed correct, and
    should be cross-checked before being trusted for anything high-stakes).

    allow_shift_search=False restricts to the naive batch id only (matches
    this function's original, pre-fix behavior) — useful for isolating
    whether a shift-search result was actually needed.
    """
    data = fetch_full_tile(tile_uri)
    bt = extract_batch_table(data)
    gml_ids = bt.get("gml:id", [])
    if gml_id not in gml_ids:
        raise ValueError(f"{gml_id} not found in batch table for {tile_uri}")
    base_idx = gml_ids.index(gml_id)
    n = len(gml_ids)

    recorded_height = None
    for key in ("bldg:measuredheight", "Height"):
        if key in bt and base_idx < len(bt[key]):
            recorded_height = bt[key][base_idx]
            break

    glb = data[batch_table_end_offset(data):]
    gltf = pygltflib.GLTF2().load_from_bytes(glb)
    binary_blob = gltf.binary_blob()

    feature_table = _feature_table(data)
    rtc_center = feature_table.get("RTC_CENTER")
    if rtc_center is None:
        raise ValueError(f"No RTC_CENTER in feature table for {tile_uri}")
    rtc_center = np.asarray(rtc_center)

    if allow_shift_search:
        rings = [[0]] + [[k, -k] for k in range(1, MAX_SHIFT_WINDOW + 1)]
    else:
        rings = [[0]]
    offsets = [o for ring in rings for o in ring]
    candidates = {base_idx + o for o in offsets if 0 <= base_idx + o < n}

    # points come back already in EPSG:3414 (correct per-mesh 3D-Tiles transform)
    decoded_by_id = _decode_batch_geometry(gltf, binary_blob, candidates, rtc_center)

    # Evaluate candidates ring-by-ring, nearest offset first (cluster-filtering
    # before the height check, since debris can otherwise inflate z-extent past
    # what a real match would show) -- and stop after the first ring that
    # produces any match, checking the rest of that same ring only to detect a
    # tie (the "ambiguous" flag). Nothing farther out can ever be nearer than a
    # match already found, so this is exactly equivalent to evaluating every
    # candidate out to the full window and picking the nearest -- just without
    # wastefully cluster-filtering candidates that can never win. Measured
    # necessary: _keep_largest_spatial_cluster (KDTree clustering) was found to
    # dominate wall time (~75% on a 60-building profile, ~13.5 calls/building
    # against the old exhaustive-to-+-/-10 approach) precisely because most
    # buildings match at or near offset 0 and every farther candidate was still
    # being fully cluster-filtered for nothing.
    matches = []
    for ring in rings:
        for offset in ring:
            c = base_idx + offset
            if c not in decoded_by_id:
                continue
            pts, faces = decoded_by_id[c]
            faces = _keep_largest_spatial_cluster(pts, faces)
            used = np.unique(faces)
            z_extent = pts[used][:, 2].max() - pts[used][:, 2].min()
            if recorded_height is None or abs(z_extent - recorded_height) <= max(0.3, 0.15 * recorded_height):
                matches.append((offset, pts, faces))
        if matches:
            break

    if not matches:
        return None

    matches.sort(key=lambda m: abs(m[0]))
    best_offset, pts, faces = matches[0]
    ambiguous = sum(1 for m in matches if abs(m[0]) == abs(best_offset)) > 1

    # Compact down to only the vertices the (possibly cluster-filtered)
    # faces actually reference.
    used = np.unique(faces)
    remap = np.zeros(len(pts), dtype=np.int64)
    remap[used] = np.arange(len(used))
    world_pts = pts[used]  # already EPSG:3414 from _decode_batch_geometry
    faces = remap[faces]

    mesh = trimesh.Trimesh(vertices=world_pts, faces=faces, process=False)
    mesh = repair_mesh(mesh)
    mesh.metadata["onemap_batch_offset"] = int(best_offset)
    mesh.metadata["onemap_batch_ambiguous"] = bool(ambiguous)
    return mesh


def repair_mesh(mesh):
    """pymeshfix repair for watertight output, falling back to lightweight
    trimesh-native cleanup if pymeshfix fails."""
    try:
        import pymeshfix

        meshfix = pymeshfix.MeshFix(mesh.vertices, mesh.faces)
        meshfix.repair(verbose=False, remove_smallest_components=False)
        if len(meshfix.v) > 0 and len(meshfix.f) > 0:
            return trimesh.Trimesh(vertices=meshfix.v, faces=meshfix.f)
    except Exception:
        pass

    try:
        mesh.fix_normals()
        mesh.update_faces(mesh.nondegenerate_faces())
        mesh.update_faces(mesh.unique_faces())
        mesh.remove_unreferenced_vertices()
    except Exception:
        pass
    return mesh
