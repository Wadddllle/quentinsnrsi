# Deploying SBG

Three ways to run this somewhere other than your own dev box, roughly in order of effort.
All three start from the same [`Dockerfile`](Dockerfile) — read the "Running it" section of
[`README.md`](README.md) first if you haven't already; this doc only covers what's different
about *where* it runs.

---

## 1. Podman (a supervisor's PC, or anywhere without Docker)

Podman is a drop-in for Docker here — nothing in this repo needs changing. Modern Podman
(4.x+) ships `podman compose`, which transparently wraps the existing
[`docker-compose.yml`](docker-compose.yml):

```bash
podman compose up --build
# → http://localhost:8000
```

Same two bind-mounts as Docker (`sg_buildings_v5.geojson` at the repo root, `data/`
writable) — see README §"Runtime data" for what needs to be there first. If `podman
compose` isn't available on their install, `pip install podman-compose` gives the same
command, or just substitute `podman` for `docker` in every command in the README (`podman
build`, `podman run -p 8000:8000 -v ...`).

No `WITH_BLENDER` build-arg needed unless they specifically want `--fuse-backend blender` —
the default image runs both the raw and watertight paths via meshlib.

---

## 2. Google Cloud Run — a first-time walkthrough

Cloud Run runs a container per request, scaling to zero when idle — no server to manage, and
(within limits below) free. It has **no bind-mount concept** like `docker-compose.yml` uses,
so the runtime data has to be *in* the image instead. That's what the Dockerfile's `cloud-run`
build target is for.

### One-time setup

```bash
# Install the gcloud CLI if you don't have it, then:
gcloud auth login
gcloud config set project <your-project-id>
gcloud services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com

# A place to store the built image
gcloud artifacts repositories create sbg --repository-format=docker --location=us-central1
```

### Build and push

The `cloud-run` build target bakes the runtime data straight into the image (Cloud Run has
no bind-mount concept), so Cloud Run never needs to fetch OneMap tiles live or reproject
footprints at cold start. It reads from a `cloud_data/` staging folder rather than `data/`
directly — `.dockerignore` excludes `data/` and `*.geojson` everywhere else, and a parent
directory that's excluded can't be selectively un-excluded, so a separate staging dir sidesteps
that instead of fighting it:

```bash
mkdir -p cloud_data
cp sg_buildings_v5.geojson cloud_data/
cp data/dtm.tif cloud_data/
cp -r data/onemap_store cloud_data/

docker build --target cloud-run -t us-central1-docker.pkg.dev/<project-id>/sbg/sbg:cloud-run .
docker push us-central1-docker.pkg.dev/<project-id>/sbg/sbg:cloud-run
```

(No local Docker daemon? `gcloud builds submit --tag ... --config` can build it in the cloud
instead — more setup for a first attempt, so build locally and push if you can.)

### Deploy

```bash
gcloud run deploy sbg \
  --image us-central1-docker.pkg.dev/<project-id>/sbg/sbg:cloud-run \
  --region us-central1 \
  --memory 4Gi --cpu 4 \
  --no-cpu-throttling \
  --min-instances 0 --max-instances 1 \
  --concurrency 1 \
  --timeout 3600 \
  --allow-unauthenticated
```

Two of these flags are **required for correctness**, not just tuning — get them wrong and
the symptom is "builds randomly hang," not an error message:

- **`--no-cpu-throttling`.** This app runs STL builds on a background thread
  (`sbg/ui/jobs.py`'s `ThreadPoolExecutor`) while the browser polls
  `GET /api/stl/jobs/{id}` every ~2 s. Cloud Run's *default* billing mode only gives a
  container CPU while it's actively handling a request — between polls, a throttled instance's
  background thread gets starved and a build can stall for minutes instead of finishing in
  seconds. `--no-cpu-throttling` ("CPU is always allocated") is what makes the polling
  architecture actually work, and it's the setting that changes the cost math below.
- **`--concurrency 1`.** A real 8-direction wind build measured ~2.76 GB peak RSS on a domain
  this size (see the plan doc). Don't let Cloud Run route a second concurrent build into the
  same instance and blow past `--memory 4Gi`.

`--min-instances 0` is the actual free-tier lever (scale-to-zero when nobody's using it);
`--max-instances 1` caps runaway cost if this ever gets more traffic than expected.

### Is it actually free?

**Google Cloud Run has a genuinely perpetual free tier** (not a 90-day trial credit) that
resets every month:

| | free per month |
|---|---|
| vCPU-time | 180,000 vCPU-seconds |
| Memory | 360,000 GiB-seconds |
| Requests | 2,000,000 |
| Egress (N. America) | ~1 GB |

With `--no-cpu-throttling`, billing runs for the **whole time an instance stays warm**, not
just the ~40 s–2 min a build itself takes — Cloud Run keeps an instance alive for a few
minutes after the last request before scaling it back to zero, and that idle window is the
real cost driver.

**Worked estimate**, at 4 vCPU / 4 GiB and assuming a ~5-minute (300 s) warm window per visit:

```
4 vCPU × 300 s = 1,200 vCPU-seconds   \
4 GiB  × 300 s = 1,200 GiB-seconds     >  per "session" (one visit, however many polls it takes)
```

- vCPU budget: 180,000 / 1,200 ≈ **~150 sessions/month** before you'd owe anything (the
  binding constraint — memory's own budget allows ~300 sessions).
- For occasional use — a handful of domain builds a week, a supervisor checking in now and
  then — this comfortably stays free. Assume "free" for light/personal use, not
  unconditionally: dozens of sessions *per day* would cross it.
- Small, separate cost: the image itself lives in Artifact Registry, which has its own small
  free tier (0.5 GB storage). A ~3 GB image (with `onemap_store` baked in) will incur a
  couple of cents a month in storage — real, but trivially small, worth knowing about rather
  than being surprised by.

These are estimates to size expectations against, not a guarantee — Cloud Run's exact idle
timeout and billing granularity can change; check the Cloud Console's billing report after
your first week of real use rather than trusting the math above forever.

### Updating a deployed revision

Same build+push, then `gcloud run deploy sbg --image ...:cloud-run` again — Cloud Run creates
a new revision and shifts traffic over with no downtime.

---

## 3. Skipping `onemap_store` in the cloud image

The `cloud-run` Dockerfile target bakes in `onemap_store` (2.6 GB) because live OneMap tile
fetching (~80 s/domain) is slower and network-dependent, which compounds badly with the
CPU-throttling gotcha above if you forget `--no-cpu-throttling`. If image size or build-time
data prep is more of a constraint than build speed for your use case, skip the
`cp -r data/onemap_store cloud_data/` step, drop the `COPY cloud_data/onemap_store/` line
from the Dockerfile's `cloud-run` stage, and drop the `--store` flag from its `CMD` — the app
falls back to live tile fetching with no other changes needed.

---

## Deferred: dropping `sg_buildings_v5.geojson` entirely

`sg_buildings_v5.geojson` (135 MB, an external NUS UAL dataset) is the one remaining runtime
dependency that isn't OneMap's own data, and it exists purely to drive the 2D map's footprint
outlines and click-to-select/highlight behavior — not for any semantic backfill (the app
already doesn't rely on OSM's `building_archetype`/height tags for anything load-bearing).

It's tempting to replace it with something derived entirely from OneMap: `data/onemap_buildings.jsonl`
(already required for the wind-buffer feature) has per-building attributes, but only a
**point** (lat/lng) per building, not a footprint polygon — so it can't drive the 2D
selection UI as-is, which needs real vector outlines to test "is this building inside the
drawn boundary."

The real fix would be a new **island-wide footprint-ring extraction pass**: the per-domain
footprint clustering that already exists in `sbg/onemap_native/extract.py` (used at STL-build
time, scoped to one domain) run once over all ~24,644 OneMap tiles, producing footprint
polygons for every building the same way `onemap_store`'s precompute already walks the whole
tileset. That's a real, well-scoped project — same cost class as building `onemap_store` — but
a separate one from deployment hardening, and it's deliberately not attempted here. Worth
doing later specifically because it would drop the one external, non-OneMap data dependency
this app has, which matters most for the local-hosting story (one less file someone has to
go find and download correctly before anything works).
