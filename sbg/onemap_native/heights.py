"""Fast building-height lookup over an arbitrary polygon, for sizing the CFD buffer.

WHY NOT THE FOOTPRINT INDEX
    `sbg/onemap_native/ui/footprints.py` carries a `height` per record, and it is the
    obvious thing to reach for -- but it streams the raw `sg_buildings_v5.geojson`, where
    **72.8% of heights are 0** (86,474 of 118,782, measured). The OneMap height backfill
    only ever landed in the v1 CityJSON, never in that geojson. Sizing a buffer from it
    would silently under-read H by a large factor.

    `data/onemap_buildings.jsonl` is SLA's own batch-table data: 146,645 unique buildings,
    **0% zero heights**, real surveyed values. Reusing `sbg.onemap.backfill.load_onemap_points`
    (already deduped by gml_id and reprojected to EPSG:3414) costs 6.2 s cold, 0.06 s to
    build the tree, and **0.7 ms** per ROI query -- fast enough for a live preview.

    Cross-checked against the truth: for the Kent Ridge ROI this reports max 100.2 m, and
    the exact value computed at build time from the extracted OneMap meshes is 100.2 m.
"""
from __future__ import annotations

import pickle
import time
from pathlib import Path

import numpy as np

from sbg.config import DATA_DIR

_RECORDS = DATA_DIR / "onemap_buildings.jsonl"
_CACHE = DATA_DIR / "onemap_height_index.pkl"
_INDEX = None      # process-level memo: (xy, heights, tree)


def load_height_index(cache: Path = _CACHE, log=print):
    """(xy Nx2 EPSG:3414, heights N, cKDTree). Memoised per process, pickled per mtime."""
    global _INDEX
    if _INDEX is not None:
        return _INDEX

    from scipy.spatial import cKDTree
    mtime = _RECORDS.stat().st_mtime if _RECORDS.exists() else None
    if cache and Path(cache).exists():
        try:
            blob = pickle.loads(Path(cache).read_bytes())
            if blob.get("mtime") == mtime:
                xy, h = blob["xy"], blob["h"]
                _INDEX = (xy, h, cKDTree(xy))
                return _INDEX
        except Exception:
            pass   # a stale/corrupt cache is never fatal -- just rebuild it

    t0 = time.time()
    from sbg.onemap.backfill import load_onemap_points
    xy, records = load_onemap_points()
    h = np.array([r["height"] for r in records], dtype=float)
    log(f"[heights] {len(h):,} OneMap buildings loaded in {time.time() - t0:.1f}s")
    if cache:
        try:
            Path(cache).write_bytes(pickle.dumps({"mtime": mtime, "xy": xy, "h": h}))
        except Exception:
            pass
    _INDEX = (xy, h, cKDTree(xy))
    return _INDEX


def roi_height_stats(polygon, log=print) -> dict:
    """Height statistics for the buildings inside `polygon` (EPSG:3414).

    Returns max/p90/median/count. Both max and p90 are reported deliberately: one tall
    outlier doubles H and so **2.2x's the domain area** (measured on Kent Ridge -- max
    100.2 m vs p90 50.0 m gives an 18.4 vs 8.5 km^2 envelope), so the caller should show
    both rather than silently picking max.
    """
    from shapely import contains_xy
    xy, h, tree = load_height_index(log=log)

    xmin, ymin, xmax, ymax = polygon.bounds
    cx, cy = 0.5 * (xmin + xmax), 0.5 * (ymin + ymax)
    r = 0.5 * float(np.hypot(xmax - xmin, ymax - ymin))
    idx = np.asarray(tree.query_ball_point([cx, cy], r), dtype=np.int64)
    if idx.size == 0:
        return {"count": 0, "max": 0.0, "p90": 0.0, "median": 0.0}

    sub = xy[idx]
    inside = contains_xy(polygon, sub[:, 0], sub[:, 1])
    hs = h[idx][inside]
    if hs.size == 0:
        return {"count": 0, "max": 0.0, "p90": 0.0, "median": 0.0}
    return {"count": int(hs.size), "max": float(hs.max()),
            "p90": float(np.percentile(hs, 90)), "median": float(np.median(hs))}
