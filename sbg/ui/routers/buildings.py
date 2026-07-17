"""POST /api/buildings/* -- the first mutating operation in the live UI
(Phase 3 of the project plan: remove-building + versioning). Wraps
sbg.edit.remove_building directly (no pipeline logic duplicated here) and
layers session/undo bookkeeping + spatial-index invalidation on top, both of
which are sbg/ui/-only concerns -- sbg/edit.py itself is untouched.

remove_building(cm, id, compact=False) is called with compact ALWAYS False
here -- compaction is O(all vertices) and would make undo lossy/expensive
(see sbg/ui/session.py's module docstring); it's deliberately not something
this interactive session ever triggers.
"""
from fastapi import APIRouter, HTTPException, Request

from sbg.edit import remove_building

router = APIRouter(prefix="/api/buildings", tags=["buildings"])


@router.post("/{building_id:path}/remove")
def remove(building_id: str, request: Request):
    cm = request.app.state.cm
    if building_id not in cm["CityObjects"]:
        raise HTTPException(404, f"No such CityObject: {building_id!r}")

    # Captured BEFORE remove_building() runs -- it doesn't return the
    # deleted object (see sbg/edit.py) -- and by reference, not a deep copy;
    # see session.py's module docstring for why that's safe here.
    snapshot = cm["CityObjects"][building_id]
    remove_building(cm, building_id, compact=False)

    request.app.state.session.record("remove", building_id, snapshot)
    request.app.state.spatial_index.mark_removed(building_id)
    # Can't be patched incrementally (it's precomputed serialized bytes) --
    # invalidate and let /api/dataset/buildings recompute lazily on the next
    # full-island request, not eagerly here. See dataset.py.
    request.app.state.full_island_buildings_body = None

    return {"ok": True, "removed_id": building_id, "session": request.app.state.session.to_status_dict()}


@router.post("/undo")
def undo(request: Request):
    session = request.app.state.session
    entry = session.pop_last()
    if entry is None:
        raise HTTPException(400, "Nothing to undo")

    cm = request.app.state.cm
    index = request.app.state.spatial_index
    if entry.op == "remove":
        cm["CityObjects"][entry.building_id] = entry.snapshot
        index.mark_restored(entry.building_id)
    elif entry.op == "add":
        del cm["CityObjects"][entry.building_id]
        # No spatial-index bookkeeping needed yet -- Phase 3 never adds a
        # building, this branch exists for Phase 4 to fall straight into.

    request.app.state.full_island_buildings_body = None
    return {"ok": True, "undone": entry.to_dict(), "session": session.to_status_dict()}
