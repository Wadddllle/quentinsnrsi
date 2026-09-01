"""FastAPI app for the lean v2 tile-native STL tool.

Startup loads ONLY the sg_buildings_v5 footprint index (for the 2D basemap +
domain in/out preview) -- not the 1.3GB CityJSON the v1 UI loads. Endpoints:
  GET  /api/footprints          -- all-island footprints (+archetype) for the 2D map
  GET  /api/onemap/search       -- location search (reuses sbg.onemap.client)
  POST /api/domain/heights      -- ring/bbox -> building-height stats (buffer sizing)
  POST /api/domain/preview      -- ring/bbox [+wind] -> kept/crossing ids, stats,
                                   and the resolved buffer/envelope/rings to draw
  POST /api/stl/run             -- ring/bbox [+wind] -> {job_id}
  GET  /api/stl/jobs/{id}       -- job status/progress
  GET  /api/stl/jobs/{id}/download   -- the finished STL (first output)
  GET  /api/stl/jobs/{id}/files/{name} -- one named output (STL or .wind.json)
  GET  /api/stl/jobs/{id}/bundle -- every output as a zip

TWO POLYGONS, and the distinction is load-bearing: the ring the user draws is the
REGION OF INTEREST (it decides which buildings are kept, and is identical for every
wind direction so dose maps stay comparable). When wind is requested, the polygon that
is actually BUILT is the larger buffer envelope, and the buffer ring is bare terrain
with no buildings. So `preview`'s kept/crossing counts are always about the ROI, never
the envelope -- the UI must not imply buffer-region buildings are included.
"""
import io
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from shapely.geometry import Polygon, box

from sbg.config import DATA_DIR
from sbg.onemap.client import search_buildings
from sbg.onemap_native.ui.footprints import load_footprint_index
from sbg.onemap_native.ui.stl_job import run_stl_job
from sbg.onemap_native.ui.wind_plan import plan_wind
from sbg.ui.jobs import create_job, get_job
from sbg.ui.responses import orjson_response

WEBUI_DIST = Path(__file__).resolve().parents[3] / "webui-v2" / "dist"
STL_JOBS_DIR = DATA_DIR / "v2_stl_jobs"
# whitelist of build_domain_stl knobs a client may override (everything else uses
# the tuned defaults from build.py)
_BUILD_OPTS = {"step", "voxel_size", "target_reduction", "decimate_error", "workers",
               "include_base", "placement", "coupling_lambda", "fuse_backend",
               "crossing"}
_PLACEMENTS = ("group", "drape", "laplacian")
_CROSSING = ("auto", "drop", "keep")
_FUSE_BACKENDS = ("meshlib", "blender")
# Serving from a job dir means a client-supplied filename reaches the filesystem.
# Extensions the pipeline actually produces; everything else is refused outright.
_SERVABLE_SUFFIXES = (".stl", ".json")


def _domain_polygon(payload):
    """ring (>=3 [x,y] EPSG:3414) or bbox [xmin,ymin,xmax,ymax] -> shapely polygon.

    This is the REGION OF INTEREST, not necessarily what gets built -- see the module
    docstring. `roi_ring` is accepted as a clearer alias for `ring`.
    """
    ring = payload.get("roi_ring") or payload.get("ring")
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
    if poly.geom_type != "Polygon":
        raise HTTPException(400, "domain must be a single polygon (got "
                                 f"{poly.geom_type}); self-intersecting rings split "
                                 "into several parts when repaired")
    return poly


def _height_stats(poly):
    """OneMap building-height stats inside `poly`, or None if unavailable.

    NOT from the footprint index: that streams the raw sg_buildings_v5 geojson, where
    72.8% of heights are 0 (86,474 of 118,782) -- the OneMap backfill only ever landed
    in the v1 CityJSON. `heights.py` reads data/onemap_buildings.jsonl instead (146,645
    buildings, 0% zero heights). ~6 s cold, 0.7 ms per query after.
    """
    try:
        from sbg.onemap_native.heights import roi_height_stats
        return roi_height_stats(poly, log=lambda *_: None)
    except Exception as e:      # a missing/corrupt jsonl must not break the preview
        print(f"[v2] height index unavailable: {e}", flush=True)
        return None


def _plan(payload, roi):
    """Resolve payload['wind'] against the ROI, or (None, roi) when no wind is asked
    for. Returns (plan, build_polygon) -- the build polygon is the envelope when wind
    is in play and the ROI itself otherwise."""
    # `is None` and not falsiness: an EMPTY wind dict is a client that meant to ask for
    # wind and sent nothing usable. Silently treating it as "no wind" would ship a
    # single unrotated domain to someone who asked for a rose.
    if payload.get("wind") is None:
        return None, roi
    try:
        plan = plan_wind(payload["wind"], roi, _height_stats(roi))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return plan, plan["envelope"]


def _job_file(job_id, name):
    """Resolve a client-supplied filename inside a job dir, or 404.

    Never interpolate the client string into a path: take only the basename, resolve,
    and confirm the result is still inside the job directory.
    """
    job = get_job(job_id)
    if job is None:
        raise HTTPException(404, "unknown job")
    job_dir = (STL_JOBS_DIR / job_id).resolve()
    safe = Path(str(name)).name
    if not safe or safe.startswith(".") or not safe.endswith(_SERVABLE_SUFFIXES):
        raise HTTPException(404, "no such file")
    p = (job_dir / safe).resolve()
    if not p.is_relative_to(job_dir) or not p.is_file():
        raise HTTPException(404, "no such file")
    return p


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
        # `features` exists so a newer frontend can detect an older backend. The
        # server is started with plain uvicorn (no reload), so a Python change needs a
        # manual restart -- and an older build silently IGNORED an unknown `wind`
        # field rather than rejecting it, happily returning one axis-aligned domain to
        # a user who asked for eight rotated ones. Silent version skew, no error.
        return {"ok": True, "features": ["wind", "multi_output", "bundle"]}

    @app.get("/api/footprints")
    def footprints():
        return orjson_response({"footprints": app.state.footprints.all_records()})

    @app.get("/api/onemap/search")
    def search(q: str, max_results: int = 10):
        if not q or not q.strip():
            raise HTTPException(400, "q must not be empty")
        return {"results": search_buildings(q, max_results=max_results)}

    @app.post("/api/domain/heights")
    def heights(payload: dict = Body(...)):
        """Building-height stats for the ROI -- what sizes the buffer.

        Both `max` and `p90` are reported deliberately: one tall outlier doubles H and
        so ~2.2x's the domain area (measured on Kent Ridge -- max 100.2 m vs p90 50.0 m
        gives an 18.4 vs 8.5 km^2 envelope). The UI should show both, not silently pick.
        """
        stats = _height_stats(_domain_polygon(payload))
        if stats is None:
            raise HTTPException(503, "OneMap height index unavailable "
                                     "(data/onemap_buildings.jsonl missing?)")
        return stats

    @app.post("/api/domain/preview")
    def preview(payload: dict = Body(...)):
        roi = _domain_polygon(payload)
        idx = app.state.footprints
        # kept/crossing are ALWAYS about the ROI, never the buffered envelope -- the
        # buffer is bare terrain by design and contains no buildings at all.
        kept = idx.query_contained(roi)
        crossing = idx.query_intersects_not_contained(roi)
        out = {
            "kept": kept, "crossing": crossing,
            "kept_count": len(kept), "crossing_count": len(crossing),
            "area_km2": round(roi.area / 1e6, 4), "bounds": list(roi.bounds),
            "roi_ring": [[float(x), float(y)] for x, y in roi.exterior.coords],
        }
        plan, build_poly = _plan(payload, roi)
        if plan is not None:
            out["wind"] = {k: v for k, v in plan.items()
                           if k not in ("envelope", "build_kwargs")}
            out["build_area_km2"] = round(build_poly.area / 1e6, 4)
        return orjson_response(out)

    @app.post("/api/stl/run")
    def stl_run(payload: dict = Body(...)):
        roi = _domain_polygon(payload)
        # Drop None: a client that sends `"crossing": null` means "not specified",
        # and forwarding it would override the pipeline default with nothing. (A
        # frontend Number() slip on a string option produces exactly that.)
        opts = {k: v for k, v in (payload.get("options") or {}).items()
                if k in _BUILD_OPTS and v is not None}
        # placement/fuse_backend are the free-form strings a client can send -- validate
        # here rather than letting an unknown value reach the pipeline.
        if opts.get("placement") not in (None,) + _PLACEMENTS:
            raise HTTPException(400, f"placement must be one of {_PLACEMENTS}")
        if opts.get("fuse_backend") not in (None,) + _FUSE_BACKENDS:
            raise HTTPException(400, f"fuse_backend must be one of {_FUSE_BACKENDS}")
        if opts.get("crossing") not in (None,) + _CROSSING:
            raise HTTPException(400, f"crossing must be one of {_CROSSING}")

        # Re-resolved server-side from the same plan_wind() the preview used, so the
        # geometry cannot drift between what the user approved and what is built. The
        # UI should echo back the buffer_m the preview returned to freeze it exactly --
        # otherwise a height-index change between preview and submit could resize it.
        plan, build_poly = _plan(payload, roi)
        core = roi if plan is not None else None
        extra = (f"ROI {roi.area/1e6:.3f} km^2"
                 + (f" -> build envelope {build_poly.area/1e6:.3f} km^2, "
                    f"{len(plan['dirs'])} direction(s), buffer "
                    f"{plan['buffer_m']['upwind']:.0f}/{plan['buffer_m']['downwind']:.0f}/"
                    f"{plan['buffer_m']['lateral']:.0f} m" if plan else ""))
        job = create_job(run_stl_job, build_poly, STL_JOBS_DIR,
                         store_dir=app.state.store_dir, log_extra=extra,
                         core_polygon=core,
                         wind=plan["build_kwargs"] if plan else None, **opts)
        resp = {"job_id": job.id}
        if plan is not None:
            resp["wind"] = {k: v for k, v in plan.items()
                            if k not in ("envelope", "build_kwargs")}
        return orjson_response(resp)

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

    @app.get("/api/stl/jobs/{job_id}/files/{name}")
    def stl_file(job_id: str, name: str):
        p = _job_file(job_id, name)
        media = "model/stl" if p.suffix == ".stl" else "application/json"
        return FileResponse(str(p), media_type=media, filename=p.name)

    @app.post("/api/stl/jobs/{job_id}/cancel")
    def stl_cancel(job_id: str):
        """Ask a running job to stop at its next stage boundary.

        Cooperative, not immediate: the heavy stages are single calls into meshlib/OpenVDB
        that cannot be interrupted, so this can take seconds to a couple of minutes on a
        big domain. The UI says so rather than pretending the button is instant.
        """
        job = get_job(job_id)
        if job is None:
            raise HTTPException(404, "no such job")
        return {"cancelled": job.cancel(), "status": job.status}

    @app.get("/api/stl/jobs/{job_id}/bundle")
    def stl_bundle(job_id: str):
        """Every output plus a manifest, as one zip.

        Built lazily on request and streamed, never eagerly at job end: 16 x ~25 MB is
        ~400 MB, and eager zipping would double the wall clock for a user who only
        wanted one direction.
        """
        job = get_job(job_id)
        if job is None or job.status != "done" or not job.result:
            raise HTTPException(404, "job not finished")
        job_dir = (STL_JOBS_DIR / job_id).resolve()
        outputs = job.result.get("outputs") or [job.result]

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
            for o in outputs:
                stl = Path(o["stl_path"])
                if stl.is_file():
                    z.write(stl, stl.name)
                # A wind build writes <stem>.wind.json, any other build writes
                # <stem>.build.json. Ship whichever exists -- the STL alone carries no
                # metadata at all, so without its sidecar the domain, the options and
                # (for a wind run) the rotation are simply lost.
                for suf in (".wind.json", ".build.json"):
                    side = stl.with_suffix(suf)
                    if side.is_file():
                        z.write(side, side.name)
            import json
            z.writestr("manifest.json", json.dumps({
                "job_id": job_id,
                "crs": "EPSG:3414",
                "created_at": job.created_at,
                "count": len(outputs),
                "outputs": [{k: v for k, v in o.items() if k != "stl_path"}
                            for o in outputs],
                "note": (("Each wind_<bearing>.stl is ALREADY ROTATED so the flow is +Y "
                          "(inlet ymin, outlet ymax). Its wind_<bearing>.wind.json holds "
                          "the rotation back to true EPSG:3414 -- apply it only when "
                          "producing a georeferenced product, never before binning "
                          "particle tracks.")
                         if len(outputs) > 1 or job.result.get("wind") else
                         ("Single unrotated domain in true EPSG:3414. The .build.json "
                          "sidecar records the domain polygon, the build options and the "
                          "commit, so this mesh can be reproduced.")),
            }, indent=2))
        buf.seek(0)
        assert job_dir.exists()      # sanity: outputs were written where we think
        return StreamingResponse(
            buf, media_type="application/zip",
            headers={"Content-Disposition":
                     f'attachment; filename="cfd_domain_{job_id[:8]}.zip"'})

    if not dev and WEBUI_DIST.exists():
        app.mount("/", StaticFiles(directory=str(WEBUI_DIST), html=True), name="webui")

    return app
