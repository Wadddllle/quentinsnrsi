"""FastAPI app for the lean v2 tile-native STL tool.

Startup loads ONLY the sg_buildings_v5 footprint index (for the 2D basemap +
domain in/out preview) -- not the 1.3GB CityJSON the v1 UI loads. Endpoints:
  GET  /api/footprints          -- all-island footprints (+archetype) for the 2D map
  GET  /api/onemap/search       -- location search (reuses sbg.onemap.client)
  POST /api/domain/preview      -- ring/bbox -> kept/crossing building ids + stats
  POST /api/stl/run             -- ring/bbox -> {job_id}, runs onemap_native.build
  GET  /api/stl/jobs/{id}       -- job status/progress
  GET  /api/stl/jobs/{id}/download -- the finished STL
"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from shapely.geometry import Polygon, box

from sbg.config import DATA_DIR
from sbg.onemap.client import search_buildings
from sbg.onemap_native.ui.footprints import load_footprint_index
from sbg.onemap_native.ui.stl_job import run_stl_job
from sbg.ui.jobs import create_job, get_job
from sbg.ui.responses import orjson_response

WEBUI_DIST = Path(__file__).resolve().parents[3] / "webui-v2" / "dist"
STL_JOBS_DIR = DATA_DIR / "v2_stl_jobs"
# whitelist of build_domain_stl knobs a client may override (everything else uses
# the tuned defaults from build.py)
_BUILD_OPTS = {"step", "voxel_size", "target_reduction", "decimate_error", "workers"}


def _domain_polygon(payload):
    """ring (>=3 [x,y] EPSG:3414) or bbox [xmin,ymin,xmax,ymax] -> shapely polygon."""
    ring = payload.get("ring")
    bbox = payload.get("bbox")
    if ring and len(ring) >= 3:
        poly = Polygon([(float(p[0]), float(p[1])) for p in ring])
    elif bbox and len(bbox) == 4:
        poly = box(*(float(v) for v in bbox))
    else:
        raise HTTPException(400, "provide 'ring' (>=3 [x,y] points) or 'bbox' "
                                 "[xmin,ymin,xmax,ymax] in EPSG:3414")
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.is_empty or poly.area <= 0:
        raise HTTPException(400, "degenerate domain polygon")
    return poly


def create_app(store_dir=None, dev=False):
    @asynccontextmanager
    async def lifespan(app):
        app.state.footprints = load_footprint_index()
        app.state.store_dir = str(store_dir) if store_dir else None
        print(f"[v2] ready (store={'yes' if store_dir else 'live-fetch'})", flush=True)
        yield

    app = FastAPI(title="SBG v2 (tile-native STL)", lifespan=lifespan)
    if dev:
        app.add_middleware(CORSMiddleware, allow_origins=["*"],
                           allow_methods=["*"], allow_headers=["*"])

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/footprints")
    def footprints():
        return orjson_response({"footprints": app.state.footprints.all_records()})

    @app.get("/api/onemap/search")
    def search(q: str, max_results: int = 10):
        if not q or not q.strip():
            raise HTTPException(400, "q must not be empty")
        return {"results": search_buildings(q, max_results=max_results)}

    @app.post("/api/domain/preview")
    def preview(payload: dict = Body(...)):
        poly = _domain_polygon(payload)
        idx = app.state.footprints
        kept = idx.query_contained(poly)
        crossing = idx.query_intersects_not_contained(poly)
        return {
            "kept": kept, "crossing": crossing,
            "kept_count": len(kept), "crossing_count": len(crossing),
            "area_km2": round(poly.area / 1e6, 4), "bounds": list(poly.bounds),
        }

    @app.post("/api/stl/run")
    def stl_run(payload: dict = Body(...)):
        poly = _domain_polygon(payload)
        opts = {k: v for k, v in (payload.get("options") or {}).items() if k in _BUILD_OPTS}
        job = create_job(run_stl_job, poly, STL_JOBS_DIR,
                         store_dir=app.state.store_dir,
                         log_extra=f"domain area {poly.area/1e6:.3f} km^2", **opts)
        return {"job_id": job.id}

    @app.get("/api/stl/jobs/{job_id}")
    def stl_job(job_id: str):
        job = get_job(job_id)
        if job is None:
            raise HTTPException(404, "unknown job")
        return job.to_dict()

    @app.get("/api/stl/jobs/{job_id}/download")
    def stl_download(job_id: str):
        job = get_job(job_id)
        if job is None or job.status != "done" or not job.result:
            raise HTTPException(404, "job not finished")
        path = job.result["stl_path"]
        return FileResponse(path, media_type="model/stl",
                            filename=f"cfd_domain_{job_id[:8]}.stl")

    if not dev and WEBUI_DIST.exists():
        app.mount("/", StaticFiles(directory=str(WEBUI_DIST), html=True), name="webui")

    return app
