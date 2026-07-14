"""POST /api/pipeline/* -- triggers the async STL build (see sbg.ui.pipeline)
for a drawn domain and lets the frontend poll its progress. See project
plan, Phase 6, "async STL pipeline trigger". Deliberately a separate,
explicit action from /api/cutout/commit (which stays fast/JSON-only) -- a
single domain this size can take minutes, so it isn't something that should
fire on every commit while a scientist is still iterating on a boundary.
"""
from pathlib import Path
from typing import List, Tuple

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from sbg.config import DATA_DIR
from sbg.ui.jobs import create_job, get_job
from sbg.ui.pipeline import run_stl_pipeline

router = APIRouter(prefix="/api/pipeline", tags=["pipeline"])

JOBS_ROOT = DATA_DIR / "stl_jobs"


class DomainRequest(BaseModel):
    ring: List[Tuple[float, float]]  # EPSG:3414 meters, exterior ring only


@router.post("/run")
def run(req: DomainRequest, request: Request):
    if len(req.ring) < 3:
        raise HTTPException(400, "ring must have at least 3 points")
    cm = request.app.state.cm
    job = create_job(run_stl_pipeline, cm, req.ring, JOBS_ROOT, spatial_index=request.app.state.spatial_index)
    return {"job_id": job.id}


@router.get("/jobs/{job_id}")
def get_job_status(job_id: str):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job.to_dict()


@router.get("/jobs/{job_id}/download")
def download_stl(job_id: str):
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    if job.status != "done" or not job.result:
        raise HTTPException(409, "job not finished")
    stl_path = Path(job.result["stl_path"])  # absolute, see pipeline.py's run_stl_pipeline
    if not stl_path.exists():
        raise HTTPException(404, "STL file not found on disk")
    return FileResponse(stl_path, filename=stl_path.name, media_type="application/octet-stream")
