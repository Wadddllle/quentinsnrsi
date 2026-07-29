"""Precompute the whole-island placed-building store, so domain cutouts never
re-fetch/decode tiles.

Decodes every OneMap leaf tile ONCE (correct 3D-Tiles transform, all _BATCHID
pieces, no domain clip) and saves the placed pieces to `<store>/<tile_rel>.npy`
(a pickled list of per-building dicts: float32 verts in EPSG:3414 at OneMap's
z=0 datum, int32 faces, base_z, footprint hull). Resumable (skips tiles already
in the store). The resulting store is a few GB, ships to prod, and makes
`sbg.onemap_native.build` cutouts a fast local slice + fuse instead of a slow,
flaky, per-domain tile fetch.

One-time job; run on a machine that has the tiles (local archive or network).
On the flaky USB archive, run with --workers 1 (serial reads don't stall the
drive the way concurrent ones do) and rely on --resume across restarts.

Usage:
  python -m sbg.onemap_native.precompute --out data/onemap_store --workers 1
  python -m sbg.onemap_native.precompute --out data/onemap_store --limit 50   # smoke test
"""
import argparse
import time
from pathlib import Path

import numpy as np

from sbg.onemap_native.extract import _extract_tile
from sbg.onemap_native.tiles import _tile_uri_to_local_path, domain_leaf_tiles, load_tileset


def _store_path(store_dir, tile_uri):
    return _tile_uri_to_local_path(tile_uri, Path(store_dir)).with_suffix(".npy")


def precompute_tile(tile_uri, store_dir, archive=None):
    """Decode one tile and write its pieces to the store. Returns n_pieces."""
    out_path = _store_path(store_dir, tile_uri)
    pieces = _extract_tile(tile_uri, None, archive)  # None domain = keep all
    out_path.parent.mkdir(parents=True, exist_ok=True)
    np.save(out_path, np.array(pieces, dtype=object))
    return len(pieces)


def build_store(store_dir, workers=1, limit=None, resume=True, archive=None, log=print):
    """Walk every leaf tile in the live tileset and precompute each into the
    store. `workers`=1 (serial) is safest for the flaky USB archive."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    store_dir = Path(store_dir)
    store_dir.mkdir(parents=True, exist_ok=True)
    log("enumerating whole-island leaf tiles...")
    # whole-island bbox in WGS84 (covers all of Singapore)
    leaves = domain_leaf_tiles(103.5, 1.15, 104.15, 1.50, tileset=load_tileset())
    if limit:
        leaves = leaves[:limit]
    todo = [u for u in leaves if not (resume and _store_path(store_dir, u).exists())]
    log(f"{len(leaves)} leaf tiles, {len(leaves) - len(todo)} already done, {len(todo)} to do")

    t0 = time.time()
    done = 0
    n_pieces = 0
    failed = []

    def _safe(u):
        # Skip-and-continue: a single unfetchable tile (e.g. OneMap's ~52
        # permanently-403 tiles, absent from the local archive too) must never
        # kill an unattended overnight run. Record it and move on -- rerunning
        # with --resume retries only the skipped ones.
        try:
            return precompute_tile(u, store_dir, archive)
        except Exception as e:
            failed.append((u, repr(e)))
            return 0

    def _report(i):
        el = time.time() - t0
        rate = i / max(el, 1e-6)
        eta = (len(todo) - i) / rate if rate else 0
        bar = "█" * int(30 * i / len(todo)) + "░" * (30 - int(30 * i / len(todo)))
        print(f"  [{bar}] {i}/{len(todo)} tiles  {n_pieces} pieces  ~{eta/60:.0f}min left",
              end="\r", flush=True)

    if workers <= 1:
        for i, u in enumerate(todo, 1):
            n_pieces += _safe(u)
            done = i
            if i % 5 == 0 or i == len(todo):
                _report(i)
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_safe, u): u for u in todo}
            for i, fut in enumerate(as_completed(futs), 1):
                n_pieces += fut.result()
                done = i
                if i % 5 == 0 or i == len(todo):
                    _report(i)
    print()
    log(f"done: {done} tiles, {n_pieces} pieces, {len(failed)} failed, {time.time()-t0:.0f}s. store at {store_dir}")
    if failed:
        log(f"failed tiles ({len(failed)}):")
        for u, err in failed:
            log(f"  {u}  {err}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True, help="store directory")
    ap.add_argument("--workers", type=int, default=1,
                    help="1 (serial) is safest for the flaky USB archive; higher for network")
    ap.add_argument("--limit", type=int, default=None, help="only first N tiles (smoke test)")
    ap.add_argument("--no-resume", action="store_true")
    ap.add_argument("--local", action="store_true",
                    help="read the local SSD archive first (only fetch live when a tile "
                         "is absent) -- for a bulk one-time job on a known-healthy disk")
    args = ap.parse_args()
    if args.local:
        from sbg.onemap_native import tiles
        tiles.PREFER_NETWORK = False
    build_store(args.out, workers=args.workers, limit=args.limit, resume=not args.no_resume)


if __name__ == "__main__":
    main()
