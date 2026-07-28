"""OneMap review-gate endpoints (Phase 4c of the project plan). Separate
router from routers/onemap.py (which stays scoped to the location-search
box) but shares its /api/onemap prefix -- no path collisions between the
two (search/candidates/propose/jobs/proposals/approve).

Five real steps, matching sbg.ui.onemap_review's own module docstring:
1. GET  /api/onemap/candidates       -- instant, cached-data-only lookup
2. POST /api/onemap/propose          -- async job: real fetch+decode+repair
3. GET  /api/onemap/jobs/{id}        -- poll (reuses sbg.ui.jobs)
4. GET  /api/onemap/proposals/{id}/geometry   -- contextual 3D preview data
   PATCH /api/onemap/proposals/{id}/nudge     -- manual adjust + live re-check
5. POST /api/onemap/approve          -- the only path that mutates cm
"""
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from sbg.onemap.candidates import find_candidates_in_footprint, find_candidates_near_point, latlng_to_xy
from sbg.ui.jobs import create_job, get_job
from sbg.ui.onemap_review import (
    apply_approval,
    build_preview_citymodel,
    discard_proposal,
    get_proposal,
    log_review_decision,
    nudge_candidate,
    run_propose_job,
    update_old_buildings,
)
from sbg.ui.responses import orjson_response

router = APIRouter(prefix="/api/onemap", tags=["onemap-review"])


@router.get("/candidates")
def get_candidates(
    request: Request,
    building_id: Optional[str] = None,
    x: Optional[float] = None,
    y: Optional[float] = None,
    lat: Optional[float] = None,
    lng: Optional[float] = None,
    radius_m: float = 30.0,
):
    """Three entry points converging on one shape, per the plan's own
    design: an existing SBG building (point-in-polygon against its real
    footprint -- for Esplanade-style combined outlines this is what
    surfaces "actually 4 real buildings here", not just 1), a 2D-map click
    (x/y, EPSG:3414), or a resolved search hit (lat/lng -- a search result
    only ever gives an approximate point, never a gml_id, so it re-runs the
    same nearby-candidates query centered there rather than silently
    guessing the nearest one). Zero network calls -- cached data only.
    """
    if building_id:
        poly = request.app.state.spatial_index.polygon_by_id.get(building_id)
        if poly is None:
            raise HTTPException(404, f"No footprint for building {building_id!r}")
        return {"candidates": find_candidates_in_footprint(poly)}
    if x is not None and y is not None:
        return {"candidates": find_candidates_near_point(x, y, radius_m=radius_m)}
    if lat is not None and lng is not None:
        xx, yy = latlng_to_xy(lat, lng)
        return {"candidates": find_candidates_near_point(xx, yy, radius_m=radius_m)}
    raise HTTPException(400, "Provide building_id, or x&y, or lat&lng")


class CandidateRecord(BaseModel):
    gml_id: str
    name: Optional[str] = None
    height: Optional[float] = None
    storeys: Optional[int] = None
    x: float
    y: float
    lat: Optional[float] = None
    lng: Optional[float] = None
    tiles: List[str] = []
    distance_m: Optional[float] = None


class ProposeRequest(BaseModel):
    old_building_ids: List[str]
    # The exact candidate dicts /candidates already returned and the user
    # picked from -- passed back rather than re-derived from bare gml_ids so
    # the job doesn't need to re-query the cached point set at all.
    candidates: List[CandidateRecord]


@router.post("/propose")
def propose(req: ProposeRequest, request: Request):
    if not req.candidates:
        raise HTTPException(400, "candidates must not be empty")
    cm = request.app.state.cm
    for oid in req.old_building_ids:
        if oid not in cm["CityObjects"]:
            raise HTTPException(404, f"No such CityObject: {oid!r}")
    job = create_job(
        run_propose_job, cm, request.app.state.spatial_index,
        req.old_building_ids, [c.model_dump() for c in req.candidates],
    )
    return {"job_id": job.id}


@router.get("/jobs/{job_id}")
def get_job_status(job_id: str):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job.to_dict()


@router.get("/proposals/{proposal_id}/geometry")
def get_proposal_geometry(proposal_id: str, request: Request, gml_ids: Optional[str] = None):
    """Real geometry for the proposal's current (post-nudge) candidate
    mesh(es), as a small mini-CityJSON -- rendered frontend-side through the
    exact same scoped-CityJSONLoader overlay mechanism already built for
    Phase 4b's added-buildings preview (see build_preview_citymodel's own
    docstring for why this reuse matters, not just convenience).
    """
    wanted = gml_ids.split(",") if gml_ids else None
    preview = build_preview_citymodel(request.app.state.cm, proposal_id, wanted)
    if preview is None:
        raise HTTPException(404, "proposal not found")
    return orjson_response(preview)


class NudgeRequest(BaseModel):
    gml_id: str
    dx: float = 0.0
    dy: float = 0.0
    dz: float = 0.0
    rotation_deg: float = 0.0


@router.patch("/proposals/{proposal_id}/nudge")
def nudge(proposal_id: str, req: NudgeRequest, request: Request):
    sanity = nudge_candidate(
        proposal_id, req.gml_id, req.dx, req.dy, req.dz, req.rotation_deg,
        request.app.state.cm, request.app.state.spatial_index,
    )
    if sanity is None:
        raise HTTPException(404, "proposal or candidate not found")
    return {"sanity": sanity}


class OldBuildingsRequest(BaseModel):
    old_building_ids: List[str]  # the FULL replacement list, not a delta


@router.patch("/proposals/{proposal_id}/old-buildings")
def patch_old_buildings(proposal_id: str, req: OldBuildingsRequest, request: Request):
    """Widens/narrows the old-building side of an already-proposed
    replacement -- "I realized the fetched mesh also covers a couple of
    adjacent buildings, pool them in." Does not touch any candidate mesh
    (see update_old_buildings's own docstring); just recomputes what
    they're compared against and re-runs sanity_check.
    """
    result = update_old_buildings(proposal_id, req.old_building_ids, request.app.state.cm, request.app.state.spatial_index)
    if result is None:
        raise HTTPException(404, "proposal not found")
    return result


class ApproveRequest(BaseModel):
    proposal_id: str
    decision: str  # "approve" | "reject" | "needs_adjustment"
    gml_ids: List[str] = []  # which candidates to actually embed -- only used for "approve"
    note: Optional[str] = None


@router.post("/approve")
def approve(req: ApproveRequest, request: Request):
    proposal = get_proposal(req.proposal_id)
    if proposal is None:
        raise HTTPException(404, "proposal not found")

    if req.decision == "approve":
        if not req.gml_ids:
            raise HTTPException(400, "gml_ids must list at least one candidate to approve")
        new_ids = apply_approval(
            request.app.state.cm, request.app.state.session,
            request.app.state.spatial_index, req.proposal_id, req.gml_ids,
        )
        request.app.state.full_island_buildings_body = None
        log_review_decision(req.proposal_id, "approve", req.note, proposal["old_building_ids"], req.gml_ids)
        # Same footprint-record shape (and same reuse-of-already-computed-
        # by-mark_added data) every Phase 4b add endpoint already returns --
        # lets the frontend push straight into local 2D/3D state instead of
        # refetching, and reuse the exact same removedIds/addedFootprints
        # display mechanism a plain Remove+Add already gets for free.
        records = request.app.state.spatial_index.footprint_records
        return {
            "ok": True, "old_building_ids": proposal["old_building_ids"], "new_building_ids": new_ids,
            "new_footprints": [records[nid] for nid in new_ids if nid in records],
            "session": request.app.state.session.to_status_dict(),
        }

    if req.decision in ("reject", "needs_adjustment"):
        log_review_decision(req.proposal_id, req.decision, req.note, proposal["old_building_ids"], list(proposal["candidates"].keys()))
        discard_proposal(req.proposal_id)
        return {"ok": True}

    raise HTTPException(400, f"Unknown decision: {req.decision!r}")
