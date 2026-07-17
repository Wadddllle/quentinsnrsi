"""Downloads every tile in tile_manifest.json's minimal covering set (run
compute_tile_manifest.py first) -- the full, mesh-containing B3DM payload
for each tile (NOT the range-limited batch-table-only fetch Phase 1.5's
crawl already did), saved as raw bytes for later offline mesh extraction.

Concurrency (~16 workers) and headers mirror sbg/onemap/client.py's already
crawl-tested pattern (24,644 tiles at 16 workers, zero errors/403s/429s --
see the project plan's "Full OneMap mesh scrape" section). Reuses
sbg.onemap.client.HEADERS directly rather than re-deriving them.

Output layout deliberately MIRRORS each tile URI's own path structure under
--out (default data/onemap_full_tiles/), e.g.
".../sg_noterrain_tiles/4/0/0_5.b3dm" -> "data/onemap_full_tiles/4/0/0_5.b3dm".
This is NOT arbitrary -- an earlier version of this project's tile caching
underscore-joined path segments into one flattened filename and that turned
out to be irreversibly ambiguous (two different real tile paths collided to
the same cache filename). Preserving the real path hierarchy sidesteps that
entirely.

Resumable by construction: re-running this script skips any tile whose
output file already exists and is non-empty, so it's safe to interrupt
(Ctrl-C, laptop sleep, closed terminal) and rerun later -- no separate
checkpoint file needed, the downloaded files ARE the checkpoint.

Usage:
    .venv/bin/python onemap_full_scrape/compute_tile_manifest.py   # once, no network
    .venv/bin/python onemap_full_scrape/download_tiles.py          # the actual download
    .venv/bin/python onemap_full_scrape/download_tiles.py --workers 8 --out /some/other/path
"""
import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sbg.config import DATA_DIR
from sbg.onemap.client import HEADERS

MANIFEST_PATH = Path(__file__).resolve().parent / "tile_manifest.json"
DEFAULT_OUT = DATA_DIR / "onemap_full_tiles"


def tile_uri_to_local_path(tile_uri: str, out_dir: Path) -> Path:
    # Keep everything after ".../sg_noterrain_tiles/" as the relative path --
    # preserves the tileset's own level/x/y_z.b3dm structure exactly, no
    # flattening, no ambiguity (see module docstring).
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
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"output directory (default: {DEFAULT_OUT})")
    ap.add_argument("--workers", type=int, default=16, help="concurrent download workers (default 16, matches the already-crawl-tested pattern)")
    ap.add_argument("--timeout", type=int, default=120, help="per-tile request timeout in seconds")
    ap.add_argument("--limit", type=int, default=None, help="stop after this many tiles (for a quick smoke test, e.g. --limit 20)")
    args = ap.parse_args()

    if not MANIFEST_PATH.exists():
        print(f"ERROR: {MANIFEST_PATH} not found -- run compute_tile_manifest.py first (no network needed for that step).", file=sys.stderr)
        sys.exit(1)

    with open(MANIFEST_PATH) as f:
        manifest = json.load(f)
    tiles = manifest["tiles"]
    if args.limit:
        tiles = tiles[: args.limit]

    args.out.mkdir(parents=True, exist_ok=True)
    print(f"[download_tiles] {len(tiles)} tiles to fetch, {args.workers} workers, output -> {args.out}", file=sys.stderr)

    session = requests.Session()
    session.headers.update(HEADERS)

    done = 0
    skipped = 0
    failed = []
    total_bytes = 0
    t0 = time.time()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(download_one, uri, args.out, args.timeout, session): uri for uri in tiles}
        for fut in as_completed(futures):
            uri = futures[fut]
            try:
                _uri, _path, nbytes, status = fut.result()
                done += 1
                total_bytes += nbytes
                if status.startswith("skipped"):
                    skipped += 1
            except Exception as e:
                failed.append((uri, str(e)))
                done += 1

            if done % 200 == 0 or done == len(tiles):
                elapsed = time.time() - t0
                rate_mb_s = (total_bytes / 1e6) / elapsed if elapsed > 0 else 0
                print(
                    f"[download_tiles] {done}/{len(tiles)} "
                    f"({skipped} already had, {len(failed)} failed) -- "
                    f"{total_bytes/1e9:.2f}GB downloaded, {rate_mb_s:.1f}MB/s, {elapsed/60:.1f}min elapsed",
                    file=sys.stderr,
                )

    print(f"\n[download_tiles] DONE: {done - skipped - len(failed)} fetched, {skipped} already present, {len(failed)} failed.", file=sys.stderr)
    if failed:
        failed_log = Path(__file__).resolve().parent / "failed_tiles.json"
        with open(failed_log, "w") as f:
            json.dump(failed, f, indent=2)
        print(f"[download_tiles] {len(failed)} failures logged to {failed_log} -- rerun this script (same command) to retry just those, everything else is skipped as already-done.", file=sys.stderr)


if __name__ == "__main__":
    main()
