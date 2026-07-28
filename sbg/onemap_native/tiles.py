"""Selecting and reading OneMap 3D leaf tiles that cover a domain.

The tile-native pipeline extracts the WHOLE mesh of each leaf tile covering a
domain (no per-building _BATCHID isolation, no shift-search, no LoD1 fallback)
-- the same thing OneMap's own viewer renders. Leaf tiles are the finest LOD and
partition space, so gathering every leaf intersecting the domain gives complete,
non-overlapping coverage.

Tiles are read from the local full-archive first (ssd-data/onemap_all_tiles/,
all ~24.6k leaf tiles), falling back to a live fetch.
"""
import json
import math
import struct
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import pygltflib
import requests

from sbg.onemap.b3dm import batch_table_end_offset, extract_batch_table
from sbg.onemap.client import HEADERS, TILESET_URL, load_tileset

LOCAL_ARCHIVE = Path("/home/quentin/snrsi/ssd-data/onemap_all_tiles")
_session = requests.Session()

# Network-primary by default: fetch tiles live and (optionally) cache to disk.
# The 113GB USB-SSD archive was found unreliable -- concurrent reads over WSL's
# 9p layer stall the drive into an unkillable D-state hang. The CDN handles
# concurrency fine and never hangs (a bad request just errors + retries), and
# no prod deployment wants a 120GB local dependency anyway. Set PREFER_NETWORK
# = False to read the local archive first (only sensible for a bulk one-time
# job on a KNOWN-healthy local -- not USB -- disk).
PREFER_NETWORK = True


def _tile_uri_to_local_path(tile_uri, archive=LOCAL_ARCHIVE):
    parsed = urlparse(tile_uri)
    marker = "sg_noterrain_tiles/"
    idx = parsed.path.find(marker)
    rel = parsed.path[idx + len(marker):] if idx != -1 else parsed.path.lstrip("/")
    return archive / rel


def _fetch_live(tile_uri, timeout):
    r = _session.get(tile_uri, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.content


def fetch_tile(tile_uri, archive=LOCAL_ARCHIVE, timeout=120, prefer_network=None,
               cache_dir=None):
    """Fetch a tile's bytes. Network-primary by default (PREFER_NETWORK); falls
    back to the local archive on any network error. If prefer_network is False,
    reads the local archive first and only fetches live when the tile is absent.
    If cache_dir is given, live-fetched bytes are written there for reuse.
    """
    if prefer_network is None:
        prefer_network = PREFER_NETWORK
    lp = _tile_uri_to_local_path(tile_uri, archive)

    if not prefer_network and lp.exists() and lp.stat().st_size > 0:
        return lp.read_bytes()

    try:
        content = _fetch_live(tile_uri, timeout)
    except Exception:
        if lp.exists() and lp.stat().st_size > 0:  # fall back to whatever we have
            return lp.read_bytes()
        raise

    if cache_dir is not None:
        cp = _tile_uri_to_local_path(tile_uri, Path(cache_dir))
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_bytes(content)
    return content


def _region_deg(bounding_volume):
    r = bounding_volume.get("region")
    if not r:
        return None
    w, s, e, n = r[:4]
    if abs(w) < 7:  # radians
        w, s, e, n = map(math.degrees, (w, s, e, n))
    return (w, s, e, n)


def _region_intersects(region, lon_min, lat_min, lon_max, lat_max):
    if region is None:
        return True  # box/sphere bounds: no cheap check, assume possible
    w, s, e, n = region
    return not (e < lon_min or w > lon_max or n < lat_min or s > lat_max)


def domain_leaf_tiles(lon_min, lat_min, lon_max, lat_max, tileset=None):
    """Walk the live tileset tree (pruning by bounding region) and return every
    leaf tile URI whose region intersects the given WGS84 lat/lng bbox.
    Follows nested tileset.json references.
    """
    if tileset is None:
        tileset = load_tileset()
    base_url = TILESET_URL.rsplit("/", 1)[0]
    leaves = []

    def walk(node, cur_base):
        if not _region_intersects(_region_deg(node.get("boundingVolume", {})),
                                   lon_min, lat_min, lon_max, lat_max):
            return
        content = node.get("content", {})
        uri = content.get("uri") or content.get("url")
        children = node.get("children", [])
        if uri and uri.endswith(".json"):
            u = uri if uri.startswith("http") else f"{cur_base}/{uri}"
            try:
                nested = _session.get(u, timeout=60).json()
            except Exception:
                nested = {}
            if "root" in nested:
                walk(nested["root"], u.rsplit("/", 1)[0])
            return
        if uri and not children:  # leaf with real content
            leaves.append(uri if uri.startswith("http") else f"{cur_base}/{uri}")
        for child in children:
            walk(child, cur_base)

    walk(tileset.get("root", {}), base_url)
    return leaves


def feature_table(data):
    """B3DM feature table dict (carries RTC_CENTER)."""
    (_v, _bl, ft_json_len, _fb, _bj, _bb) = struct.unpack("<6I", data[4:28])
    ft_json = data[28:28 + ft_json_len].decode("utf-8").rstrip("\x00")
    return json.loads(ft_json) if ft_json.strip() else {}


def load_gltf(data):
    """Parse the embedded GLB of a b3dm into a pygltflib.GLTF2 + binary blob."""
    gltf = pygltflib.GLTF2().load_from_bytes(data[batch_table_end_offset(data):])
    return gltf, gltf.binary_blob()
