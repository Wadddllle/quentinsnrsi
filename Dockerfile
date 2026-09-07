# SBG v2 -- tile-native OneMap -> CFD STL, as one sendable image.
#
#   docker build -t sbg .                              # slim, raw path only (default)
#   docker build -t sbg --build-arg WITH_BLENDER=1 .    # + Blender, watertight path too
#
# meshlib is the default fuse backend now (see CLAUDE.md) -- Blender is only an
# escape hatch for `--fuse-backend blender`, so it defaults OFF here.
#
# Runtime data is NOT baked into this (the `runtime` target, i.e. plain `docker
# build .`): mount sg_buildings_v5.geojson and data/ as volumes, see
# docker-compose.yml. Tiles are fetched live from OneMap by default, so no 2.6GB
# store is required for local use. For Cloud Run (no bind mounts available), use
# the `cloud-run` target below instead, which bakes the data in.

# ---------- stage 1: build the frontend ----------
FROM node:20-slim AS webui
WORKDIR /build
COPY webui-v2/package.json webui-v2/package-lock.json ./
RUN npm ci
COPY webui-v2/ ./
RUN npm run build          # -> /build/dist

# ---------- stage 2: runtime ----------
FROM python:3.12-slim AS runtime

# Blender is needed ONLY by the watertight path (voxel remesh), and only if you
# want `--fuse-backend blender` specifically -- the default fuse backend is
# meshlib, which needs no Blender at all (see CLAUDE.md). Off by default for a
# smaller image; set 1 to also support the blender fuse backend.
ARG WITH_BLENDER=0
ARG BLENDER_VERSION=4.5.11
ARG BLENDER_SERIES=4.5

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SBG_BLENDER_PATH=/opt/blender/blender

# Blender's headless build still links X11/GL/audio libs even with --background.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl xz-utils \
        $( [ "$WITH_BLENDER" = "1" ] && echo \
           libx11-6 libxi6 libxxf86vm1 libxfixes3 libxrender1 libxkbcommon0 \
           libsm6 libice6 libgl1 libegl1 libglib2.0-0 ) \
    && rm -rf /var/lib/apt/lists/*

RUN if [ "$WITH_BLENDER" = "1" ]; then \
        curl -fsSL "https://download.blender.org/release/Blender${BLENDER_SERIES}/blender-${BLENDER_VERSION}-linux-x64.tar.xz" \
          | tar -xJ -C /opt && mv "/opt/blender-${BLENDER_VERSION}-linux-x64" /opt/blender && \
        /opt/blender/blender --version; \
    fi

WORKDIR /app
COPY requirements-v2.txt ./
RUN pip install --no-cache-dir -r requirements-v2.txt

COPY sbg/ ./sbg/
COPY --from=webui /build/dist ./webui-v2/dist

# sbg.config derives paths from the repo root (parent of sbg/), so these are the
# two runtime files to mount. data/ also receives job output + the footprint cache.
VOLUME ["/app/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=4).status==200 else 1)"

# --no-browser: nothing to open inside a container. 0.0.0.0 so the port maps out.
CMD ["python", "-m", "sbg.onemap_native.ui", \
     "--host", "0.0.0.0", "--port", "8000", "--no-browser"]

# ---------- stage 3: cloud-run (data baked in, no bind mounts) ----------
# Cloud Run has no docker-compose-style volume mount, so this target bakes the
# runtime data straight into the image instead of relying on a mount. Bundles
# onemap_store too: live tile fetch is ~80s/domain vs ~0.2s from the store, and
# that gap matters more under Cloud Run's request-based CPU throttling (see
# DEPLOY.md) than it does on a machine you leave running.
#
# Build context note: `.dockerignore` excludes `data/` and `*.geojson`
# everywhere (they're mounted, not baked in, for the default `runtime` target)
# -- and a parent directory that's ignored can't be selectively un-ignored. So
# this target reads from a separate `cloud_data/` staging dir instead (see
# DEPLOY.md), not from `data/`/the repo root directly:
#
#   mkdir -p cloud_data
#   cp sg_buildings_v5.geojson cloud_data/
#   cp data/dtm.tif cloud_data/
#   cp -r data/onemap_store cloud_data/
#   docker build --target cloud-run -t sbg:cloud-run .
#
# Adds ~2.77GB over the `runtime`/default target -- only this target pays for it.
FROM runtime AS cloud-run
COPY cloud_data/sg_buildings_v5.geojson ./sg_buildings_v5.geojson
COPY cloud_data/dtm.tif ./data/dtm.tif
COPY cloud_data/onemap_store/ ./data/onemap_store/
# Pre-warm footprint_index_cache.pkl at build time so a cold start never pays the
# ~22s reprojection cost -- the whole point of scale-to-zero is that this cost
# would otherwise be paid on every cold start, not just once.
RUN python -c "from sbg.onemap_native.ui.footprints import load_footprint_index; load_footprint_index()"

CMD ["python", "-m", "sbg.onemap_native.ui", \
     "--host", "0.0.0.0", "--port", "8000", "--no-browser", \
     "--store", "data/onemap_store"]
