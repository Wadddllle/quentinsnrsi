"""Lightweight all-island footprint index for the v2 lean UI.

Loads `sg_buildings_v5.geojson` footprints (reprojected once to EPSG:3414) plus
`building_archetype`, into a shapely STRtree. This is ALL the v2 tool needs for
the 2D navigation basemap + domain in/out preview -- far lighter than v1's
1.3GB CityJSON load. Reprojected records are cached to disk (keyed on the source
file's mtime) so restarts are fast.

Reuses `sbg.build_sbg.iter_features` / `reproject_polygon_rings` (the exact
streaming + 4326->3414 reprojection Deliverable 1 already uses).
"""
import pickle
import time
from pathlib import Path

from shapely.geometry import MultiPolygon, Polygon, box
from shapely.strtree import STRtree

from sbg.build_sbg import iter_features, reproject_polygon_rings
from sbg.config import DATA_DIR, SG_BUILDINGS_GEOJSON

_CACHE = DATA_DIR / "footprint_index_cache.pkl"


class FootprintIndex:
    """STRtree over sg_buildings_v5 footprints (EPSG:3414). `records` maps id ->
    {id, rings (exterior rings per part), archetype, height} for the 2D view."""

    def __init__(self, records):
        self.records = records
        self.ids = list(records.keys())
        self.polygons = [_record_polygon(records[i]) for i in self.ids]
        self.tree = STRtree(self.polygons)
        self.polygon_by_id = dict(zip(self.ids, self.polygons))

    def query_contained(self, domain):
        """Ids whose footprint is fully inside domain (cutout keep semantics)."""
        return [self.ids[i] for i in self.tree.query(domain, predicate="contains")]

    def query_intersects_not_contained(self, domain):
        """Ids that cross the boundary (intersect but not contained) -- the
        'would be dropped because it crosses the domain line' warning set."""
        contained = set(self.query_contained(domain))
        idxs = self.tree.query(domain, predicate="intersects")
        return [self.ids[i] for i in idxs if self.ids[i] not in contained]

    def query_bbox(self, xmin, ymin, xmax, ymax):
        b = box(xmin, ymin, xmax, ymax)
        return [self.ids[i] for i in self.tree.query(b, predicate="intersects")]

    def all_records(self):
        return list(self.records.values())


def _record_polygon(rec):
    parts = [Polygon(r) for r in rec["rings"] if len(r) >= 3]
    return parts[0] if len(parts) == 1 else MultiPolygon(parts)


def _feature_polygon(feat):
    """Reproject a GeoJSON feature's footprint to EPSG:3414 -> (shapely poly,
    exterior-ring lists). Returns (None, None) on degenerate/invalid geometry."""
    geom = feat.get("geometry") or {}
    gtype = geom.get("type")
    coords = geom.get("coordinates")
    if gtype == "Polygon":
        polys = [coords]
    elif gtype == "MultiPolygon":
        polys = coords
    else:
        return None, None
    parts = []
    for rings in polys:
        rings_xy = reproject_polygon_rings(rings)
        if not rings_xy or len(rings_xy[0]) < 3:
            continue
        try:
            parts.append(Polygon(rings_xy[0], rings_xy[1:]))
        except Exception:
            continue
    if not parts:
        return None, None
    poly = parts[0] if len(parts) == 1 else MultiPolygon(parts)
    if not poly.is_valid:
        poly = poly.buffer(0)  # fix self-intersections (same OSM data pathology v1 handles)
        if poly.is_empty:
            return None, None
    geoms = [poly] if poly.geom_type == "Polygon" else list(poly.geoms)
    return poly, [list(g.exterior.coords) for g in geoms]


def _build_records(path, log):
    t0 = time.time()
    records = {}
    seen = 0
    for feat in iter_features(path):
        props = feat.get("properties", {})
        oid = props.get("id")
        if not oid:
            continue
        if oid in records:
            oid = f"{oid}#{seen}"  # dedupe suffix, matching build_sbg
        seen += 1
        _poly, rings = _feature_polygon(feat)
        if rings is None:
            continue
        records[oid] = {
            "id": oid,
            "rings": rings,
            "archetype": props.get("building_archetype"),
            "height": props.get("height"),
        }
    log(f"[footprints] reprojected {len(records)} footprints in {time.time() - t0:.0f}s")
    return records


def load_footprint_index(path=SG_BUILDINGS_GEOJSON, cache=_CACHE, log=print):
    """Load (or build+cache) the footprint index. Cache is invalidated when the
    source geojson is newer than the cache file."""
    path = Path(path)
    if cache and Path(cache).exists() and Path(cache).stat().st_mtime >= path.stat().st_mtime:
        t0 = time.time()
        with open(cache, "rb") as f:
            records = pickle.load(f)
        log(f"[footprints] loaded {len(records)} cached footprints in {time.time() - t0:.0f}s")
    else:
        records = _build_records(path, log)
        if cache:
            Path(cache).parent.mkdir(parents=True, exist_ok=True)
            with open(cache, "wb") as f:
                pickle.dump(records, f, protocol=pickle.HIGHEST_PROTOCOL)
            log(f"[footprints] cached -> {cache}")
    t0 = time.time()
    idx = FootprintIndex(records)
    log(f"[footprints] STRtree built in {time.time() - t0:.0f}s ({len(idx.ids)} footprints)")
    return idx
