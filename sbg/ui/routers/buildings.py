"""POST /api/buildings/* -- geometry-mutating operations in the live UI
(Phase 3: remove-building + versioning; Phase 4b: add-building, three ways).
Wraps sbg.edit.add_building/remove_building directly (no pipeline logic
duplicated here) and layers session/undo bookkeeping + spatial-index
invalidation on top, both of which are sbg/ui/-only concerns -- sbg/edit.py
itself is untouched.

remove_building(cm, id, compact=False) is called with compact ALWAYS False
here -- compaction is O(all vertices) and would make undo lossy/expensive
(see sbg/ui/session.py's module docstring); it's deliberately not something
this interactive session ever triggers.

Deliberately separate from sbg/ui/routers/attributes.py -- that router stays
scoped to metadata-only edits (never touches `geometry`), this one is
scoped to operations that create/destroy whole CityObjects. Matches the
"change geometry vs change metadata" split the frontend UI itself makes.
"""
import json
import os
import tempfile
import uuid
from pathlib import Path as PathlibPath
from typing import List, Optional, Tuple

import numpy as np
import trimesh
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel
from pyproj import Transformer

from sbg.config import CITYJSON_REFERENCE_SYSTEM, TARGET_CRS
from sbg.edit import add_building, remove_building
from sbg.io_cityjson import AppendOnlyVertexPool, remap_vertex_indices, subset_cityjson, vertex_lookup, walk_vertex_indices
from sbg.onemap.backfill import _regenerate_height, compute_vertex_refcounts
from sbg.onemap.embed import embed_mesh_as_new_object
from sbg.ui.responses import orjson_response

router = APIRouter(prefix="/api/buildings", tags=["buildings"])


@router.get("/geometry")
def get_geometry(ids: str, request: Request):
    """Real (not approximated) geometry for specific building ids, as a
    small self-contained mini-CityJSON -- same subset_cityjson() shape the
    bbox-scoped /api/dataset/buildings endpoint already returns, just keyed
    by explicit ids instead of a bbox. Exists for one reason: the 3D view's
    added-buildings overlay used to approximate every add as a flat-topped
    box extruded from footprint+height, which is exactly right for Path 1
    (that box IS the real geometry) but visibly wrong for Path 2/3 (a real
    imported CityJSON building or an uploaded mesh has actual wall/roof
    shape a box can't represent) -- direct user report. The frontend fetches
    this for just the ids it doesn't already have real geometry for and
    renders it through a second, small, synchronous CityJSONLoader instance
    (see SbgViewer3D.vue/ThreeJsViewer.vue) rather than hand-rolling
    triangulation of arbitrary Solid/CompositeSolid/MultiSurface geometry
    here -- subset_cityjson()'s output is already exactly what that loader
    expects.
    """
    cm = request.app.state.cm
    requested = [i for i in ids.split(",") if i]
    found = [i for i in requested if i in cm["CityObjects"]]
    if not found:
        return orjson_response({
            "type": "CityJSON", "version": cm["version"], "transform": cm["transform"],
            "metadata": dict(cm.get("metadata", {})), "CityObjects": {}, "vertices": [],
        })
    return orjson_response(subset_cityjson(cm, found))


@router.post("/{building_id:path}/remove")
def remove(building_id: str, request: Request):
    cm = request.app.state.cm
    if building_id not in cm["CityObjects"]:
        raise HTTPException(404, f"No such CityObject: {building_id!r}")

    # Captured BEFORE remove_building() runs -- it doesn't return the
    # deleted object (see sbg/edit.py) -- and by reference, not a deep copy;
    # see session.py's module docstring for why that's safe here.
    snapshot = cm["CityObjects"][building_id]
    remove_building(cm, building_id, compact=False)

    request.app.state.session.record("remove", building_id, snapshot)
    request.app.state.spatial_index.mark_removed(building_id)
    # Can't be patched incrementally (it's precomputed serialized bytes) --
    # invalidate and let /api/dataset/buildings recompute lazily on the next
    # full-island request, not eagerly here. See dataset.py.
    request.app.state.full_island_buildings_body = None

    return {"ok": True, "removed_id": building_id, "session": request.app.state.session.to_status_dict()}


class HeightPatch(BaseModel):
    height: float


@router.patch("/{building_id:path}/height")
def patch_height(building_id: str, patch: HeightPatch, request: Request):
    """Direct user request: "only rare weird cases are absurdly wrong, like
    Marina Bay Sands" -- OneMap's nearest-centroid backfill (see
    onemap/backfill.py's own honest follow-up: ~9.5% of matches are a real
    coin-flip without a second independent signal) occasionally assigns a
    tower's real neighbor's height instead of its own. This exposes the
    exact same geometry-aware height fix backfill.py's bulk script already
    uses (_regenerate_height -- moves the roof, doesn't just relabel the
    attribute) as a live, single-building, undoable edit.

    LoD1/extruded (Solid/CompositeSolid) only, by explicit design -- a
    mesh-embedded building (MultiSurface/CompositeSurface, e.g. an OneMap
    review-gate replacement) has no single "roof height" to move; fixing
    that kind of building means replacing its geometry, not editing a
    number. _regenerate_height itself already refuses non-Solid geometry
    (returns False, does nothing) -- this endpoint checks explicitly first
    so the error is clear instead of a silent no-op.

    vertex_refcounts is recomputed fresh every call (~3.7s measured on the
    full 118,782-building dataset) rather than cached -- this is a rare,
    deliberate one-off correction action per the user's own framing, not a
    hot path, and a persistent cache here would need its own invalidation
    tracked across every other geometry-mutating endpoint (remove, every
    add path, OneMap approve, undo) for a feature that might get used a
    handful of times a session. Simple and always-correct beats fast and
    another cache-staleness risk to get wrong, especially on a code path
    with this project's own track record of exactly that category of bug.
    """
    cm = request.app.state.cm
    if building_id not in cm["CityObjects"]:
        raise HTTPException(404, f"No such CityObject: {building_id!r}")
    obj = cm["CityObjects"][building_id]
    geom = obj["geometry"][0] if obj.get("geometry") else None
    if geom is None or geom["type"] not in ("Solid", "CompositeSolid"):
        raise HTTPException(
            400,
            "Height editing is only available for LoD1/extruded buildings -- "
            "this building has real mesh geometry (e.g. a OneMap review-gate "
            "replacement), which has no single roof height to move.",
        )

    transform = cm["transform"]
    _sx, _sy, sz = transform["scale"]
    _tx, _ty, tz = transform["translate"]
    base_q = round((0.0 - tz) / sz)
    local_indices = set(walk_vertex_indices(geom["boundaries"]))
    # _regenerate_height (and this endpoint) assume a flat base at real-world
    # Z=0 -- true for every building in the actual production dataset
    # (build_sbg.py's own convention, confirmed directly), but NOT true for
    # a terrain-draped extrusion (conforming_overlay's own output, where the
    # base follows local elevation instead). If base_q matches nothing here,
    # that assumption doesn't hold for this file -- reject explicitly rather
    # than silently misclassify every vertex (base included) as "roof" and
    # flatten the whole building to the new height, which is exactly what a
    # first, untested version of this endpoint did against a draped fixture.
    if not any(cm["vertices"][i][2] == base_q for i in local_indices):
        raise HTTPException(
            500,
            "This building's base isn't at real-world Z=0 (a flat, terrain-draped, "
            "or otherwise non-standard extrusion) -- height editing assumes the "
            "standard flat-base convention and can't safely edit this geometry.",
        )
    top_indices = [i for i in local_indices if cm["vertices"][i][2] != base_q]

    # Snapshot BEFORE mutating: boundaries by reference (safe -- see
    # session.py's own module docstring on why a reassignment, not an
    # in-place mutation, makes this safe to capture by reference) plus the
    # exact current quantized Z of every top vertex, since some of those
    # get moved IN PLACE (not given a fresh copy) when this building is
    # the vertex's only user -- restoring the old boundaries reference
    # alone wouldn't undo an in-place move of a vertex it still points at.
    snapshot = {
        "attributes": dict(obj["attributes"]),
        "boundaries": geom["boundaries"],
        "moved_vertices": [(i, cm["vertices"][i][2]) for i in top_indices],
    }

    vertex_refcounts = compute_vertex_refcounts(cm)
    pool = AppendOnlyVertexPool(cm)
    changed = _regenerate_height(cm, obj, patch.height, vertex_refcounts, pool)
    obj["attributes"]["height"] = patch.height
    obj["attributes"]["height_source"] = "manual"

    request.app.state.session.record("edit_height", building_id, snapshot)
    request.app.state.full_island_buildings_body = None
    # Height doesn't change the footprint (XY unchanged), but the 2D/info-
    # panel record does carry height/height_source -- refresh just those.
    records = request.app.state.spatial_index.footprint_records
    if building_id in records:
        records[building_id]["height"] = patch.height
        records[building_id]["height_source"] = "manual"

    return {
        "ok": True, "building_id": building_id, "height": patch.height,
        "geometry_changed": changed, "session": request.app.state.session.to_status_dict(),
        # Same {id, rings, height, height_source} shape every other add-path
        # endpoint returns -- lets the frontend push straight into its
        # existing addedFootprints/addedCitymodel overlay mechanism (see
        # App.vue's pushAddedFootprint) to show the corrected geometry
        # immediately, reusing machinery instead of building a new path.
        "footprint": records.get(building_id),
    }


# --- Phase 4b: Add Building, three paths -----------------------------------


class AddHypotheticalRequest(BaseModel):
    # [exterior_ring, hole_ring, ...], each a list of (x, y) EPSG:3414 tuples
    # -- same shape sbg.edit.add_building's own `footprint` param already
    # expects, so this is passed straight through unmodified.
    footprint: List[List[Tuple[float, float]]]
    height: float
    attributes: Optional[dict] = None
    base_z: Optional[float] = None


@router.post("/add")
def add_hypothetical(req: AddHypotheticalRequest, request: Request):
    """Path 1: a made-up mass with a made-up height, for CFD scenario
    testing ("what if a building sat here") -- not a claim about anything
    real. Only height is required; add_building's own manual/<uuid4> default
    covers the id, attributes are fully optional. base_z defaults to flat
    0.0 -- does NOT wire the island-terrain ElevationLookup, which is
    "built but disabled pending bugs" elsewhere in this project, not a safe
    default to build a new feature on top of.
    """
    cm = request.app.state.cm
    try:
        obj_id = add_building(
            cm, req.footprint, req.height,
            attributes=req.attributes, crs=TARGET_CRS,
            base_z=req.base_z if req.base_z is not None else 0.0,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    request.app.state.session.record("add", obj_id, snapshot=None)
    request.app.state.spatial_index.mark_added(obj_id)
    request.app.state.full_island_buildings_body = None

    return {
        "ok": True, "building_id": obj_id,
        # mark_added() just computed this record (rings/height/height_source)
        # for the spatial index -- returned here too so the frontend can push
        # it directly into its own footprint/overlay state instead of
        # re-fetching the full-island footprint list just to reflect one new
        # building.
        "footprint": request.app.state.spatial_index.footprint_records.get(obj_id),
        "session": request.app.state.session.to_status_dict(),
    }


def _extract_epsg(ref_system):
    """Pulls the trailing numeric EPSG code out of either the URL form this
    project writes (".../EPSG/0/3414") or the URN form other tools sometimes
    use ("urn:ogc:def:crs:EPSG::4326") -- both just end in the code itself.
    """
    if not ref_system:
        return None
    import re
    m = re.search(r"(\d{4,5})\s*$", str(ref_system))
    return int(m.group(1)) if m else None


_TARGET_EPSG = _extract_epsg(CITYJSON_REFERENCE_SYSTEM)


@router.post("/import-cityjson")
async def import_cityjson(request: Request, file: UploadFile = File(...)):
    """Path 2: a real building someone already has as a proper, georeferenced
    CityJSON file (e.g. one of onemap_landmarks/exports/*.city.json). Every
    Building CityObject in the upload gets merged in -- an upload can
    legitimately contain more than one (a multi-building complex), so all
    resulting adds are pushed as one Session.record_group() action: one
    Undo click reverses the whole import, not one building at a time.
    """
    cm = request.app.state.cm
    try:
        uploaded = json.loads(await file.read())
    except Exception as e:
        raise HTTPException(400, f"Could not parse uploaded file as JSON: {e}")

    if not all(k in uploaded for k in ("CityObjects", "vertices", "transform")):
        raise HTTPException(400, "Not a valid CityJSON file (missing CityObjects/vertices/transform)")

    uploaded_epsg = _extract_epsg((uploaded.get("metadata") or {}).get("referenceSystem"))
    # Missing metadata -> assume already TARGET_CRS rather than block the
    # upload; every real CityJSON this project produces sets this field, so
    # "missing" in practice means "a hand-crafted or stripped-down file,
    # already in our own coordinates" more often than "unknown foreign CRS".
    transformer = None
    if uploaded_epsg is not None and uploaded_epsg != _TARGET_EPSG:
        transformer = Transformer.from_crs(f"EPSG:{uploaded_epsg}", TARGET_CRS, always_xy=True)

    pool = AppendOnlyVertexPool(cm)
    uploaded_lookup = vertex_lookup(uploaded)

    added_ids = []
    for obj_id, obj in uploaded["CityObjects"].items():
        if obj.get("type") != "Building" or not obj.get("geometry"):
            continue

        old_indices = list(dict.fromkeys(walk_vertex_indices([g["boundaries"] for g in obj["geometry"]])))
        remap = {}
        for old_idx in old_indices:
            x, y, z = uploaded_lookup(old_idx)
            if transformer is not None:
                x, y = transformer.transform(x, y)
            remap[old_idx] = pool.add(x, y, z)

        new_geometry = [
            {**g, "boundaries": remap_vertex_indices(g["boundaries"], remap)}
            for g in obj["geometry"]
        ]

        new_id = obj_id if obj_id not in cm["CityObjects"] else f"manual/{uuid.uuid4().hex}"
        new_attrs = dict(obj.get("attributes") or {})
        new_attrs.setdefault("height_source", "uploaded_cityjson")
        cm["CityObjects"][new_id] = {"type": "Building", "attributes": new_attrs, "geometry": new_geometry}
        request.app.state.spatial_index.mark_added(new_id)
        added_ids.append(new_id)

    if not added_ids:
        raise HTTPException(400, "No Building CityObjects with geometry found in the uploaded file")

    request.app.state.session.record_group([("add", oid, None) for oid in added_ids])
    request.app.state.full_island_buildings_body = None

    records = request.app.state.spatial_index.footprint_records
    return {
        "ok": True, "building_ids": added_ids,
        "footprints": [records[oid] for oid in added_ids if oid in records],
        "session": request.app.state.session.to_status_dict(),
    }


@router.post("/import-mesh")
async def import_mesh(
    request: Request,
    file: UploadFile = File(...),
    dx: float = Form(0.0),
    dy: float = Form(0.0),
    dz: float = Form(0.0),
    rotation_deg: float = Form(0.0),
    attributes: Optional[str] = Form(None),
):
    """Path 3: a real or custom mesh (STL or OBJ) placed into the scene.
    dx/dy/dz are simple additive offsets applied directly to the file's own
    raw vertex coordinates -- 0,0,0 (the default) means "use the file's own
    coordinates as-is", which is exactly right for a file already exported
    in real EPSG:3414 coordinates (e.g. onemap_landmarks/exports/*.obj,
    cjio subsets of the real SBG dataset -- confirmed directly by
    inspecting one: vertex range ~32844-33106 x, ~31286-31537 y, matching
    real SVY21 magnitudes, no placement needed at all). For an arbitrary
    local-origin file (a custom-modeled STL), the frontend's ghost-preview
    placement UI computes dx/dy/dz from wherever the user actually dropped
    it -- this endpoint just applies whatever offset it's given, it has no
    opinion on how that offset was derived. rotation_deg rotates around the
    mesh's own XY centroid (not the world origin), so rotating in place
    doesn't also translate it.
    """
    cm = request.app.state.cm
    suffix = PathlibPath(file.filename or "").suffix.lower()
    if suffix not in (".stl", ".obj"):
        raise HTTPException(400, f"Unsupported file type {suffix!r} -- expected .stl or .obj")

    raw = await file.read()
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(raw)
            tmp_path = tmp.name
        mesh = trimesh.load(tmp_path, force="mesh")
    except Exception as e:
        raise HTTPException(400, f"Could not parse mesh file: {e}")
    finally:
        if tmp_path:
            os.unlink(tmp_path)

    if mesh is None or len(mesh.vertices) == 0 or len(mesh.faces) == 0:
        raise HTTPException(400, "Uploaded mesh has no usable geometry")

    verts = np.asarray(mesh.vertices, dtype=float)
    if rotation_deg:
        centroid_xy = verts[:, :2].mean(axis=0)
        theta = np.radians(rotation_deg)
        cos_t, sin_t = np.cos(theta), np.sin(theta)
        rel = verts[:, :2] - centroid_xy
        verts[:, :2] = np.column_stack([
            rel[:, 0] * cos_t - rel[:, 1] * sin_t,
            rel[:, 0] * sin_t + rel[:, 1] * cos_t,
        ]) + centroid_xy
    verts[:, 0] += dx
    verts[:, 1] += dy
    verts[:, 2] += dz
    mesh.vertices = verts

    try:
        parsed_attrs = json.loads(attributes) if attributes else None
    except json.JSONDecodeError:
        raise HTTPException(400, "attributes must be valid JSON if provided")

    pool = AppendOnlyVertexPool(cm)
    obj_id = embed_mesh_as_new_object(cm, pool, mesh, parsed_attrs)

    request.app.state.session.record("add", obj_id, snapshot=None)
    request.app.state.spatial_index.mark_added(obj_id)
    request.app.state.full_island_buildings_body = None

    return {
        "ok": True, "building_id": obj_id,
        "footprint": request.app.state.spatial_index.footprint_records.get(obj_id),
        "session": request.app.state.session.to_status_dict(),
    }


@router.post("/undo")
def undo(request: Request):
    session = request.app.state.session
    entries = session.pop_last()
    if not entries:
        raise HTTPException(400, "Nothing to undo")

    cm = request.app.state.cm
    index = request.app.state.spatial_index
    for entry in entries:
        if entry.op == "remove":
            cm["CityObjects"][entry.building_id] = entry.snapshot
            index.mark_restored(entry.building_id)
        elif entry.op == "add":
            del cm["CityObjects"][entry.building_id]
            index.mark_add_undone(entry.building_id)
        elif entry.op == "edit_attributes":
            cm["CityObjects"][entry.building_id]["attributes"] = entry.snapshot
        elif entry.op == "edit_height":
            obj = cm["CityObjects"][entry.building_id]
            obj["attributes"] = entry.snapshot["attributes"]
            obj["geometry"][0]["boundaries"] = entry.snapshot["boundaries"]
            # Reverts any vertex _regenerate_height moved IN PLACE (see the
            # endpoint's own comment) -- restoring the old boundaries
            # reference alone isn't enough if it still points at a vertex
            # whose Z was mutated directly rather than given a fresh copy.
            for idx, old_z in entry.snapshot["moved_vertices"]:
                cm["vertices"][idx][2] = old_z
            records = index.footprint_records
            if entry.building_id in records:
                records[entry.building_id]["height"] = entry.snapshot["attributes"].get("height")
                records[entry.building_id]["height_source"] = entry.snapshot["attributes"].get("height_source")

    request.app.state.full_island_buildings_body = None
    # "undone" is a list even for the (overwhelmingly common) single-entry
    # case, so the frontend has one shape to handle -- SbgViewer3D's
    # lastUndone watcher (Phase 4a) already expects a single object, so
    # App.vue picks entries[0] for that specific use; the full list is here
    # for anything that needs to react to a whole multi-entry action.
    return {"ok": True, "undone": [e.to_dict() for e in entries], "session": session.to_status_dict()}
