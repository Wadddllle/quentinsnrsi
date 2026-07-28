"""Embeds a real OneMap mesh into an SBG CityObject as a MultiSurface geometry,
replacing its LoD1 Solid box. Used for the small, curated Phase 1.6 landmark
list (domes/stadiums/multi-block complexes a simple extrusion can't represent).

Usage: .venv/bin/python -m sbg.onemap.embed
"""
import json
import sys
import uuid

from sbg.config import SBG_OUTPUT
from sbg.io_cityjson import AppendOnlyVertexPool
from sbg.onemap.mesh import extract_building_mesh


def mesh_to_multisurface(pool, mesh):
    """Appends every mesh vertex and returns a CityJSON MultiSurface geometry
    (one triangular ring per face). Tagged lod="3" -- a raw OneMap tile mesh
    is real per-facade geometry (walls, roof shape, domes), closer to LoD3
    fidelity than the "2" this was previously (incorrectly) tagged with; a
    future LoD2-simplification pass can append a second, lower-detail
    geometry entry alongside this one (CityJSON's `geometry` field is a
    list for exactly this reason) rather than needing to replace it."""
    local_to_pool_idx = [pool.add(x, y, z) for x, y, z in mesh.vertices]
    boundaries = [[[local_to_pool_idx[i] for i in face]] for face in mesh.faces]
    return {"type": "MultiSurface", "lod": "3", "boundaries": boundaries}


def embed_mesh_as_new_object(cm, pool, mesh, attributes, building_id=None):
    """Embeds `mesh` (a trimesh.Trimesh, already in real-world TARGET_CRS
    coordinates -- caller's job to place/reproject it there) as a brand NEW
    CityObject, not a replacement of an existing one. Despite living in this
    OneMap-flavored module, this function itself has nothing OneMap-specific
    about it -- Phase 4b (add-building) reuses it verbatim for uploaded
    STL/OBJ meshes, since the embedding step doesn't care whether the mesh
    came from a fetched OneMap tile or a user's own file. Mirrors
    replace_with_onemap_mesh's body (mesh_to_multisurface + attribute
    tagging) but creates a new id instead of overwriting an existing
    object -- that function's overwrite-in-place is the wrong shape for a
    pure add.
    """
    geometry = mesh_to_multisurface(pool, mesh)
    obj_id = building_id or f"manual/{uuid.uuid4().hex}"
    if obj_id in cm["CityObjects"]:
        raise ValueError(f"CityObject id {obj_id!r} already exists")

    final_attributes = dict(attributes or {})
    final_attributes.setdefault("height_source", "manual_mesh")
    final_attributes["mesh_vertex_count"] = len(mesh.vertices)
    final_attributes["mesh_face_count"] = len(mesh.faces)
    final_attributes["mesh_watertight"] = bool(mesh.is_watertight)

    cm["CityObjects"][obj_id] = {
        "type": "Building",
        "attributes": final_attributes,
        "geometry": [geometry],
    }
    return obj_id


def replace_with_onemap_mesh(cm, pool, obj_id, tile_uri, gml_id):
    """Fetches+repairs the real mesh and replaces obj_id's geometry in-place.
    Returns True on success, False if the tile/building had no usable mesh.
    """
    mesh = extract_building_mesh(tile_uri, gml_id)
    if mesh is None or len(mesh.vertices) == 0:
        return False

    geometry = mesh_to_multisurface(pool, mesh)
    obj = cm["CityObjects"][obj_id]
    obj["geometry"] = [geometry]
    obj["attributes"]["height_source"] = "onemap_mesh"
    obj["attributes"]["mesh_vertex_count"] = len(mesh.vertices)
    obj["attributes"]["mesh_face_count"] = len(mesh.faces)
    obj["attributes"]["mesh_watertight"] = bool(mesh.is_watertight)
    return True


# All 6 previously-listed candidates (The Interlace, Esplanade domes, National Stadium,
# Flower Dome, Cloud Forest, Singapore Indoor Stadium) were built, embedded, visually
# reviewed against real neighboring context, found to have real placement/scope problems
# (floating domes with no base, overlapping structures, ~70-300m offsets from OneMap's
# own reference point), and REVERTED. Do not repopulate this list until the human review
# gate described in /home/quentin/snrsi/onemap_landmarks/NOTES.md exists — that file also
# has the full tile_uri/gml_id table so none of the lookup work needs repeating.
CANDIDATES = []


def main():
    print(f"Loading {SBG_OUTPUT}...", file=sys.stderr)
    cm = json.load(open(SBG_OUTPUT))
    pool = AppendOnlyVertexPool(cm)

    replaced = 0
    for obj_id, tile_uri, gml_id in CANDIDATES:
        if obj_id not in cm["CityObjects"]:
            print(f"  SKIP {obj_id}: not in SBG", file=sys.stderr)
            continue
        if cm["CityObjects"][obj_id]["attributes"].get("height_source") == "onemap_mesh":
            print(f"  SKIP {obj_id}: already mesh-replaced", file=sys.stderr)
            continue
        ok = replace_with_onemap_mesh(cm, pool, obj_id, tile_uri, gml_id)
        print(f"  {'OK' if ok else 'FAILED'}: {obj_id} <- {gml_id}", file=sys.stderr)
        if ok:
            replaced += 1

    print(f"Replaced {replaced}/{len(CANDIDATES)}", file=sys.stderr)

    with open(SBG_OUTPUT, "w") as f:
        json.dump(cm, f)
    print(f"Wrote {SBG_OUTPUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
