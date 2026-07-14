"""GET /api/dataset/* -- bbox-scoped reads against the loaded SBG dataset,
resolved via the in-memory spatial index (sbg.ui.spatial_index) so the
browser never has to fetch the full 118k-building file. See project plan,
Phase 6, Phase 1 build order item.
"""
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import ORJSONResponse

from sbg.io_cityjson import subset_cityjson

router = APIRouter(prefix="/api/dataset", tags=["dataset"])


def _parse_bbox(bbox: str):
    try:
        xmin, ymin, xmax, ymax = (float(v) for v in bbox.split(","))
    except (ValueError, AttributeError):
        raise HTTPException(400, "bbox must be 'xmin,ymin,xmax,ymax' in EPSG:3414 meters")
    if xmin >= xmax or ymin >= ymax:
        raise HTTPException(400, "bbox min must be less than max")
    return xmin, ymin, xmax, ymax


@router.get("/buildings")
def get_buildings(bbox: str, request: Request):
    """Bbox-scoped mini-CityJSON (own vertex array, remapped indices) --
    feeds ThreeJsViewer's `citymodel` prop directly. Buildings whose
    footprint merely intersects the bbox are included whole (not clipped),
    so panning slightly won't cut buildings in half mid-view.

    Returned via ORJSONResponse, constructed explicitly (see /footprints
    below for why: FastAPI's default jsonable_encoder pass is the dominant
    cost for a large plain-dict/list payload like this, not the actual JSON
    serialization).
    """
    xmin, ymin, xmax, ymax = _parse_bbox(bbox)
    index = request.app.state.spatial_index
    ids = index.query_intersects_bbox(xmin, ymin, xmax, ymax)
    # The only caller (SbgViewer3D's full-island load) always requests a
    # bbox covering the entire dataset -- serve the response precomputed
    # once at startup (see app.py's lifespan) instead of paying
    # subset_cityjson()'s ~13s O(all boundary-index references) walk again
    # on every request.
    if len(ids) == len(index.ids):
        return Response(content=request.app.state.full_island_buildings_body, media_type="application/json")
    if not ids:
        cm = request.app.state.cm
        return ORJSONResponse({
            "type": "CityJSON",
            "version": cm["version"],
            "transform": cm["transform"],
            "metadata": dict(cm.get("metadata", {})),
            "CityObjects": {},
            "vertices": [],
        })
    return ORJSONResponse(subset_cityjson(request.app.state.cm, ids))


@router.get("/footprints")
def get_footprints(bbox: str, request: Request):
    """Flat {id, rings, height, height_source} list for the 2D plan-view
    canvas -- no 3D geometry, no vertex pooling, just exterior (+ hole)
    rings in real-world (x, y). Records are precomputed once at startup
    (sbg.ui.spatial_index.SpatialIndex.footprint_records) -- this endpoint
    used to reconstruct every matched footprint's shapely Polygon from
    scratch per request, measured at 28s/42MB for a full-island bbox before
    that fix; it's now a plain dict lookup per matched id.

    Still measured at ~11.6s for the full-island case even after that fix --
    isolated the remaining cost to FastAPI's default `jsonable_encoder` pass
    (a slow recursive type-checking walk of the return value before handing
    it to the JSON encoder), which is pure overhead here since every value
    in these records is already a plain JSON-serializable primitive.
    Constructing `ORJSONResponse` directly and returning it bypasses that
    pass entirely (FastAPI does not re-process a Response object a handler
    returns) and uses `orjson`'s Rust-based encoder besides.
    """
    xmin, ymin, xmax, ymax = _parse_bbox(bbox)
    index = request.app.state.spatial_index
    ids = index.query_intersects_bbox(xmin, ymin, xmax, ymax)
    return ORJSONResponse({"buildings": [index.footprint_records[obj_id] for obj_id in ids]})


@router.get("/stats")
def get_stats(request: Request):
    cm = request.app.state.cm
    index = request.app.state.spatial_index
    return {
        "total_city_objects": len(cm["CityObjects"]),
        "indexed_buildings": len(index.ids),
        "transform": cm["transform"],
    }
