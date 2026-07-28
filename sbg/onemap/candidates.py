"""Candidate discovery for the OneMap review-gate (Phase 4c of the project
plan) -- point-in-polygon / point-in-radius lookups against the already-
cached OneMap batch-table crawl (data/onemap_buildings.jsonl, ~822k records
/ ~35k unique buildings from Phase 1.5). Zero network calls, near-instant --
this module never fetches a tile, it only tells the caller which real
OneMap buildings exist near a given place so the caller (an SBG footprint,
a 2D map click, or a resolved search hit) can decide what to fetch next.

Deliberately not ICP or any shape-matching technique -- direct user question
answered in the project plan: ICP refines alignment between two point sets
ALREADY assumed to correspond to the same object, it has no notion of "does
this footprint even correspond to that mesh." Point-in-polygon containment
against real OneMap centroids answers the actual question ("how many real
buildings does this SBG footprint represent") directly, and is exactly what
this module does.
"""
import json
from collections import defaultdict

import numpy as np
from pyproj import Transformer

from sbg.config import SOURCE_CRS, TARGET_CRS
from sbg.onemap.crawl_tiles import OUTPUT_PATH as ONEMAP_RECORDS_PATH

_transformer = Transformer.from_crs(SOURCE_CRS, TARGET_CRS, always_xy=True)

# Module-level cache: the raw crawl is 822k lines / ~210MB, not something to
# re-read per request. Populated lazily on first use (not at import time --
# keeps `import sbg.onemap.candidates` cheap for callers that only need the
# lat/lng->xy conversion or don't hit this data path at all this run) and
# kept for the process lifetime, matching every other "load big cached file
# once, hold in memory" pattern already established in sbg/ui/ (SpatialIndex,
# the full-island buildings body, etc.).
_cache = None


def _load():
    global _cache
    if _cache is not None:
        return _cache

    # One record per unique gml_id for display fields (name/height/storeys/
    # lat/lng) -- first-seen is fine, these don't vary meaningfully across a
    # building's ~5.6 duplicate tile references (see backfill.py's own
    # dedup). ALL tile URIs are kept per gml_id, not just the first, though --
    # extract_building_mesh's own docstring notes a real fraction of
    # per-tile extractions fail self-validation and the caller should retry
    # a different candidate tile for the same building; backfill.py's
    # load_onemap_points() (used for height matching) doesn't need this
    # since it never re-fetches a tile, but candidates.py's callers do.
    best = {}
    tiles = defaultdict(list)
    with open(ONEMAP_RECORDS_PATH) as f:
        for line in f:
            rec = json.loads(line)
            gid = rec.get("gml_id")
            if not gid or rec.get("lat") is None or rec.get("lng") is None:
                continue
            if gid not in best:
                best[gid] = rec
            tile = rec.get("tile")
            if tile and tile not in tiles[gid]:
                tiles[gid].append(tile)

    gml_ids = list(best.keys())
    lons = [best[g]["lng"] for g in gml_ids]
    lats = [best[g]["lat"] for g in gml_ids]
    xs, ys = _transformer.transform(lons, lats)
    xy = np.column_stack([xs, ys])

    _cache = (xy, gml_ids, best, tiles)
    return _cache


def latlng_to_xy(lat, lng):
    x, y = _transformer.transform(lng, lat)
    return float(x), float(y)


def _candidate_dict(gid, best, tiles, x, y, distance_m):
    rec = best[gid]
    name = (rec.get("name") or "").strip() or None
    return {
        "gml_id": gid,
        "name": name,
        "height": rec.get("height"),
        "storeys": rec.get("storeys"),
        "x": float(x),
        "y": float(y),
        "lat": rec.get("lat"),
        "lng": rec.get("lng"),
        "tiles": tiles.get(gid, []),
        "distance_m": float(distance_m),
    }


def find_candidates_near_point(x, y, radius_m=30.0, limit=25):
    """Real OneMap buildings within radius_m of (x, y) EPSG:3414 -- the
    entry point for a 2D-map-click or a resolved search hit (a search
    result only ever gives an approximate lat/lng, never a gml_id/tile --
    this is what turns "roughly here" into a real, pickable list instead of
    silently guessing the nearest point).
    """
    xy, gml_ids, best, tiles = _load()
    d = np.linalg.norm(xy - np.array([x, y]), axis=1)
    order = np.argsort(d)
    result = []
    for i in order:
        if d[i] > radius_m:
            break
        result.append(_candidate_dict(gml_ids[i], best, tiles, xy[i, 0], xy[i, 1], d[i]))
        if len(result) >= limit:
            break
    return result


def find_candidates_in_footprint(footprint_polygon, buffer_m=10.0, limit=25):
    """Real OneMap buildings whose reference point falls inside
    footprint_polygon (buffered by buffer_m -- OSM's traced outline and
    OneMap's real footprint don't always agree to the meter). The "replace
    this selected SBG building" entry point: for a normal building this
    should return exactly one candidate; for a combined-outline footprint
    like Esplanade's, several -- that's the actual question this function
    answers (see module docstring on why this replaces the originally-
    proposed ICP idea).
    """
    from shapely import vectorized  # local import: only needed on this path

    xy, gml_ids, best, tiles = _load()
    buffered = footprint_polygon.buffer(buffer_m)
    mask = vectorized.contains(buffered, xy[:, 0], xy[:, 1])
    idxs = np.nonzero(mask)[0]
    if idxs.size == 0:
        return []

    centroid = footprint_polygon.centroid
    cx, cy = centroid.x, centroid.y
    d = np.linalg.norm(xy[idxs] - np.array([cx, cy]), axis=1)
    order = np.argsort(d)
    result = [
        _candidate_dict(gml_ids[idxs[i]], best, tiles, xy[idxs[i], 0], xy[idxs[i], 1], d[i])
        for i in order[:limit]
    ]
    return result
