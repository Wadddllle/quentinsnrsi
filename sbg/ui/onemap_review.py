"""OneMap review-gate (Phase 4c of the project plan): propose -> numeric
sanity check -> contextual 3D preview -> manual nudge -> explicit approve.
The real implementation of onemap_landmarks/NOTES.md's proposed fix for the
6 reverted Phase 1.6 landmark-mesh incidents (Esplanade/Indoor Stadium/
Flower Dome/etc. -- floating domes, overlapping structures, 70-300m
placement offsets, all shipped and caught only by an isolated-render blind
spot). Nothing here writes to `cm` until approve; propose/nudge only ever
touch this module's own in-memory proposal store.

Kept deliberately separate from sbg/onemap/embed.py -- that module owns the
mechanics of turning a trimesh.Trimesh into CityJSON geometry (reused
unmodified here and by Phase 4b's mesh-import path); this module owns the
review workflow (what gets proposed, checked, previewed, and approved) on
top of it.
"""
import time
import uuid
from pathlib import Path

import numpy as np

from sbg.config import DATA_DIR
from sbg.io_cityjson import AppendOnlyVertexPool
from sbg.onemap.mesh import extract_building_mesh
from sbg.onemap.embed import embed_mesh_as_new_object, mesh_to_multisurface

REVIEW_LOG_PATH = DATA_DIR / "onemap_review_log.jsonl"

# NOTES.md's own example heuristics, made real and configurable here rather
# than hardcoded inline -- these are advisory (they add friction/flag a
# proposal, never hard-block an approve) per the plan's own framing: a
# scientist may have real-world reasons an edge case is still correct.
CENTROID_OFFSET_WARN_M = 30.0
AREA_RATIO_WARN_LOW = 0.3
AREA_RATIO_WARN_HIGH = 3.0
HEIGHT_RATIO_WARN_LOW = 0.5
HEIGHT_RATIO_WARN_HIGH = 2.0

# How many of a building's ~5.6-average duplicate tile references to try
# before giving up on it -- extract_building_mesh's own self-validation
# guard rejects a real fraction of individual tile extractions (Draco
# _BATCHID quantization imprecision, not fully root-caused, see mesh.py), so
# a single-tile attempt would under-report real availability. Bounded so one
# bad candidate can't stall a propose job trying all ~5-6 duplicates.
MAX_TILE_ATTEMPTS = 4

# In-memory only, matching this project's established single-instance/
# no-concurrency assumption (same as sbg.ui.jobs' own job store). Keyed by
# proposal_id (== the job id that created it, one less concept to track).
_proposals = {}  # proposal_id -> {"old_building_ids": [...], "candidates": {gml_id: _CandidateState}}


class _CandidateState:
    __slots__ = ("gml_id", "original_mesh", "centroid_xy", "tile_used", "record", "dx", "dy", "dz", "rotation_deg")

    def __init__(self, gml_id, mesh, tile_used, record):
        self.gml_id = gml_id
        self.original_mesh = mesh  # never mutated -- nudge always re-derives from this
        self.centroid_xy = mesh.vertices[:, :2].mean(axis=0)
        self.tile_used = tile_used
        self.record = record  # the candidates.py dict (name/height/storeys/x/y/lat/lng)
        self.dx = self.dy = self.dz = self.rotation_deg = 0.0

    def current_mesh(self):
        """Original mesh with the cumulative nudge transform applied --
        rotate around the ORIGINAL centroid (not wherever it's drifted to),
        same convention sbg/ui/routers/buildings.py's import-mesh endpoint
        already uses, so repeated nudges stay predictable instead of
        compounding around a moving pivot."""
        verts = self.original_mesh.vertices.copy()
        if self.rotation_deg:
            theta = np.radians(self.rotation_deg)
            cos_t, sin_t = np.cos(theta), np.sin(theta)
            rel = verts[:, :2] - self.centroid_xy
            verts[:, :2] = np.column_stack([
                rel[:, 0] * cos_t - rel[:, 1] * sin_t,
                rel[:, 0] * sin_t + rel[:, 1] * cos_t,
            ]) + self.centroid_xy
        verts[:, 0] += self.dx
        verts[:, 1] += self.dy
        verts[:, 2] += self.dz
        mesh = self.original_mesh.copy()
        mesh.vertices = verts
        return mesh


def sanity_check(old_footprint_polygon, old_height, mesh, ref_xy):
    """The exact check that caught all 6 original Phase 1.6 incidents (real
    offsets measured then: 10-297m) -- footprint-area ratio, height ratio,
    and centroid offset against OneMap's OWN independent lat/lng reference
    point for this gml_id (not derived from our own extraction/reprojection
    pipeline, so it can't agree with a placement bug by construction).
    """
    verts = mesh.vertices
    mesh_area = (verts[:, 0].max() - verts[:, 0].min()) * (verts[:, 1].max() - verts[:, 1].min())
    mesh_height = float(verts[:, 2].max() - verts[:, 2].min())
    mesh_centroid = verts[:, :2].mean(axis=0)

    old_area = old_footprint_polygon.area if old_footprint_polygon is not None else None
    old_centroid = old_footprint_polygon.centroid if old_footprint_polygon is not None else None

    area_ratio = (mesh_area / old_area) if old_area else None
    height_ratio = (mesh_height / old_height) if old_height else None
    # Signed vector (ref - mesh), not just the magnitude -- lets the frontend
    # offer a real "close the gap" nudge button instead of a dead label (a
    # real user complaint: "what does the position offset button even do,
    # it just says position offset but doesn't do anything"). Computed
    # against whatever the mesh's CURRENT position is (post any prior
    # nudge), so adding this delta to the cumulative nudge closes the
    # CURRENT residual gap exactly, not the original one.
    centroid_offset_dx = centroid_offset_dy = None
    centroid_offset_m = None
    if ref_xy is not None:
        centroid_offset_dx = float(ref_xy[0] - mesh_centroid[0])
        centroid_offset_dy = float(ref_xy[1] - mesh_centroid[1])
        centroid_offset_m = float(np.hypot(centroid_offset_dx, centroid_offset_dy))
    old_vs_ref_offset_m = (
        float(np.hypot(old_centroid.x - ref_xy[0], old_centroid.y - ref_xy[1]))
        if old_centroid is not None and ref_xy is not None else None
    )

    flags = []
    if centroid_offset_m is not None and centroid_offset_m > CENTROID_OFFSET_WARN_M:
        flags.append("centroid_offset_high")
    if area_ratio is not None and not (AREA_RATIO_WARN_LOW <= area_ratio <= AREA_RATIO_WARN_HIGH):
        flags.append("area_ratio_off")
    if height_ratio is not None and not (HEIGHT_RATIO_WARN_LOW <= height_ratio <= HEIGHT_RATIO_WARN_HIGH):
        flags.append("height_ratio_off")

    return {
        "mesh_area_m2": round(mesh_area, 1),
        "mesh_height_m": round(mesh_height, 2),
        "old_area_m2": round(old_area, 1) if old_area else None,
        "old_height_m": round(old_height, 2) if old_height else None,
        "area_ratio": round(area_ratio, 3) if area_ratio is not None else None,
        "height_ratio": round(height_ratio, 3) if height_ratio is not None else None,
        "centroid_offset_m": round(centroid_offset_m, 1) if centroid_offset_m is not None else None,
        "centroid_offset_dx": round(centroid_offset_dx, 2) if centroid_offset_dx is not None else None,
        "centroid_offset_dy": round(centroid_offset_dy, 2) if centroid_offset_dy is not None else None,
        "old_footprint_vs_reference_offset_m": round(old_vs_ref_offset_m, 1) if old_vs_ref_offset_m is not None else None,
        "flags": flags,
    }


def _old_baseline(old_building_ids, cm, spatial_index):
    """(old_footprint, old_height) for whatever's currently in
    old_building_ids -- shared by run_propose_job, nudge_candidate, and
    update_old_buildings (the pooling endpoint), all of which need to
    recompute this same "what are we comparing the new mesh(es) against"
    baseline whenever the old-building set changes.
    """
    old_polys = []
    old_heights = []
    for oid in old_building_ids:
        poly = spatial_index.polygon_by_id.get(oid)
        if poly is not None:
            old_polys.append(poly)
        obj = cm["CityObjects"].get(oid)
        if obj is not None and obj["attributes"].get("height"):
            old_heights.append(obj["attributes"]["height"])

    old_footprint = None
    if old_polys:
        from shapely.ops import unary_union
        old_footprint = unary_union(old_polys) if len(old_polys) > 1 else old_polys[0]
    old_height = max(old_heights) if old_heights else None
    return old_footprint, old_height


def _footprint_rings(footprint):
    """Exterior-only rings (matches SpatialIndex._make_record's own
    convention) for the frontend's contextual outline overlay."""
    if footprint is None:
        return []
    geoms = [footprint] if footprint.geom_type == "Polygon" else list(footprint.geoms)
    return [list(g.exterior.coords) for g in geoms]


def _extract_with_retry(gml_id, tiles):
    """Tries each of a building's known tile references in turn -- see
    MAX_TILE_ATTEMPTS. Returns (mesh, tile_used, attempted, last_error) --
    mesh is None if every attempt failed."""
    attempted = 0
    last_error = None
    for tile in tiles[:MAX_TILE_ATTEMPTS]:
        attempted += 1
        try:
            mesh = extract_building_mesh(tile, gml_id)
        except Exception as e:
            last_error = str(e)
            continue
        if mesh is not None and len(mesh.vertices) > 0:
            return mesh, tile, attempted, None
        last_error = "no usable mesh in this tile (isolation failed or self-validation rejected it)"
    return None, None, attempted, last_error


def run_propose_job(job, cm, spatial_index, old_building_ids, candidate_records):
    """The async job body (see sbg.ui.jobs.create_job) -- fetches+decodes+
    repairs a mesh for each candidate (real network I/O, real per-building
    CPU cost, correctly NOT a synchronous endpoint -- this project has twice
    already had to retrofit the job/polling pattern after wrongly assuming a
    heavy endpoint was fast enough, see project plan). candidate_records:
    the exact dicts candidates.py's find_candidates_* already returned to
    the client and the client is handing back (name/height/storeys/x/y/
    lat/lng/tiles) -- server re-derives nothing from a bare id list, so a
    stale/tampered gml_id can't silently pull the wrong tile set.
    """
    old_footprint, old_height = _old_baseline(old_building_ids, cm, spatial_index)

    job.set_stage("fetching_candidates")
    proposal_id = job.id
    candidates_state = {}

    # Fetched concurrently, not serially -- each candidate is an independent
    # real network fetch (up to MAX_TILE_ATTEMPTS tiles, each up to 120s) +
    # Draco decode, so a serial loop meant total wait time was the SUM across
    # every selected candidate. A real user report ("stuck on fetching unless
    # I uncheck the building") traced to exactly this: with several
    # candidates selected (common -- OneMap's own tile-tree duplication means
    # a building often has 2-6 real gml_id entries nearby), one slow/failing
    # candidate stalled the whole batch behind it. Concurrency bounds the
    # wait by the SLOWEST candidate instead.
    def _fetch_one(rec):
        gml_id = rec["gml_id"]
        job.log_line(f"fetching {gml_id} ({rec.get('name') or 'unnamed'})...")
        mesh, tile_used, attempted, error = _extract_with_retry(gml_id, rec.get("tiles") or [])
        if mesh is None:
            job.log_line(f"  FAILED after {attempted} tile(s): {error}")
            return rec, None, {
                "gml_id": gml_id, "name": rec.get("name"), "status": "failed",
                "attempted_tiles": attempted, "error": error,
            }
        sanity = sanity_check(old_footprint, old_height, mesh, (rec["x"], rec["y"]))
        job.log_line(f"  OK via {tile_used} ({len(mesh.vertices)} verts, {len(mesh.faces)} faces) -- flags: {sanity['flags'] or 'none'}")
        return rec, (mesh, tile_used), {
            "gml_id": gml_id, "name": rec.get("name"), "status": "ok",
            "tile_used": tile_used, "attempted_tiles": attempted,
            "vertex_count": len(mesh.vertices), "face_count": len(mesh.faces),
            "sanity": sanity,
        }

    from concurrent.futures import ThreadPoolExecutor
    results_by_gml_id = {}
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(candidate_records)))) as pool:
        futures = [pool.submit(_fetch_one, rec) for rec in candidate_records]
        for fut in futures:
            rec, mesh_info, result = fut.result()
            results_by_gml_id[rec["gml_id"]] = result
            if mesh_info is not None:
                mesh, tile_used = mesh_info
                candidates_state[rec["gml_id"]] = _CandidateState(rec["gml_id"], mesh, tile_used, rec)

    # Preserve the order candidates were requested in, not futures-completion order.
    results = [results_by_gml_id[rec["gml_id"]] for rec in candidate_records]

    _proposals[proposal_id] = {"old_building_ids": list(old_building_ids), "candidates": candidates_state}
    job.set_stage("done")
    return {
        "proposal_id": proposal_id, "old_building_ids": list(old_building_ids),
        "old_footprint_rings": _footprint_rings(old_footprint), "candidates": results,
    }


def get_proposal(proposal_id):
    return _proposals.get(proposal_id)


def nudge_candidate(proposal_id, gml_id, dx, dy, dz, rotation_deg, cm, spatial_index):
    """Applies an ABSOLUTE cumulative transform (not a delta -- see
    _CandidateState.current_mesh) and recomputes the sanity check against
    the same old-footprint/reference-point baseline the original propose
    used, so the live-updating number the plan asked for ("adjust by eye,
    watch the number that caught the original problems move in real time")
    is directly comparable to the pre-nudge one.
    """
    proposal = _proposals.get(proposal_id)
    if proposal is None:
        return None
    state = proposal["candidates"].get(gml_id)
    if state is None:
        return None

    state.dx, state.dy, state.dz, state.rotation_deg = dx, dy, dz, rotation_deg
    mesh = state.current_mesh()

    old_footprint, old_height = _old_baseline(proposal["old_building_ids"], cm, spatial_index)
    return sanity_check(old_footprint, old_height, mesh, (state.record["x"], state.record["y"]))


def update_old_buildings(proposal_id, new_old_building_ids, cm, spatial_index):
    """Widens (or narrows) the old-building side of an already-proposed
    replacement -- the "I realized the fetched mesh also covers a couple of
    adjacent buildings, pool them in" case. Deliberately does NOT re-fetch
    or re-decode any candidate mesh (those don't change) -- just updates
    what they're being compared against, recomputing sanity_check for every
    already-fetched candidate against the new baseline via the same
    _old_baseline() helper propose/nudge already use, and returning a fresh
    outline for the frontend's contextual overlay. new_old_building_ids is
    the FULL replacement list (not a delta) -- simpler for the frontend to
    resend its whole current set on every add/remove than to reason about
    incremental diffs server-side.
    """
    proposal = _proposals.get(proposal_id)
    if proposal is None:
        return None
    proposal["old_building_ids"] = list(new_old_building_ids)

    old_footprint, old_height = _old_baseline(proposal["old_building_ids"], cm, spatial_index)
    results = []
    for gml_id, state in proposal["candidates"].items():
        mesh = state.current_mesh()
        sanity = sanity_check(old_footprint, old_height, mesh, (state.record["x"], state.record["y"]))
        results.append({"gml_id": gml_id, "sanity": sanity})

    return {
        "old_building_ids": proposal["old_building_ids"],
        "old_footprint_rings": _footprint_rings(old_footprint),
        "candidates": results,
    }


def build_preview_citymodel(cm, proposal_id, gml_ids=None):
    """A small, self-contained mini-CityJSON ({CityObjects, vertices,
    transform}) covering the CURRENT (post-nudge) state of the given
    proposal's candidate meshes -- same shape sbg.io_cityjson.subset_cityjson
    produces, so the frontend can render it through the exact scoped
    CityJSONLoader mechanism already built for Phase 4b's added-buildings
    overlay (ThreeJsViewer.vue's updateAddedBuildings), rather than a new
    rendering path. Reuses cm["transform"] (not a fresh one centered on
    just these meshes) so the result lands in the SAME local scene frame as
    the real, already-loaded whole-island geometry -- required for the
    contextual "old footprint outline + real neighbors + proposed mesh, all
    together" view the plan calls for; an isolated transform would silently
    misplace it relative to everything else, the identical bug class this
    whole feature exists to prevent falling into again.
    """
    proposal = _proposals.get(proposal_id)
    if proposal is None:
        return None

    # AppendOnlyVertexPool only ever needs a dict with "vertices" (mutated
    # via .append) and "transform" -- a scratch dict with a FRESH empty
    # vertex list (never the real cm["vertices"]) means this preview can
    # never touch the actual working dataset, while still reusing cm's real
    # transform (scale/translate) so the result lands in the same local
    # scene frame as everything else already loaded -- see this function's
    # own docstring for why that alignment matters.
    scratch = {"vertices": [], "transform": cm["transform"]}
    pool = AppendOnlyVertexPool(scratch)

    new_cityobjects = {}
    wanted = gml_ids if gml_ids is not None else list(proposal["candidates"].keys())
    for gml_id in wanted:
        state = proposal["candidates"].get(gml_id)
        if state is None:
            continue
        mesh = state.current_mesh()
        geometry = mesh_to_multisurface(pool, mesh)
        new_cityobjects[f"proposal/{gml_id}"] = {
            "type": "Building",
            "attributes": {"onemap_gml_id": gml_id, "name": state.record.get("name")},
            "geometry": [geometry],
        }

    return {
        "type": "CityJSON",
        "version": cm["version"],
        "transform": cm["transform"],
        "metadata": dict(cm.get("metadata", {})),
        "CityObjects": new_cityobjects,
        "vertices": scratch["vertices"],
    }


def apply_approval(cm, session, spatial_index, proposal_id, approved_gml_ids):
    """The only path that mutates the real working copy -- removes every
    old_building_id (atomically undoable, same as a normal Remove) and adds
    one new CityObject per approved candidate (its CURRENT, i.e. post-nudge,
    mesh) via embed_mesh_as_new_object -- both pushed as ONE
    Session.record_group() action, so a single Undo reverses the whole
    replace, matching Phase 4b's own multi-object-import precedent.
    """
    proposal = _proposals.get(proposal_id)
    if proposal is None:
        raise ValueError(f"No such proposal: {proposal_id!r}")

    entries = []
    for oid in proposal["old_building_ids"]:
        if oid not in cm["CityObjects"]:
            continue
        snapshot = cm["CityObjects"][oid]
        del cm["CityObjects"][oid]
        spatial_index.mark_removed(oid)
        entries.append(("remove", oid, snapshot))

    pool = AppendOnlyVertexPool(cm)
    new_ids = []
    for gml_id in approved_gml_ids:
        state = proposal["candidates"].get(gml_id)
        if state is None:
            continue
        mesh = state.current_mesh()
        rec = state.record
        attrs = {
            "height_source": "onemap_mesh",
            "onemap_gml_id": gml_id,
            "onemap_storeys": rec.get("storeys"),
        }
        name = rec.get("name")
        if name:
            attrs["onemap_name"] = name
        obj_id = embed_mesh_as_new_object(cm, pool, mesh, attrs)
        spatial_index.mark_added(obj_id)
        entries.append(("add", obj_id, None))
        new_ids.append(obj_id)

    session.record_group(entries)
    del _proposals[proposal_id]
    return new_ids


def log_review_decision(proposal_id, decision, note, old_building_ids, gml_ids):
    """Cheap audit trail -- NOTES.md's own reviewed_replacements.json idea,
    real implementation of it. Not load-bearing (nothing reads this back
    programmatically), just a durable record of what was reviewed and why."""
    REVIEW_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    import orjson
    entry = {
        "proposal_id": proposal_id, "decision": decision, "note": note,
        "old_building_ids": old_building_ids, "gml_ids": gml_ids,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with open(REVIEW_LOG_PATH, "ab") as f:
        f.write(orjson.dumps(entry) + b"\n")


def discard_proposal(proposal_id):
    _proposals.pop(proposal_id, None)
