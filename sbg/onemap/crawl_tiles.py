"""Bulk-crawl the OneMap 3D tileset once: enumerate every leaf tile, pull
batch-table-only metadata (real heights, storeys, names) via HTTP Range
requests, and write one flat JSONL of building records.

Usage: .venv/bin/python -m sbg.onemap.crawl_tiles
"""
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from sbg.config import DATA_DIR
from sbg.onemap.client import fetch_batch_table, iter_leaf_tiles, load_tileset

OUTPUT_PATH = DATA_DIR / "onemap_buildings.jsonl"
CACHE_DIR = DATA_DIR / "onemap_cache" / "batch_tables"


def _cache_path_for(uri):
    tail = uri.split("sg_noterrain_tiles/", 1)[-1]
    return CACHE_DIR / (tail.replace("/", "_") + ".json")


def _fetch_one(uri, range_bytes=16384):
    cache_path = _cache_path_for(uri)
    if cache_path.exists():
        return json.loads(cache_path.read_text())
    try:
        bt = fetch_batch_table(uri, range_bytes=range_bytes)
    except Exception as e:
        bt = {"_error": str(e)}
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(bt))
    return bt


def repair_errors(max_workers=16, range_bytes=131072):
    """Re-fetches any tile whose cache entry is an error (usually: batch table
    slightly exceeded the range budget) with a larger range, overwriting the cache.
    Re-enumerates tiles (cheap, tileset itself is cached) to recover each bad
    cache path's original URI rather than trying to reverse the filename mangling.
    """
    tileset = load_tileset()
    uris = list(iter_leaf_tiles(tileset))

    bad = []
    for uri in uris:
        path = _cache_path_for(uri)
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            data = {"_error": "corrupt cache file"}
        if "_error" in data:
            bad.append((uri, path))

    print(f"Found {len(bad)} tiles to repair", file=sys.stderr)
    if not bad:
        return 0

    fixed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fetch_batch_table, uri, range_bytes): path for uri, path in bad}
        for fut in as_completed(futures):
            path = futures[fut]
            try:
                bt = fut.result()
                path.write_text(json.dumps(bt))
                fixed += 1
            except Exception as e:
                path.write_text(json.dumps({"_error": str(e)}))

    print(f"Repaired {fixed}/{len(bad)} tiles", file=sys.stderr)
    return fixed


def rebuild_output_from_cache():
    """Rebuilds the JSONL output from whatever is currently cached on disk
    (use after repair_errors() to pick up newly-fixed tiles without re-crawling)."""
    tileset = load_tileset()
    uris = list(iter_leaf_tiles(tileset))

    all_records = []
    missing = 0
    for uri in uris:
        path = _cache_path_for(uri)
        if not path.exists():
            missing += 1
            continue
        bt = json.loads(path.read_text())
        if "_error" in bt:
            continue
        all_records.extend(records_from_batch_table(bt, uri))

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        for rec in all_records:
            f.write(json.dumps(rec) + "\n")
    print(f"Rebuilt {OUTPUT_PATH}: {len(all_records)} records, {missing} tiles not yet cached", file=sys.stderr)


def records_from_batch_table(bt, tile_uri):
    count = 0
    for key in ("gml:id", "Latitude", "Longitude", "Height"):
        if isinstance(bt.get(key), list):
            count = len(bt[key])
            break
    if count == 0:
        return []

    def col(key):
        vals = bt.get(key)
        return vals if isinstance(vals, list) and len(vals) == count else [None] * count

    gml_ids = col("gml:id")
    names = col("gml:name")
    lats = col("Latitude")
    lngs = col("Longitude")
    heights = col("bldg:measuredheight") if "bldg:measuredheight" in bt else col("Height")
    storeys = col("bldg:storeysaboveground")

    return [
        {
            "gml_id": gml_ids[i],
            "name": names[i],
            "lat": lats[i],
            "lng": lngs[i],
            "height": heights[i],
            "storeys": storeys[i],
            "tile": tile_uri,
        }
        for i in range(count)
    ]


def main(max_workers=8, limit=None):
    tileset = load_tileset()
    uris = list(iter_leaf_tiles(tileset))
    if limit:
        uris = uris[:limit]
    print(f"Enumerated {len(uris)} leaf tiles", file=sys.stderr)

    all_records = []
    errors = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_one, uri): uri for uri in uris}
        for i, fut in enumerate(as_completed(futures), 1):
            uri = futures[fut]
            bt = fut.result()
            if "_error" in bt:
                errors += 1
            else:
                all_records.extend(records_from_batch_table(bt, uri))
            if i % 200 == 0 or i == len(uris):
                print(
                    f"  ...{i}/{len(uris)} tiles ({time.time() - t0:.0f}s), "
                    f"{len(all_records)} building records so far, {errors} errors",
                    file=sys.stderr,
                )

    print(f"Done: {len(uris)} tiles, {errors} errors, {len(all_records)} building records, {time.time() - t0:.0f}s", file=sys.stderr)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        for rec in all_records:
            f.write(json.dumps(rec) + "\n")
    print(f"Wrote {OUTPUT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
