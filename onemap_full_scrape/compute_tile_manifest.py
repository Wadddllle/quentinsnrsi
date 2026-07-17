"""Computes the MINIMAL set of OneMap leaf tiles needed to fetch every real
building's full mesh at least once -- no network calls, uses only the
already-cached data/onemap_buildings.jsonl (from the Phase 1.5 batch-table
crawl, which already recorded every unique building's (gml_id, tile) pair).

Why this matters (measured directly, not assumed): the same building's
attributes appear in 5.61 real duplicate records on average across different
tiles (822,364 raw records -> 146,645 unique gml_ids), because OneMap's
tileset tree has real spatial/LOD overlap between neighboring/ancestor
tiles. Fetching every one of the 24,582 distinct tiles referenced anywhere
in the cache would mean paying for ~5.5x more downloaded bytes than
necessary. A simple greedy "first tile that mentions each gml_id" cover
needs only 4,481 tiles to include every unique building at least once --
computed directly against the real cache, not estimated:

    total raw records:                822364
    unique buildings (by gml_id):     146645
    distinct tiles referenced at all: 24582
    minimal covering tile set:        4481   (5.49x fewer than "fetch everything")

At the already-measured ~5.0MB/tile average (real sampled range 76KB-27.5MB
across 82 tiles spanning all 6 tree levels, see the project plan's "Full
OneMap mesh scrape" section), that's roughly 123GB for "fetch every tile"
vs. roughly 22GB for the minimal cover -- the difference between "does not
fit in a laptop's real free disk space" and "comfortably does."

This is a greedy set cover (assign each gml_id to the first tile that
mentions it, in file order), not a provably-minimal one -- computing an
actually-minimal set cover is NP-hard in general, and greedy is the
standard, good-enough approximation for this kind of coverage problem. Not
worth the complexity here: real-world clustering (most buildings are only
referenced by a small, tight set of tiles anyway) means greedy is very
likely close to optimal in practice, and getting within a few percent of
minimal is worth far more effort than chasing exact optimality.

Usage: .venv/bin/python onemap_full_scrape/compute_tile_manifest.py
Output: onemap_full_scrape/tile_manifest.json
    {"tiles": [tile_uri, ...], "gml_id_to_tile": {gml_id: tile_uri, ...}}
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sbg.config import DATA_DIR

ONEMAP_BUILDINGS_JSONL = DATA_DIR / "onemap_buildings.jsonl"
MANIFEST_PATH = Path(__file__).resolve().parent / "tile_manifest.json"


def compute_manifest():
    gml_id_to_tile = {}
    total_records = 0

    with open(ONEMAP_BUILDINGS_JSONL) as f:
        for line in f:
            rec = json.loads(line)
            total_records += 1
            gml_id = rec["gml_id"]
            if gml_id not in gml_id_to_tile:
                gml_id_to_tile[gml_id] = rec["tile"]

    tiles = sorted(set(gml_id_to_tile.values()))

    print(f"total raw records:            {total_records}", file=sys.stderr)
    print(f"unique buildings (gml_id):    {len(gml_id_to_tile)}", file=sys.stderr)
    print(f"minimal covering tile set:    {len(tiles)}", file=sys.stderr)
    print(f"estimated download volume:    ~{len(tiles) * 5.0 / 1024:.1f}GB (at 5.0MB/tile average)", file=sys.stderr)

    return {"tiles": tiles, "gml_id_to_tile": gml_id_to_tile}


if __name__ == "__main__":
    manifest = compute_manifest()
    with open(MANIFEST_PATH, "w") as f:
        json.dump(manifest, f)
    print(f"Wrote {MANIFEST_PATH} ({len(manifest['tiles'])} tiles, {len(manifest['gml_id_to_tile'])} buildings)", file=sys.stderr)
