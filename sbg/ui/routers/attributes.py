"""PATCH /api/buildings/*/attributes -- metadata-only editing (Phase 4a of
the project plan). Deliberately a separate router file from buildings.py --
that one stays scoped to geometry-mutating ops (add/remove); this one never
touches `geometry`, only `attributes`, mirroring the UI's own clean split
between "change geometry" and "change metadata" (the source of the user's
own "it gets a little confusing" complaint that prompted this split).
"""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(prefix="/api/buildings", tags=["buildings"])

# Rejected outright (400), not just made read-only client-side -- load-
# bearing, not a nicety: without this, "fixing a label" could silently
# desync the displayed height number from the real extruded geometry with
# zero validation anywhere in the stack. These are exactly the fields real
# geometry-mutating operations (remove/add, and later the OneMap review-gate)
# are responsible for keeping in sync with actual geometry.
_BLOCKLIST = {
    "height",
    "height_source",
    "mesh_vertex_count",
    "mesh_face_count",
    "mesh_watertight",
    "onemap_gml_id",
    "onemap_storeys",
    "onemap_name",
}


class AttributesPatch(BaseModel):
    attributes: dict


@router.patch("/{building_id:path}/attributes")
def patch_attributes(building_id: str, patch: AttributesPatch, request: Request):
    cm = request.app.state.cm
    if building_id not in cm["CityObjects"]:
        raise HTTPException(404, f"No such CityObject: {building_id!r}")

    blocked = _BLOCKLIST & patch.attributes.keys()
    if blocked:
        raise HTTPException(400, f"Cannot edit geometry-linked field(s) here: {sorted(blocked)}")

    obj = cm["CityObjects"][building_id]
    # A COPY, not a bare reference -- unlike remove's snapshot (safe as a
    # reference because the object is deleted, not mutated in place), this
    # dict survives and gets mutated below, so a bare reference would
    # corrupt the undo snapshot the instant the patch applies.
    snapshot = dict(obj["attributes"])
    obj["attributes"].update(patch.attributes)

    request.app.state.session.record("edit_attributes", building_id, snapshot)
    # Stale in the precomputed full-island blob exactly like a removal is --
    # see buildings.py's remove() for why this is invalidated, not patched.
    request.app.state.full_island_buildings_body = None

    return {"ok": True, "building_id": building_id, "attributes": obj["attributes"], "session": request.app.state.session.to_status_dict()}
