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
            self.footprint_records[obj_id] = self._make_record(obj_id, poly, obj)
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

        # Phase 3 (remove-building): shapely's STRtree has no
        # removal/insertion API, and a full rebuild was measured at ~10.2s
        # over 118,782 buildings -- too slow to pay per click. Since removal
        # only ever needs the tree to STOP returning something (never to
        # find something new), every query method below filters its raw
        # tree results through this set instead of rebuilding. Not just an
        # optimization: sbg.io_cityjson.subset_cityjson() does
        # cm["CityObjects"][obj_id] with no existence check, so a stale
        # removed id reaching it would KeyError -- this filtering is what
        # guarantees that never happens.
        #
        # Adding a NEW footprint can't use this same trick, since exclusion
        # can't make something findable that was never in the tree -- Phase
        # 4b (add-building) solves this with pending_additions below: a
        # small, linear-scanned dict of polygons alongside the immutable
        # STRtree, tested with the same shapely predicate the tree query
        # already uses. Cheap as long as the number of adds in a session
        # stays small (dozens, not thousands) -- no cap/consolidation into a
        # real rebuilt tree yet, a known cliff, not solved now (matches the
        # same tradeoff already accepted for removed_ids at Phase 3).
        self.removed_ids = set()
        self.pending_additions = {}
        # For active_count below -- O(1) membership test to tell "removed an
        # original building" apart from "removed a same-session addition"
        # (see mark_removed's own comment for why that distinction matters).
        self._ids_set = set(self.ids)

    def _make_record(self, obj_id, poly, obj):
        geoms = [poly] if poly.geom_type == "Polygon" else list(poly.geoms)
        return {
            "id": obj_id,
            "rings": [list(g.exterior.coords) for g in geoms],
            "height": obj["attributes"].get("height"),
            "height_source": obj["attributes"].get("height_source"),
        }

    def mark_removed(self, obj_id):
        self.removed_ids.add(obj_id)
        # Real bug, found via a user report (KeyError in subset_cityjson):
        # removing a building added earlier THIS session left it in BOTH
        # removed_ids and pending_additions -- every query method's
        # pending_additions merge (query_intersects_bbox etc.) doesn't itself
        # check removed_ids, so the dead id kept coming back out of queries
        # and reaching cm["CityObjects"][obj_id] after it had already been
        # deleted. Popping it here (instead of only adding to removed_ids)
        # keeps "removed_ids wins" a real invariant instead of one that only
        # holds for buildings that existed at startup.
        self.pending_additions.pop(obj_id, None)

    def mark_restored(self, obj_id):
        self.removed_ids.discard(obj_id)
        # Mirror of the mark_removed fix above: undoing the removal of a
        # same-session addition needs to put it back in pending_additions
        # too, not just clear removed_ids -- otherwise it becomes invisible
        # to every query method again even though cm["CityObjects"] genuinely
        # has it back. mark_added() already does exactly this derivation.
        if obj_id not in self._ids_set:
            self.mark_added(obj_id)

    def mark_added(self, obj_id):
        """Call after inserting obj_id into cm["CityObjects"] -- derives its
        polygon/record directly from self.cm (already-updated by the
        caller), same as __init__ does for every building at startup.
        """
        obj = self.cm["CityObjects"][obj_id]
        poly = building_footprint_polygon(self.cm, obj)
        if poly is None:
            return
        self.pending_additions[obj_id] = poly
        self.footprint_records[obj_id] = self._make_record(obj_id, poly, obj)
        # Real bug, user report: polygon_by_id is otherwise only ever built
        # once at __init__ from the startup dataset -- a same-session
        # addition (e.g. a fresh OneMap replacement) was findable via
        # query_contained() (which merges in pending_additions) but then
        # KeyError'd the instant a caller (conforming_overlay, for the STL
        # pipeline) tried to actually look up its polygon here. Keeping this
        # in sync is what query_contained()'s own callers assume already
        # holds.
        self.polygon_by_id[obj_id] = poly

    def mark_add_undone(self, obj_id):
        self.pending_additions.pop(obj_id, None)
        self.footprint_records.pop(obj_id, None)
        self.polygon_by_id.pop(obj_id, None)

    @property
    def active_count(self):
        # removed_ids can now contain ids that were never in self.ids (a
        # same-session addition that got removed -- see mark_removed) --
        # only ids that are BOTH removed and part of the original startup
        # index should count against len(self.ids); pending_additions is
        # already exactly "currently active" (mark_removed pops from it),
        # so it needs no equivalent filtering.
        removed_originals = len(self.removed_ids & self._ids_set)
        return len(self.ids) - removed_originals + len(self.pending_additions)

    def query_intersects_bbox(self, xmin, ymin, xmax, ymax):
        """Ids of buildings whose footprint intersects the bbox (for viewport-scoped fetches)."""
        domain = box(xmin, ymin, xmax, ymax)
        idxs = self.tree.query(domain, predicate="intersects")
        result = [self.ids[i] for i in idxs if self.ids[i] not in self.removed_ids]
        result += [oid for oid, poly in self.pending_additions.items() if domain.intersects(poly)]
        return result

    def query_contained(self, domain_polygon):
        """Ids of buildings whose footprint is fully contained in domain_polygon
        (cutout.py's own keep/drop semantics -- exact, not just bbox-filtered:
        shapely's predicate query does the real geometric test, not just an
        extent check).
        """
        idxs = self.tree.query(domain_polygon, predicate="contains")
        result = [self.ids[i] for i in idxs if self.ids[i] not in self.removed_ids]
        result += [oid for oid, poly in self.pending_additions.items() if domain_polygon.contains(poly)]
        return result

    def query_intersects_not_contained(self, domain_polygon):
        """Ids of buildings that cross the domain boundary (intersect but
        aren't fully contained) -- for the cutout-preview 'would be dropped
        because it crosses the line' warning, as distinct from buildings
        that are simply nowhere near the domain.
        """
        contained = set(self.query_contained(domain_polygon))
        idxs = self.tree.query(domain_polygon, predicate="intersects")
        result = [self.ids[i] for i in idxs if self.ids[i] not in contained and self.ids[i] not in self.removed_ids]
        result += [
            oid for oid, poly in self.pending_additions.items()
            if oid not in contained and domain_polygon.intersects(poly)
        ]
        return result


def build_index(cm):
    t0 = time.time()
    index = SpatialIndex(cm)
    elapsed = time.time() - t0
    return index, elapsed
