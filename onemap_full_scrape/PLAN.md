# Full OneMap mesh scrape — ground-truth archive

## What this is

Downloads the real 3D mesh (not just the height/attribute metadata Phase 1.5
already crawled) for every unique building OneMap knows about, as a raw
"ground truth" archive to have on hand later — for validating the pipeline,
for Phase 1.6-style landmark replacement without re-fetching each one
individually, or whatever else comes up. This does **not** get wired into
the live app automatically; it's just a data archive you build once and keep.

## The numbers (measured, not guessed)

The same building's OneMap record shows up in multiple tiles — real,
measured duplication, computed directly from the already-cached
`data/onemap_buildings.jsonl` (the Phase 1.5 crawl's output, 822,364 raw
records):

| | count |
|---|---|
| raw records | 822,364 |
| unique buildings (by `gml_id`) | 146,645 |
| distinct tiles referenced anywhere | 24,582 |
| **minimal set of tiles needed to cover every building at least once** | **4,481** |

Fetching all 24,582 referenced tiles would mean downloading ~5.5x more than
necessary. `compute_tile_manifest.py` computes the smaller 4,481-tile set
instead (a greedy set cover — not provably minimal, but real-world building
clustering makes greedy close enough; computing an actually-minimal cover is
NP-hard and not worth chasing here). At the already-measured ~5.0MB/tile
average (real sampled range 76KB–27.5MB across 82 tiles spanning all 6 tree
levels — see the main project plan's "Full OneMap mesh scrape" section):

- Fetch everything: ~123GB
- Fetch the minimal cover: **~22GB**

You mentioned your home PC has terabytes free, so the tight-disk-budget
concern that motivated this in the first place doesn't really apply — but
the dedup is still worth keeping even so, since it's strictly better
regardless of disk space: ~5.5x less time waiting, ~5.5x less bandwidth, and
no redundant tile files cluttering the archive. So `download_tiles.py` fetches
the 4,481-tile minimal set, not all 24,582.

(One thing worth remembering only if you ever run this somewhere
disk-constrained again: `df -h`/Python's `shutil.disk_usage()` inside WSL2
report space inside the `.vhdx` virtual disk, not the real Windows host
disk's free space — that's the "storage is a lie" thing you ran into. If
that matters again, check real free space via
`powershell.exe -Command "Get-PSDrive C"` from inside WSL, not `df`.)

## How to run it

```bash
# 1. (Already done on this machine — tile_manifest.json is committed to git,
#    see below, so a fresh clone doesn't even need this step.) No network
#    calls, just reads the already-cached data/onemap_buildings.jsonl:
.venv/bin/python onemap_full_scrape/compute_tile_manifest.py

# 2. The actual download. Resumable — safe to Ctrl-C and rerun; anything
#    already on disk gets skipped, not re-fetched.
.venv/bin/python onemap_full_scrape/download_tiles.py
```

Smoke-tested already (5 real tiles, confirmed valid B3DM files land in the
right place, confirmed a rerun correctly skips all 5 instead of
re-downloading) — this is real, working code, not a sketch.

`download_tiles.py` prints progress every 200 tiles (count, GB so far,
MB/s, elapsed time) so you can tell it's actually moving during an
unattended weekend run. At 16 concurrent workers (the same concurrency this
project's earlier batch-table crawl already proved safe against the live
API — 24,644 tiles, zero errors/403s/429s), a short burst test measured
~33MB/s; that was never confirmed to hold over a sustained multi-hour pull,
so treat the real completion time as "somewhere between 15 minutes and
a couple hours," not a fixed number.

Files land under `data/onemap_full_tiles/`, mirroring each tile's own
`level/x/y_z.b3dm` path exactly (not flattened into one folder) —
deliberate: an earlier version of this project's tile caching flattened
paths by joining segments with underscores and that turned out to be
genuinely ambiguous (two different real tiles collided to the same cache
filename). Mirroring the real path structure sidesteps that outright.

If some tiles fail (network hiccup, etc.), they're logged to
`onemap_full_scrape/failed_tiles.json` and the run still completes for
everything else — just rerun the same command afterward to retry only the
failures (everything already downloaded gets skipped automatically).

## What this does NOT do (a separate, later, CPU-bound step — not this weekend)

This only downloads the raw tile files. It does **not** decode them into
per-building meshes (Draco decode + `pymeshfix` repair, the same pipeline
`sbg/onemap/mesh.py::extract_building_mesh` already does for individual
Phase 1.6 landmarks) — doing that for 146,645 buildings is a real CPU-bound
job (recall a single large mesh's `pymeshfix` repair alone has taken
15–60+ seconds in this project's own STL-pipeline work), plausibly a
multi-day job on a single laptop, not something to bolt onto an overnight
download. Keeping the raw tiles as the archive (rather than eagerly
re-encoding everything into some other format) is also just the more
defensible "ground truth" artifact anyway — nothing lossy happens until you
actually decide to extract a specific building later, on demand, exactly
like the existing landmark-replacement code already does.

## Verifying it worked

```bash
# should match tile_manifest.json's tile count once fully done
find data/onemap_full_tiles -type f -name '*.b3dm' | wc -l

# spot-check a few files are real B3DM (not empty/error pages)
for f in $(find data/onemap_full_tiles -type f | head -5); do head -c 4 "$f"; echo "  <- $f"; done

# rerun the download command — should report 0 fetched, N already present
.venv/bin/python onemap_full_scrape/download_tiles.py
```

## Getting this onto your home PC

`onemap_full_scrape/*.py` and `onemap_full_scrape/tile_manifest.json` are
**not** covered by the existing `/data/` gitignore rule (they live in this
new top-level folder, not under `data/`) — confirmed directly with
`git check-ignore`. The manifest is 19MB, small enough to just commit
straight to git rather than making you copy the 212MB
`data/onemap_buildings.jsonl` it's derived from onto the other machine —
commit both the scripts and `tile_manifest.json`, push, and a plain
`git pull` on the home PC gets you straight to step 2 (the actual download)
with nothing else to transfer first. `data/onemap_full_tiles/` (the
downloaded output) stays exactly where the existing `/data/` gitignore rule
already covers it — never gets committed, same as the rest of `data/`.

I haven't committed/pushed anything myself — that's your call to make when
you're ready.
