"""CityJSON vertex pooling and file I/O shared across pipeline phases."""
import json
from pathlib import Path

from shapely.geometry import MultiPolygon, Polygon

from sbg.config import CITYJSON_REFERENCE_SYSTEM, CITYJSON_VERSION, TRANSFORM_SCALE


class VertexPool:
    """Dedups real-world (x, y, z) vertices and assigns stable indices.

    Dedup precision matches TRANSFORM_SCALE (millimeters) so distinct pool
    entries never collapse onto the same quantized integer vertex later.
    """

    def __init__(self, precision=3):
        self._precision = precision
        self._index = {}
        self.vertices = []  # list of (x, y, z) float tuples, real-world coords

    def add(self, x, y, z):
        key = (round(x, self._precision), round(y, self._precision), round(z, self._precision))
        idx = self._index.get(key)
        if idx is None:
            idx = len(self.vertices)
            self._index[key] = idx
            self.vertices.append(key)
        return idx

    def compute_transform(self, scale=TRANSFORM_SCALE):
        xs, ys, zs = zip(*self.vertices)
        return {"scale": [scale, scale, scale], "translate": [min(xs), min(ys), min(zs)]}

    def quantized(self, transform):
        sx, sy, sz = transform["scale"]
        tx, ty, tz = transform["translate"]
        return [
            [round((x - tx) / sx), round((y - ty) / sy), round((z - tz) / sz)]
            for x, y, z in self.vertices
        ]


class AppendOnlyVertexPool:
    """Appends new (already real-world) vertices to an existing CityJSON's
    vertex list, quantizing with the file's existing transform. Avoids
    rebuilding/re-quantizing every vertex already in the file — used when
    editing/augmenting an already-finalized CityJSON in place (embedding a
    replacement mesh, adding a new building) rather than building one fresh.
    """

    def __init__(self, cm):
        self.vertices = cm["vertices"]  # mutated in place
        t = cm["transform"]
        self.sx, self.sy, self.sz = t["scale"]
        self.tx, self.ty, self.tz = t["translate"]

    def add(self, x, y, z):
        idx = len(self.vertices)
        self.vertices.append(
            [
                round((x - self.tx) / self.sx),
                round((y - self.ty) / self.sy),
                round((z - self.tz) / self.sz),
            ]
        )
        return idx


def new_cityjson(reference_system=CITYJSON_REFERENCE_SYSTEM, extra_metadata=None):
    metadata = {"referenceSystem": reference_system}
    if extra_metadata:
        metadata.update(extra_metadata)
    return {
        "type": "CityJSON",
        "version": CITYJSON_VERSION,
        "CityObjects": {},
        "vertices": [],
        "metadata": metadata,
    }


def vertex_lookup(cm):
    """Returns a function idx -> (x, y, z) in real-world coords for this CityJSON."""
    verts = cm["vertices"]
    transform = cm["transform"]
    sx, sy, sz = transform["scale"]
    tx, ty, tz = transform["translate"]

    def lookup(idx):
        x, y, z = verts[idx]
        return (x * sx + tx, y * sy + ty, z * sz + tz)

    return lookup


def solid_shells(geom):
    """Normalizes a Solid/CompositeSolid geometry dict into a list of shells
    (each shell a list of faces, per CityJSON boundary nesting). Returns None
    for any other geometry type (e.g. MultiSurface).
    """
    gtype = geom["type"]
    if gtype == "Solid":
        return [geom["boundaries"]]
    if gtype == "CompositeSolid":
        return geom["boundaries"]
    return None


def footprint_rings(cm, obj):
    """Reconstructs each solid's bottom-face rings (exterior + holes) in
    real-world (x, y) coords: [[exterior, hole1, ...], ...], one entry per
    solid part (>1 for CompositeSolid/MultiPolygon-sourced buildings).
    Returns None if the object has no usable Solid/CompositeSolid geometry.
    """
    shells = solid_shells(obj["geometry"][0])
    if shells is None:
        return None
    lookup = vertex_lookup(cm)

    parts = []
    for solid in shells:
        bottom_face = solid[0][0]
        rings = [[lookup(i)[:2] for i in ring] for ring in bottom_face]
        rings = [r for r in rings if len(r) >= 3]
        if rings:
            parts.append(rings)
    return parts or None


def walk_vertex_indices(boundaries):
    """Recursively yields every integer vertex index in a nested CityJSON
    boundaries list — works uniformly across Solid/CompositeSolid/MultiSurface
    nesting depths since it just recurses until it hits an int.
    """
    if isinstance(boundaries, int):
        yield boundaries
    else:
        for item in boundaries:
            yield from walk_vertex_indices(item)


def remap_vertex_indices(boundaries, mapping):
    """Rewrites a nested boundaries list's vertex indices per `mapping` (old
    index -> new index) — the compaction counterpart to walk_vertex_indices.
    """
    if isinstance(boundaries, int):
        return mapping[boundaries]
    return [remap_vertex_indices(item, mapping) for item in boundaries]


def building_footprint_polygon(cm, obj):
    """Reconstructs a building's 2D footprint (with holes) as a shapely
    Polygon/MultiPolygon. Returns None if the object has no usable geometry.
    """
    parts = footprint_rings(cm, obj)
    if not parts:
        return None
    polys = []
    for rings in parts:
        exterior, *interiors = rings
        try:
            poly = Polygon(exterior, interiors)
        except Exception:
            continue
        if poly.is_valid and poly.area > 0:
            polys.append(poly)
    if not polys:
        return None
    return polys[0] if len(polys) == 1 else MultiPolygon(polys)


def building_footprint_centroid(cm, obj):
    """Centroid of a Building CityObject's largest solid's exterior ring
    (real-world coords). Returns None if the object has no usable geometry.
    """
    parts = footprint_rings(cm, obj)
    if not parts:
        return None

    def ring_area(pts):
        return abs(sum(pts[i][0] * pts[(i + 1) % len(pts)][1] - pts[(i + 1) % len(pts)][0] * pts[i][1] for i in range(len(pts)))) / 2

    best_ring = max((part[0] for part in parts), key=ring_area)
    xs = [p[0] for p in best_ring]
    ys = [p[1] for p in best_ring]
    return (sum(xs) / len(xs), sum(ys) / len(ys))


def subset_cityjson(cm, obj_ids):
    """Builds a new CityJSON containing only the given CityObject ids, with a
    compacted vertex list (only vertices those objects actually reference)
    and remapped indices. Shared by cutout.py's cutout() and the UI's
    bbox-scoped dataset endpoint -- same output shape, same compaction logic,
    written once.
    """
    obj_ids = list(obj_ids)
    referenced = set()
    for obj_id in obj_ids:
        for geom in cm["CityObjects"][obj_id]["geometry"]:
            referenced.update(walk_vertex_indices(geom["boundaries"]))
    referenced_sorted = sorted(referenced)
    remap = {old: new for new, old in enumerate(referenced_sorted)}

    new_cm = {
        "type": "CityJSON",
        "version": cm["version"],
        "transform": cm["transform"],
        "metadata": dict(cm.get("metadata", {})),
        "CityObjects": {},
        "vertices": [cm["vertices"][i] for i in referenced_sorted],
    }
    for obj_id in obj_ids:
        obj = cm["CityObjects"][obj_id]
        new_geoms = []
        for geom in obj["geometry"]:
            new_geom = dict(geom)
            new_geom["boundaries"] = remap_vertex_indices(geom["boundaries"], remap)
            new_geoms.append(new_geom)
        new_cm["CityObjects"][obj_id] = {
            "type": obj["type"],
            "attributes": obj["attributes"],
            "geometry": new_geoms,
        }
    return new_cm


def split_composite_solids(cm):
    """Returns a copy of cm where every CompositeSolid-geometried CityObject
    is replaced by one Solid-typed pseudo-object per part (ids
    "<id>__part<i>"), for consumption by cjio's OBJ exporter.

    Root-caused directly (not a guess): cjio 0.10.1's export2obj()
    (cjio/cityjson.py) only handles MultiSurface/CompositeSurface/Solid
    geometry types -- CompositeSolid falls through every branch unhandled,
    so the object's `o <id>` line gets written but faces_to_obj() is never
    called for it, silently producing zero faces. CompositeSolid is exactly
    what sbg/extrude.py emits for any MultiPolygon footprint (an OSM
    `relation` with disjoint parts -- e.g. twin towers modeled as one
    building), not a rare edge case: confirmed by dumping every `o` marker
    in a real STL job's exported OBJ and finding each CompositeSolid
    building's marker immediately followed by the next object's marker with
    no vertex/face lines in between.

    Only meant for a throwaway pre-OBJ-export copy, never the real output
    file -- splitting loses the "these parts are one building" CityJSON
    semantic, which doesn't matter here since the STL pipeline's later
    Blender join step fuses every object into one mesh soup regardless of
    CityObject identity anyway. Vertex indices are untouched (same shared
    vertex list, same transform) -- this only re-partitions which
    CityObject entry owns which already-built geometry, so no VertexPool
    involved and no need to re-run finalize_and_save.
    """
    new_objects = {}
    for obj_id, obj in cm["CityObjects"].items():
        composite_geoms = [g for g in obj.get("geometry", []) if g["type"] == "CompositeSolid"]
        if not composite_geoms:
            new_objects[obj_id] = obj
            continue
        other_geoms = [g for g in obj.get("geometry", []) if g["type"] != "CompositeSolid"]
        if other_geoms:
            new_objects[obj_id] = {**obj, "geometry": other_geoms}
        part_n = 0
        for geom in composite_geoms:
            for solid in geom["boundaries"]:
                new_objects[f"{obj_id}__part{part_n}"] = {
                    "type": obj["type"],
                    "attributes": obj["attributes"],
                    "geometry": [{"type": "Solid", "lod": geom.get("lod", "1"), "boundaries": solid}],
                }
                part_n += 1
    new_cm = dict(cm)
    new_cm["CityObjects"] = new_objects
    return new_cm


def finalize_and_save(cm, pool, path):
    """Sets cm['transform']/cm['vertices'] from the pool and writes cm to path."""
    transform = pool.compute_transform()
    cm["transform"] = transform
    cm["vertices"] = pool.quantized(transform)

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(cm, f)
    return path
