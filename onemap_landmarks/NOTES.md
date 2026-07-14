# Phase 1.6 landmark mesh work — reverted, kept for later re-implementation

## Status

All 6 mesh replacements below were built, embedded into `data/sbg.city.json`, and then
**reverted** back to LoD1 boxes (copied `data/sbg.city.json.pre-mesh-embed-backup` back
over `data/sbg.city.json`) after visual review found real placement/scope problems.
The extraction pipeline (`sbg/onemap/mesh.py`, `sbg/onemap/embed.py`) still works and is
still in the `sbg/` package — nothing there was reverted, only the SBG data file.

**Do not re-run `sbg/onemap/embed.py`'s CANDIDATES list against `sbg.city.json` again
as-is** — it will silently re-apply the same unreviewed replacements. Build the
review-gate step described below first.

## Candidates found (tile URI + gml_id), reusable as-is

| Building | SBG object id | gml_id | tile URI |
|---|---|---|---|
| The Interlace | `relation/7441127` | `SLA_BLDG2_1fe6b054-fab9-42ba-9821-0c6a98e83f98` | `.../4/5/1_4.b3dm` |
| Esplanade Theatres on the Bay (domes) | `way/97582570` | `SLA_BLDG2_e9850f77-b4a1-4b67-9b2c-61c38941e442` | `.../7/78/12_0.b3dm` |
| National Stadium | `way/182827369` | `SLA_BLDG2_31cf6c75-b8fc-4b33-af89-e789837f57e9` | `.../3/5/1_0.b3dm` |
| Flower Dome | `way/171142595` | `SLA_BLDG2_136d04c6-2354-48d0-885e-97b3624ed79d` | `.../6/41/2_0.b3dm` |
| Cloud Forest | `way/171142597` | `SLA_BLDG2_442c591c-8282-4f2d-a552-42e3ef8a7c7c` | `.../6/41/2_0.b3dm` |
| Singapore Indoor Stadium | `way/172472785` | `SLA_BLDG2_0a738074-bf78-4546-8df9-17042dc5c458` | `.../5/21/4_0.b3dm` |
| ArtScience Museum (needs **add**, no SBG object exists) | — | not yet found | — |

Full base URL: `https://www.onemap.gov.sg/omapi/tilesets/sg_noterrain_tiles/`

## What went wrong (visual review by user, against real neighboring context)

- **The Interlace**: "somewhat correct" — best of the 6, but not perfect either.
- **Singapore Indoor Stadium**: placed ~297m off from OneMap's own stated reference
  point for that gml_id — visually offset up-left from where it should be.
- **Esplanade domes**: floating disconnected from the base/mall — the old SBG footprint
  (`way/97582570`, 109-point combined outline) likely represents dome+mall+lobby as one
  shape (same "combined blob" pattern as Interlace), while OneMap's mesh for that gml_id
  is *only* the two roof domes. Swapping 1:1 deleted the mall's representation.
- **Flower Dome / Cloud Forest**: now overlapping each other, which they don't in reality.

## Root-cause hypothesis (not fully confirmed, but well-supported)

Checked each replaced building's mesh centroid (mean of all embedded vertices) against
OneMap's own `Latitude`/`Longitude` batch-table field for that same gml_id (an
independent reference, not derived from our own transform pipeline):

| Building | offset from OneMap's own reference point |
|---|---|
| Singapore Indoor Stadium | 297m |
| Esplanade domes | 159m |
| Flower Dome | 155m |
| Cloud Forest | 72m |
| The Interlace | 10m |

These don't scale consistently with mesh size or share one consistent direction, which
argues against a simple systematic rotation/translation bug in the coordinate pipeline
(`sbg/onemap/mesh.py`'s ENU-at-RTC_CENTER → ECEF → WGS84 → SVY21 chain — that pipeline's
*height* component was validated precisely against Interlace's known height, 97.345m
exact match, so the rotation itself is likely fine). More likely explanation: **scope
mismatch** — the OSM footprint and the OneMap mesh don't always represent the same
physical extent (combined multi-structure footprint vs. a single sub-structure's mesh),
so a small "centroid offset" here isn't just a placement error, it can also just be two
different-shaped things being compared. Translating a mesh by `-offset` would NOT
reliably fix this — it doesn't address a shape/scope mismatch, only a pure translation
one, and we don't have strong evidence it's purely the latter.

## Human-in-the-loop review gate — the plan for next time

Turn `embed.py` from "auto-apply the whole CANDIDATES list" into propose → review → apply:

1. **Numeric sanity check per candidate**, computed before touching `sbg.city.json`:
   - old footprint area (from the existing LoD1 solid) vs. new mesh's XY bounding-box area
   - old height vs. new mesh's height range
   - centroid offset vs. OneMap's own `Latitude`/`Longitude` reference (the check used
     above — reuse it, it's cheap and already caught real problems)
   - flag anything past a threshold (e.g. offset > ~30m, or area ratio far from 1.0)
2. **Contextual render, not an isolated one** — every render so far (this folder's PNGs)
   showed the candidate mesh alone, which is exactly why the scope mismatch wasn't
   visible until loaded in ninja next to real, unchanged neighbors. Render old box +
   its real nearby neighbors + the proposed new mesh together.
3. **Explicit recorded approval** — a small `reviewed_replacements.json` (approve /
   reject / needs-adjustment per gml_id) that `embed.py` reads and only applies entries
   marked approved. No silent batch-apply.

This pattern is also basically the earlier-noted "select a building → refresh from
OneMap" idea for the deferred web editor — same shape, human-triggered and
human-confirmed instead of batch-applied.

## Files in this folder

- `renders/` — the Poly3DCollection PNGs rendered during this session (isolated views,
  no neighbor context — that's exactly the gap to fix in the review-gate renderer)
- `exports/` — the `cjio subset`-extracted `.city.json` + `.obj` for each candidate, plus
  the raw `interlace_tile.glb` (Draco-compressed original tile mesh, for re-testing the
  decode pipeline without re-fetching from OneMap)
- `lookups/` — coordinate/tile/gml_id lookups gathered during the landmark search
