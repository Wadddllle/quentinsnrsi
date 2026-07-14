"""POST /api/cutout/* -- domain boundary drawing, previewed and committed
both via the spatial index (no full-dataset scan). See project plan, Phase
6, Phase 1 build order.

commit() deliberately does NOT call sbg.cutout.cutout() -- measured that at
~56s against the full 118,782-building dataset (bbox ~2.7km x 2.7km, kept
4,315), because cutout() reconstructs every single CityObject's footprint
polygon unconditionally before testing containment, the exact O(n) scan
spatial_index.py exists to avoid. The index's query_contained() was already
verified (see spatial_index.py) to run the same exact shapely predicate, not
just a bbox filter, so building the kept-id set from it and compacting via
subset_cityjson() is equivalent, not an approximation -- confirmed via a
preview/commit consistency check (identical kept counts on both the small
test file and a real-dataset draw). sbg.cutout.cutout() itself is untouched
and still the canonical CLI/scripting entrypoint for a one-off cutout run.
"""
import json
from datetime import datetime, timezone
from typing import List, Tuple

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from shapely.geometry import Polygon

from sbg.config import DATA_DIR
from sbg.io_cityjson import subset_cityjson

router = APIRouter(prefix="/api/cutout", tags=["cutout"])


class DomainRequest(BaseModel):
    ring: List[Tuple[float, float]]  # EPSG:3414 meters, exterior ring only (Phase 1: no domain holes)


def _domain_polygon(req: DomainRequest) -> Polygon:
    if len(req.ring) < 3:
        raise HTTPException(400, "ring must have at least 3 points")
    poly = Polygon(req.ring)
    if not poly.is_valid or poly.area <= 0:
        raise HTTPException(400, "domain ring is not a valid polygon (self-intersecting or zero area?)")
    return poly


@router.post("/preview")
def preview(req: DomainRequest, request: Request):
    """Draft polygon -> kept/crossing building ids, via the spatial index's
    exact (not just bbox) containment test -- fast enough to call on every
    drag frame while a boundary is being drawn.
    """
    domain = _domain_polygon(req)
    index = request.app.state.spatial_index
    kept_ids = index.query_contained(domain)
    crossing_ids = index.query_intersects_not_contained(domain)
    return {
        "kept_ids": kept_ids,
        "crossing_ids": crossing_ids,
        "stats": {"kept": len(kept_ids), "crossing": len(crossing_ids), "area_m2": domain.area},
    }


@router.post("/commit")
def commit(req: DomainRequest, request: Request):
    """Builds the kept-id set from the spatial index (see module docstring
    for why this bypasses sbg.cutout.cutout()) and writes the compacted
    result to data/cutouts/. Same containment semantics preview uses --
    committed kept/dropped counts match the last preview exactly.
    """
    domain = _domain_polygon(req)
    cm = request.app.state.cm
    index = request.app.state.spatial_index

    kept_ids = index.query_contained(domain)
    new_cm = subset_cityjson(cm, kept_ids)
    stats = {
        "kept": len(kept_ids),
        "dropped_outside_or_crossing": len(index.ids) - len(kept_ids),
        "dropped_no_geometry": len(cm["CityObjects"]) - len(index.ids),
        "vertices": len(new_cm["vertices"]),
    }

    # Records exactly what domain produced this file -- otherwise the cutout
    # is just a filtered building list with no way to tell later what
    # boundary made it, or to redraw the same domain again. `ring` is enough
    # to reproduce the exact cutout (feed it back into this same endpoint or
    # sbg.cutout.cutout()'s --domain-geojson); bbox/area are here too since
    # those are what a scientist would actually want to eyeball quickly.
    xmin, ymin, xmax, ymax = domain.bounds
    new_cm["metadata"]["domain"] = {
        "ring": req.ring,
        "bbox": [xmin, ymin, xmax, ymax],
        "area_m2": domain.area,
        "committed_at": datetime.now(timezone.utc).isoformat(),
    }

    out_dir = DATA_DIR / "cutouts"
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"cutout_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.city.json"
    out_path = out_dir / fname
    with open(out_path, "w") as f:
        json.dump(new_cm, f)

    return {"stats": stats, "path": str(out_path.relative_to(DATA_DIR.parent))}
