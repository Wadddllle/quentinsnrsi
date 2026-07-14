"""OneMap 3D Tiles client: patched headers (the live API 403s without them) +
HTTP Range requests so bulk crawls only ever touch the batch-table prefix of
each tile, never the multi-MB Draco mesh payload.
"""
import json
import math
from urllib.parse import urlencode

import requests
from requests.adapters import HTTPAdapter

from sbg.config import DATA_DIR
from sbg.onemap.b3dm import extract_batch_table

TILESET_URL = "https://www.onemap.gov.sg/omapi/tilesets/sg_noterrain_tiles/tileset.json"
SEARCH_URL = "https://www.onemap.gov.sg/api/common/elastic/search"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Referer": "https://www.onemap.gov.sg/3d",
}

TILESET_CACHE = DATA_DIR / "onemap_cache" / "tileset.json"

_session = requests.Session()
_session.headers.update(HEADERS)
_session.mount("https://", HTTPAdapter(pool_maxsize=32))


def search_buildings(query, max_results=10):
    """Search OneMap by name/address/postal code; returns [{name, address, postal, lat, lng}, ...]."""
    params = {"searchVal": query, "returnGeom": "Y", "getAddrDetails": "Y", "pageNum": 1}
    r = _session.get(f"{SEARCH_URL}?{urlencode(params)}", timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data.get("found"):
        return []
    return [
        {
            "name": res.get("SEARCHVAL", query),
            "address": res.get("ADDRESS", ""),
            "postal": res.get("POSTAL", ""),
            "lat": float(res.get("LATITUDE", 0)),
            "lng": float(res.get("LONGITUDE", 0)),
        }
        for res in data.get("results", [])[:max_results]
    ]


def load_tileset(force_refresh=False):
    if TILESET_CACHE.exists() and not force_refresh:
        return json.loads(TILESET_CACHE.read_text())
    r = _session.get(TILESET_URL, timeout=60)
    r.raise_for_status()
    data = r.json()
    TILESET_CACHE.parent.mkdir(parents=True, exist_ok=True)
    TILESET_CACHE.write_text(json.dumps(data))
    return data


def fetch_batch_table(uri, range_bytes=16384, timeout=30):
    """Range-GET just the header+tables prefix of a tile and parse the batch table."""
    r = _session.get(uri, headers={"Range": f"bytes=0-{range_bytes - 1}"}, timeout=timeout)
    r.raise_for_status()
    return extract_batch_table(r.content)


def _region_contains_point(region, lat, lng):
    if len(region) < 4:
        return False
    west, south, east, north = region[:4]
    if abs(west) < math.pi * 2 and abs(east) < math.pi * 2:
        west, south, east, north = map(math.degrees, (west, south, east, north))
    return west <= lng <= east and south <= lat <= north


def _bounds_contain_point(bounds, lat, lng):
    if "region" in bounds:
        return _region_contains_point(bounds["region"], lat, lng)
    return True  # box/sphere bounds: no cheap check, assume possible match


def find_tiles_at_point(tileset, lat, lng, base_url=None):
    """Traverses the tileset tree, pruning branches whose bounding region
    doesn't contain (lat, lng), and returns matching leaf tile URIs sorted by
    geometricError (lower = higher detail) — for looking up a specific known
    building's tile precisely, rather than trusting a centroid join.
    """
    if base_url is None:
        base_url = TILESET_URL.rsplit("/", 1)[0]

    matches = []

    def walk(node, current_base):
        if not _bounds_contain_point(node.get("boundingVolume", {}), lat, lng):
            return
        content = node.get("content", {})
        uri = content.get("uri") or content.get("url")
        if uri:
            if not uri.startswith("http"):
                uri = f"{current_base}/{uri}"
            if uri.endswith(".json"):
                try:
                    nested = _session.get(uri, timeout=60).json()
                except Exception:
                    nested = {}
                nested_base = uri.rsplit("/", 1)[0]
                if "root" in nested:
                    walk(nested["root"], nested_base)
            else:
                matches.append((node.get("geometricError", 0), uri))
        for child in node.get("children", []):
            walk(child, current_base)

    walk(tileset.get("root", {}), base_url)
    matches.sort(key=lambda m: m[0])
    return [uri for _err, uri in matches]


def iter_leaf_tiles(tileset, base_url=None):
    """Walks the full tileset tree (no point filter) and yields every leaf
    tile's content URI, following nested tileset.json references.
    """
    if base_url is None:
        base_url = TILESET_URL.rsplit("/", 1)[0]

    def walk(node, current_base):
        content = node.get("content", {})
        uri = content.get("uri") or content.get("url")
        if uri:
            if not uri.startswith("http"):
                uri = f"{current_base}/{uri}"
            if uri.endswith(".json"):
                try:
                    nested = _session.get(uri, timeout=60).json()
                except Exception:
                    nested = {}
                nested_base = uri.rsplit("/", 1)[0]
                if "root" in nested:
                    yield from walk(nested["root"], nested_base)
            else:
                yield uri
        for child in node.get("children", []):
            yield from walk(child, current_base)

    yield from walk(tileset.get("root", {}), base_url)
