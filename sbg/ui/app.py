"""FastAPI app factory for the SBG local-first web UI (Phase 6 of the
project plan). Wraps existing sbg/ package functions as HTTP endpoints --
no pipeline logic lives here, only thin routers.

In production use (python -m sbg.ui), serves the built webui/dist/ bundle
directly via StaticFiles, so the whole tool is "run one command, open a
browser tab" with no separate dev server needed. The two-process Vite dev
server + CORS setup below is for active frontend development only.
"""
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path

import orjson
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from sbg.config import SBG_OUTPUT
from sbg.io_cityjson import subset_cityjson
from sbg.topo.island_terrain import build_masked_island_grid, pack_terrain_binary
from sbg.ui.routers import cutout, dataset, onemap, pipeline
from sbg.ui.spatial_index import build_index

WEBUI_DIST = Path(__file__).resolve().parent.parent.parent / "webui" / "dist"


def create_app(dev: bool = False, dataset_path=None) -> FastAPI:
    dataset_path = Path(dataset_path) if dataset_path else SBG_OUTPUT

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Loaded once at process startup, held in memory for the process
        # lifetime -- ~1.3GB RSS / ~16s for the full 118k-building island
        # dataset (measured), acceptable for a single-user local tool. Every
        # dataset/cutout endpoint reads app.state.cm / app.state.spatial_index
        # rather than touching disk per request.
        print(f"[sbg.ui] loading {dataset_path} ...")
        t0 = time.time()
        with open(dataset_path) as f:
            cm = json.load(f)
        load_elapsed = time.time() - t0
        index, index_elapsed = build_index(cm)
        app.state.cm = cm
        app.state.spatial_index = index
        app.state.dataset_path = dataset_path
        print(
            f"[sbg.ui] loaded {len(cm['CityObjects'])} CityObjects, "
            f"indexed {len(index.ids)} buildings "
            f"({load_elapsed:.1f}s load + {index_elapsed:.1f}s index)"
        )
        # Precomputed once here, not per-request: SbgViewer3D's full-island
        # 3D load (webui/src/composables/fullIslandData.js) always requests
        # the exact same fixed bbox, and /api/dataset/buildings used to
        # rebuild subset_cityjson() over all 118,780 buildings from scratch
        # on every single request -- measured at ~13s server-side, paid
        # again on every page load/reload, entirely avoidable since the
        # dataset is static for the process's lifetime. Doing it once here
        # means it's paid during server startup (before the app accepts any
        # requests at all -- ASGI lifespan blocks traffic until this
        # completes) rather than blocking the user's first "load 3D" fetch.
        t1 = time.time()
        app.state.full_island_buildings_body = orjson.dumps(subset_cityjson(cm, index.ids))
        print(f"[sbg.ui] precomputed full-island buildings response ({time.time() - t1:.1f}s)")

        # Prototype: coarse whole-island terrain for 3D visual context only
        # (not part of the SBG dataset, not used by any CFD-facing pipeline).
        # Precomputed once here for the same reason as the buildings body
        # above -- a fixed, static payload, no reason to rebuild per request.
        # Requires data/dtm.tif to already exist (`python -m sbg.topo.dtm`).
        t2 = time.time()
        try:
            grid_z, transform = build_masked_island_grid(cm)
            app.state.island_terrain_body = pack_terrain_binary(grid_z, transform)
            print(f"[sbg.ui] precomputed island terrain mesh ({time.time() - t2:.1f}s, {len(app.state.island_terrain_body) / 1e6:.1f}MB)")
        except FileNotFoundError:
            app.state.island_terrain_body = None
            print("[sbg.ui] data/dtm.tif not found, skipping island terrain (run `python -m sbg.topo.dtm` to enable)")

        yield

    app = FastAPI(title="SBG UI", lifespan=lifespan)

    if dev:
        # Vite's dev server runs on its own port (5173+) during frontend
        # development; CORS lets it call this API directly instead of
        # needing a proxy.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    app.include_router(dataset.router)
    app.include_router(cutout.router)
    app.include_router(onemap.router)
    app.include_router(pipeline.router)

    if not dev and WEBUI_DIST.is_dir():
        app.mount("/", StaticFiles(directory=WEBUI_DIST, html=True), name="webui")

    return app
