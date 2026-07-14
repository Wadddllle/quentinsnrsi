"""Full-mesh extraction from OneMap 3D Tiles: fetch a tile, Draco-decode via
DracoPy directly (bypassing trimesh's auto-decode path, which silently
produced degenerate all-zero vertices when tested against a real tile),
reproject local ENU-at-RTC_CENTER coordinates into EPSG:3414, and repair for
watertightness.

Coordinate pipeline (empirically validated against The Interlace, where the
known height 97.345m matched exactly): local mesh vertices are already in a
direct East-North-Up frame centered at the tile's RTC_CENTER (no rotation
needed beyond ecef = RTC_CENTER + x*East + y*North + z*Up), then
ECEF -> WGS84 (EPSG:4979) -> SVY21 (EPSG:3414).
"""
import struct

import DracoPy
import numpy as np
import pygltflib
import requests
import trimesh
from pyproj import Transformer

from sbg.config import TARGET_CRS
from sbg.onemap.b3dm import batch_table_end_offset, extract_batch_table
from sbg.onemap.client import HEADERS

_ecef_to_wgs84 = Transformer.from_crs("EPSG:4978", "EPSG:4979", always_xy=True)
_wgs84_to_target = Transformer.from_crs("EPSG:4326", TARGET_CRS, always_xy=True)

# Below this Z-extent (meters), treat a primitive as a flat ground plate/decal,
# not real building volume (confirmed pattern: a companion mesh at z==0 spanning
# the same footprint as the real 3D structure).
_FLAT_PRIMITIVE_Z_TOLERANCE = 0.01


def fetch_full_tile(uri, timeout=120):
    r = requests.get(uri, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.content


def _feature_table(data):
    (_v, _bl, ft_json_len, _fb, _bj, _bb) = struct.unpack("<6I", data[4:28])
    import json

    ft_json = data[28:28 + ft_json_len].decode("utf-8").rstrip("\x00")
    return json.loads(ft_json) if ft_json.strip() else {}


def _enu_to_target_crs(local_pts, rtc_center):
    """local_pts: Nx3 array in ENU-at-rtc_center, Z-up. Returns Nx3 in (x, y, height)
    for TARGET_CRS (height stays as real-world meters above the ellipsoid,
    consistent with how build_sbg.py already treats height as a flat scalar)."""
    lon0, lat0, _h0 = _ecef_to_wgs84.transform(*rtc_center)
    phi, lam = np.radians(lat0), np.radians(lon0)

    east = np.array([-np.sin(lam), np.cos(lam), 0])
    north = np.array([-np.sin(phi) * np.cos(lam), -np.sin(phi) * np.sin(lam), np.cos(phi)])
    up = np.array([np.cos(phi) * np.cos(lam), np.cos(phi) * np.sin(lam), np.sin(phi)])

    ecef = (
        np.asarray(rtc_center)
        + np.outer(local_pts[:, 0], east)
        + np.outer(local_pts[:, 1], north)
        + np.outer(local_pts[:, 2], up)
    )
    lon, lat, h = _ecef_to_wgs84.transform(ecef[:, 0], ecef[:, 1], ecef[:, 2])
    x, y = _wgs84_to_target.transform(lon, lat)
    return np.column_stack([x, y, h])


def extract_building_mesh(tile_uri, gml_id):
    """Fetches a tile, isolates the named building's real (non-flat) mesh
    primitives, reprojects to TARGET_CRS, repairs for watertightness, and
    returns a trimesh.Trimesh — or None if the building/tile has no usable mesh.
    """
    data = fetch_full_tile(tile_uri)
    bt = extract_batch_table(data)
    if gml_id not in bt.get("gml:id", []):
        raise ValueError(f"{gml_id} not found in batch table for {tile_uri}")

    glb = data[batch_table_end_offset(data):]
    gltf = pygltflib.GLTF2().load_from_bytes(glb)
    binary_blob = gltf.binary_blob()

    all_verts = []
    all_faces = []
    vertex_offset = 0
    for mesh in gltf.meshes:
        if not mesh.name or gml_id not in mesh.name:
            continue
        for prim in mesh.primitives:
            ext = (prim.extensions or {}).get("KHR_draco_mesh_compression")
            if ext is None:
                continue
            bv = gltf.bufferViews[ext["bufferView"]]
            start = bv.byteOffset or 0
            compressed = binary_blob[start:start + bv.byteLength]
            decoded = DracoPy.decode(compressed)
            pts = np.asarray(decoded.points)
            faces = np.asarray(decoded.faces)
            if pts.size == 0 or faces.size == 0:
                continue
            z_extent = pts[:, 2].max() - pts[:, 2].min()
            if z_extent < _FLAT_PRIMITIVE_Z_TOLERANCE:
                continue  # flat ground plate / decal, not real building volume
            all_verts.append(pts)
            all_faces.append(faces + vertex_offset)
            vertex_offset += len(pts)

    if not all_verts:
        return None

    local_pts = np.concatenate(all_verts, axis=0)
    faces = np.concatenate(all_faces, axis=0)

    feature_table = _feature_table(data)
    rtc_center = feature_table.get("RTC_CENTER")
    if rtc_center is None:
        raise ValueError(f"No RTC_CENTER in feature table for {tile_uri}")

    world_pts = _enu_to_target_crs(local_pts, rtc_center)

    mesh = trimesh.Trimesh(vertices=world_pts, faces=faces, process=False)
    return repair_mesh(mesh)


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
