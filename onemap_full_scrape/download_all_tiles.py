"""Downloads EVERY leaf tile in the live OneMap 3D tileset (not just the
minimal covering set download_tiles.py computes) -- now that a real 1TB SSD
(ssd-data/, a symlink to /mnt/d/snrsi_data) removes the reason to be clever
about minimizing download volume. This sidesteps the LOD-bias bug found in
compute_tile_manifest.py's greedy "first tile in file order" selection
entirely: with every tile local, there's no "wrong tile was picked" question
left to ask -- any future extraction can pick the best (lowest
geometricError) available tile for a building directly, with everything
already on disk.

Tile list comes from a LIVE tileset walk (iter_leaf_tiles(load_tileset())),
not from onemap_buildings.jsonl's own already-crawled tile references --
deliberately more authoritative, since the batch-table crawl that produced
that jsonl had a real ~7.8% error rate before its own repair pass, and a
live walk doesn't inherit that.

Same concurrency/headers/resumability pattern as download_tiles.py (proven
safe: 24,644 tiles at 16 workers, zero errors/403s/429s across this whole
project's crawl history).

Usage:
    .venv/bin/python onemap_full_scrape/download_all_tiles.py
    .venv/bin/python onemap_full_scrape/download_all_tiles.py --workers 12 --limit 50  # smoke test
"""
import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sbg.onemap.client import HEADERS, iter_leaf_tiles, load_tileset

DEFAULT_OUT = Path("/home/quentin/snrsi/ssd-data/onemap_all_tiles")
PROGRESS_LOG = Path(__file__).resolve().parent / "download_all_tiles_progress.log"


def tile_uri_to_local_path(tile_uri: str, out_dir: Path) -> Path:
    parsed = urlparse(tile_uri)
    marker = "sg_noterrain_tiles/"
    idx = parsed.path.find(marker)
    rel = parsed.path[idx + len(marker):] if idx != -1 else parsed.path.lstrip("/")
    return out_dir / rel


def download_one(tile_uri: str, out_dir: Path, timeout: int, session: requests.Session):
    local_path = tile_uri_to_local_path(tile_uri, out_dir)
    if local_path.exists() and local_path.stat().st_size > 0:
        return tile_uri, local_path, 0, "skipped (already downloaded)"

    local_path.parent.mkdir(parents=True, exist_ok=True)
    r = session.get(tile_uri, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    local_path.write_bytes(r.content)
    return tile_uri, local_path, len(r.content), "ok"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--limit", type=int, default=None, help="stop after this many tiles (smoke test)")
    args = ap.parse_args()

    print("Enumerating full live tileset (this walks the whole tree, may take a minute)...", file=sys.stderr)
    tileset = load_tileset()
    uris = list(iter_leaf_tiles(tileset))
    if args.limit:
        uris = uris[:args.limit]
    print(f"Total tiles to fetch: {len(uris)}", file=sys.stderr)

    args.out.mkdir(parents=True, exist_ok=True)
    session = requests.Session()

    done = 0
    skipped = 0
    errors = 0
    total_bytes = 0
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(download_one, uri, args.out, args.timeout, session): uri for uri in uris}
        for i, fut in enumerate(as_completed(futures), 1):
            uri = futures[fut]
            try:
                _uri, path, nbytes, status = fut.result()
                if status.startswith("skipped"):
                    skipped += 1
                else:
                    done += 1
                    total_bytes += nbytes
            except Exception as e:
                errors += 1
                print(f"  ERROR fetching {uri}: {e}", file=sys.stderr)

            if i % 200 == 0 or i == len(uris):
                elapsed = time.time() - t0
                rate = total_bytes / elapsed / 1e6 if elapsed > 0 else 0
                msg = (f"  ...{i}/{len(uris)} tiles ({elapsed:.0f}s), "
                       f"{done} fetched / {skipped} skipped / {errors} errors, "
                       f"{total_bytes/1e9:.2f}GB downloaded ({rate:.1f}MB/s avg)")
                print(msg, file=sys.stderr)
                with open(PROGRESS_LOG, "a") as f:
                    f.write(msg + "\n")

    elapsed = time.time() - t0
    final_msg = (f"DONE: {len(uris)} tiles, {done} fetched, {skipped} skipped, {errors} errors, "
                 f"{total_bytes/1e9:.2f}GB downloaded in {elapsed:.0f}s")
    print(final_msg, file=sys.stderr)
    with open(PROGRESS_LOG, "a") as f:
        f.write(final_msg + "\n")


if __name__ == "__main__":
    main()
