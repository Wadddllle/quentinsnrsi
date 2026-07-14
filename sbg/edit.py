"""Deliverable 4: add/remove building geometries to/from SBG.

A plain importable module, not a UI (deferred). Terrain-aware: pass an
elevation_lookup (e.g. sbg.topo.dtm.ElevationLookup) and the new building's
base drapes to local ground per-vertex, matching Phase 3's conforming-mesh
convention (same style of footprint_to_solid_draped call), instead of
assuming flat z=0. For a building added into a domain that already has a
conforming-mesh terrain (sbg/topo/conforming_mesh.py), the seam-closure
guarantee only holds once the new footprint is folded back into that
domain's own conforming_overlay() re-triangulation — this module just
creates a correctly-shaped, reasonably-elevated CityObject on its own.

Usage: .venv/bin/python -m sbg.edit --demo   (round-trip smoke test)
"""
import argparse
import json
import sys
import uuid

from pyproj import Transformer

from sbg.config import SBG_OUTPUT, SOURCE_CRS, TARGET_CRS
from sbg.extrude import geometry_from_rings, geometry_from_rings_draped
from sbg.io_cityjson import AppendOnlyVertexPool, remap_vertex_indices, walk_vertex_indices


def load_sbg(path=SBG_OUTPUT):
    with open(path) as f:
        return json.load(f)


def save_sbg(cm, path=SBG_OUTPUT):
    with open(path, "w") as f:
        json.dump(cm, f)


def _reproject_rings(rings, crs):
    if crs == TARGET_CRS:
        return rings
    transformer = Transformer.from_crs(crs, TARGET_CRS, always_xy=True)
    out = []
    for ring in rings:
        lons = [p[0] for p in ring]
        lats = [p[1] for p in ring]
        xs, ys = transformer.transform(lons, lats)
        out.append(list(zip(xs, ys)))
    return out


def add_building(cm, footprint, height, attributes=None, crs=SOURCE_CRS, base_z=None, elevation_lookup=None, building_id=None):
    """footprint: [exterior_ring, hole_ring, ...], each a list of (x, y)
    tuples in `crs`. height: building height in meters.

    Ground elevation, in priority order: explicit base_z (flat, e.g. "I
    already know this site is at 15m") > elevation_lookup (drapes the base
    per-vertex, reusing extrude.py's footprint_to_solid_draped) > flat z=0.

    Returns the new building's CityObject id.
    """
    rings_xy = _reproject_rings(footprint, crs)
    pool = AppendOnlyVertexPool(cm)

    if base_z is not None:
        geometry = geometry_from_rings(pool, [rings_xy], base_z, base_z + height)
    elif elevation_lookup is not None:
        geometry = geometry_from_rings_draped(pool, [rings_xy], elevation_lookup, height)
    else:
        geometry = geometry_from_rings(pool, [rings_xy], 0.0, height)

    if geometry is None:
        raise ValueError("Footprint produced no usable geometry (degenerate ring?)")

    obj_id = building_id or f"manual/{uuid.uuid4().hex}"
    if obj_id in cm["CityObjects"]:
        raise ValueError(f"CityObject id {obj_id!r} already exists")

    final_attributes = {"height": height, "height_source": "manual"}
    if attributes:
        final_attributes.update(attributes)

    cm["CityObjects"][obj_id] = {
        "type": "Building",
        "attributes": final_attributes,
        "geometry": [geometry],
    }
    return obj_id


def remove_building(cm, building_id, compact=False):
    """Removes building_id's CityObject. If compact=True, also drops any
    vertices no longer referenced by anything and remaps indices (same
    compaction cutout.py uses) — off by default since it's O(all vertices)
    and unnecessary overhead for a single ad-hoc removal.
    """
    if building_id not in cm["CityObjects"]:
        raise KeyError(f"No such CityObject: {building_id!r}")
    del cm["CityObjects"][building_id]

    if not compact:
        return

    referenced = set()
    for obj in cm["CityObjects"].values():
        for geom in obj["geometry"]:
            referenced.update(walk_vertex_indices(geom["boundaries"]))
    referenced_sorted = sorted(referenced)
    remap = {old: new for new, old in enumerate(referenced_sorted)}

    cm["vertices"] = [cm["vertices"][i] for i in referenced_sorted]
    for obj in cm["CityObjects"].values():
        for geom in obj["geometry"]:
            geom["boundaries"] = remap_vertex_indices(geom["boundaries"], remap)


def _demo():
    """Round-trip smoke test: add a synthetic building at a known coordinate,
    verify it, remove it, verify cleanup. Prints pass/fail, doesn't touch
    the real SBG_OUTPUT file on disk.
    """
    cm = load_sbg()
    n_before = len(cm["CityObjects"])
    v_before = len(cm["vertices"])

    footprint = [[(103.8198, 1.3521), (103.8202, 1.3521), (103.8202, 1.3525), (103.8198, 1.3525), (103.8198, 1.3521)]]
    obj_id = add_building(cm, footprint, height=12.5, attributes={"note": "edit.py demo"})
    print(f"Added {obj_id}", file=sys.stderr)
    assert obj_id in cm["CityObjects"]
    assert len(cm["CityObjects"]) == n_before + 1
    assert cm["CityObjects"][obj_id]["attributes"]["height"] == 12.5
    print("  attributes:", cm["CityObjects"][obj_id]["attributes"], file=sys.stderr)

    remove_building(cm, obj_id)
    assert obj_id not in cm["CityObjects"]
    assert len(cm["CityObjects"]) == n_before
    print("Removed OK, CityObjects count restored", file=sys.stderr)

    # vertices were appended (not compacted, by design) -- confirm that's the only diff
    assert len(cm["vertices"]) > v_before
    print(f"  vertices: {v_before} -> {len(cm['vertices'])} (uncompacted leftovers from the add, expected)", file=sys.stderr)
    print("Demo round-trip PASSED", file=sys.stderr)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--demo", action="store_true", help="Run an in-memory add/remove round-trip smoke test")
    args = ap.parse_args()
    if args.demo:
        _demo()
    else:
        ap.print_help()
