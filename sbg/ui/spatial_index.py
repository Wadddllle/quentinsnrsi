"""Spatial index over SBG building footprints -- the one genuinely new piece
of backend logic for Phase 1 (see project plan, Phase 6). Built once at
process startup so every bbox-scoped dataset/cutout endpoint can answer
without scanning the full 118k-building CityJSON per request.

predicate direction confirmed empirically (shapely 2.1.2): tree.query(g,
predicate=X) returns tree items for which g.X(tree_item) is True -- e.g.
predicate='contains' returns items contained BY g, predicate='intersects'
is symmetric.
"""
import time

from shapely.geometry import box
from shapely.strtree import STRtree

from sbg.io_cityjson import building_footprint_polygon


class SpatialIndex:
    def __init__(self, cm):
        self.cm = cm
        self.ids = []
        polygons = []
        # Precomputed once here, not per-request: the 2D plan view's
        # /api/dataset/footprints endpoint used to call building_footprint_polygon()
        # again per request (the same shapely Polygon reconstruction this index
        # already does below) -- fine at ~126-building bbox scale, but measured
        # at 28s / 42MB for a full-island request, since it re-walked and
        # re-triangulated all 118,780 footprints from scratch every single call.
        # Reusing the polygon this loop already builds for the STRtree removes
        # that duplicated work entirely.
        self.footprint_records = {}
        for obj_id, obj in cm["CityObjects"].items():
            if obj["type"] != "Building":
                continue
            poly = building_footprint_polygon(cm, obj)
            if poly is None:
                continue
            self.ids.append(obj_id)
            polygons.append(poly)
            geoms = [poly] if poly.geom_type == "Polygon" else list(poly.geoms)
            self.footprint_records[obj_id] = {
                "id": obj_id,
                "rings": [list(g.exterior.coords) for g in geoms],
                "height": obj["attributes"].get("height"),
                "height_source": obj["attributes"].get("height_source"),
            }
        self.polygons = polygons
        self.tree = STRtree(polygons)
        # Lets callers go straight from a domain-filtered id list (e.g.
        # query_contained's output) back to the actual Polygon object without
        # re-deriving it from cm -- see sbg/topo/conforming_mesh.py's
        # conforming_overlay(), which used to scan and reconstruct all
        # 118,780 buildings' footprints on every single STL pipeline job
        # regardless of domain size (measured: ~15.6s of every job, real
        # data from a 4.4km^2/1,743-building run, vs. ~2.2s for everything
        # else conforming_overlay does combined) before this existing index
        # was wired in to replace that scan.
        self.polygon_by_id = dict(zip(self.ids, self.polygons))

    def query_intersects_bbox(self, xmin, ymin, xmax, ymax):
        """Ids of buildings whose footprint intersects the bbox (for viewport-scoped fetches)."""
        idxs = self.tree.query(box(xmin, ymin, xmax, ymax), predicate="intersects")
        return [self.ids[i] for i in idxs]

    def query_contained(self, domain_polygon):
        """Ids of buildings whose footprint is fully contained in domain_polygon
        (cutout.py's own keep/drop semantics -- exact, not just bbox-filtered:
        shapely's predicate query does the real geometric test, not just an
        extent check).
        """
        idxs = self.tree.query(domain_polygon, predicate="contains")
        return [self.ids[i] for i in idxs]

    def query_intersects_not_contained(self, domain_polygon):
        """Ids of buildings that cross the domain boundary (intersect but
        aren't fully contained) -- for the cutout-preview 'would be dropped
        because it crosses the line' warning, as distinct from buildings
        that are simply nowhere near the domain.
        """
        contained = set(self.query_contained(domain_polygon))
        idxs = self.tree.query(domain_polygon, predicate="intersects")
        return [self.ids[i] for i in idxs if self.ids[i] not in contained]


def build_index(cm):
    t0 = time.time()
    index = SpatialIndex(cm)
    elapsed = time.time() - t0
    return index, elapsed
