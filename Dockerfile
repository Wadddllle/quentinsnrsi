# SBG v2 -- tile-native OneMap -> CFD STL, as one sendable image.
#
#   docker build -t sbg .                       # with Blender (watertight path works)
#   docker build -t sbg --build-arg WITH_BLENDER=0 .   # slim, raw path only
#
# Runtime data is NOT baked in (see README "Running in Docker"): mount
# sg_buildings_v5.geojson and data/ as volumes. Tiles are fetched live from
# OneMap by default, so no 2.6GB store or 113GB archive is required.

# ---------- stage 1: build the frontend ----------
FROM node:20-slim AS webui
WORKDIR /build
COPY webui-v2/package.json webui-v2/package-lock.json ./
RUN npm ci
COPY webui-v2/ ./
RUN npm run build          # -> /build/dist

# ---------- stage 2: runtime ----------
FROM python:3.12-slim AS runtime

# Blender is needed ONLY by the watertight path (voxel remesh). Set 0 for a much
# smaller image that can still run --voxel-size 0 (raw), which is what the Ansys
# fault-tolerant workflow consumes.
ARG WITH_BLENDER=1
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
