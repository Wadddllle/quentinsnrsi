"""POST/GET /api/versions/* -- version-control UI backing (Phase 3 of the
project plan), wrapping sbg.ui.versioning (a second, independent git repo
scoped to data/, see that module's docstring for why).

save_version() is a plain synchronous endpoint, not a background job like
the STL pipeline (sbg/ui/routers/pipeline.py) -- measured directly against
the real 189MB data/sbg.city.json before deciding: ~4-5s per save (git add +
commit, dominated by zlib-compressing the blob), well within a "pressed a
button, briefly waits" budget, nowhere near the STL pipeline's multi-minute
job-queue-worthy cost class.

Uses orjson, not stdlib json, for the read/write of the dataset file itself
-- measured directly (not assumed) against the real file: stdlib json.dump
took ~21.5s, orjson.dumps (to bytes, in-memory) + a plain write took ~0.7s
total, a ~30x difference and the dominant cost in the whole save path (git
add+commit alone is only ~4-5s). Load is a smaller but real win too (~6s
stdlib vs ~3.5s orjson, isolated-process measurement after warming the page
cache -- an un-isolated first measurement showed orjson looking SLOWER,
which was purely a cold-cache artifact from running it third in one
sequence, not a real result). Confirmed round-trips byte-identical to
stdlib json on the real file (no NaN/Infinity anywhere, which orjson would
otherwise reject). This is the same fix already applied to
sbg/ui/routers/dataset.py for the identical reason -- FastAPI's own
jsonable_encoder pass was a separate, additional cost measured there; this
router bypasses stdlib json entirely instead, which was the whole story here
since these functions call json.dump/load directly, not through FastAPI's
response pipeline.
"""
import orjson

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from sbg.config import DATA_DIR
from sbg.ui import versioning
from sbg.ui.spatial_index import build_index

router = APIRouter(prefix="/api/versions", tags=["versions"])


class SaveRequest(BaseModel):
    note: str | None = None


@router.post("/save")
def save(req: SaveRequest, request: Request):
    session = request.app.state.session
    cm = request.app.state.cm
    # Not hardcoded to sbg.city.json -- respects --dataset (see
    # sbg/ui/__main__.py), so e.g. a small test fixture used for iteration
    # gets its own commits in the same data/.git repo, never silently
    # touching the real sbg.city.json.
    dataset_path = request.app.state.dataset_path
    filename = dataset_path.name

    message = session.commit_message(note=req.note)

    # Write the current in-memory state to disk first (this is the "save"
    # the commit is actually snapshotting) -- app.state.cm is the live
    # working copy, mutated in place by /api/buildings/*/remove and (later,
    # Phase 4) /add, never written back to disk until this action.
    with open(dataset_path, "wb") as f:
        f.write(orjson.dumps(cm))

    result = versioning.save_version(DATA_DIR, filename, message)
    session.clear()
    return result


@router.get("")
def list_versions():
    return versioning.list_versions(DATA_DIR)


@router.post("/{commit_hash}/restore")
def restore(commit_hash: str, request: Request):
    dataset_path = request.app.state.dataset_path
    filename = dataset_path.name
    try:
        versioning.restore_version(DATA_DIR, commit_hash, filename)
    except RuntimeError as e:
        raise HTTPException(400, str(e))

    with open(dataset_path, "rb") as f:
        cm = orjson.loads(f.read())
    request.app.state.cm = cm

    index, _elapsed = build_index(cm)
    request.app.state.spatial_index = index

    request.app.state.session.clear()
    request.app.state.full_island_buildings_body = None

    return {"ok": True, "restored_hash": commit_hash, "building_count": len(cm["CityObjects"])}
