"""LoD1 footprint -> CityJSON Solid extrusion (box extrusion from a flat footprint + height)."""


def _dedupe_closing_point(ring):
    """GeoJSON rings repeat the first point at the end; CityJSON rings should not."""
    if len(ring) > 1 and ring[0] == ring[-1]:
        return ring[:-1]
    return ring


def footprint_to_solid(pool, rings_xy, base_z, top_z):
    """Extrudes a footprint (exterior ring + optional interior/hole rings) into a
    CityJSON Solid's boundaries: one shell = [bottom_face, top_face, *wall_faces].

    rings_xy: list of rings, each a list of (x, y) tuples in the target CRS,
              first ring exterior, remaining rings interior holes.
    Returns None if the footprint has no usable ring (degenerate, < 3 points).
    """
    clean_rings = [_dedupe_closing_point(list(r)) for r in rings_xy]
    clean_rings = [r for r in clean_rings if len(r) >= 3]
    if not clean_rings:
        return None

    def ring_indices(ring, z):
        return [pool.add(x, y, z) for x, y in ring]

    bottom_rings = [ring_indices(ring, base_z) for ring in clean_rings]
    top_rings = [ring_indices(ring, top_z) for ring in clean_rings]

    faces = []
    # Bottom face points down: reverse winding relative to the top face.
    faces.append([list(reversed(r)) for r in bottom_rings])
    faces.append(top_rings)

    for ring in clean_rings:
        n = len(ring)
        for i in range(n):
            x0, y0 = ring[i]
            x1, y1 = ring[(i + 1) % n]
            v0b = pool.add(x0, y0, base_z)
            v1b = pool.add(x1, y1, base_z)
            v1t = pool.add(x1, y1, top_z)
            v0t = pool.add(x0, y0, top_z)
            faces.append([[v0b, v1b, v1t, v0t]])

    return [faces]  # one shell


def geometry_from_rings(pool, polygons_rings_xy, base_z, top_z):
    """Builds a CityJSON geometry dict (Solid or CompositeSolid) from one or more
    footprint polygons (each a list of rings), for Polygon vs MultiPolygon sources.
    Returns None if every polygon part was degenerate.
    """
    solids = [footprint_to_solid(pool, rings, base_z, top_z) for rings in polygons_rings_xy]
    solids = [s for s in solids if s is not None]
    if not solids:
        return None
    if len(solids) == 1:
        return {"type": "Solid", "lod": "1", "boundaries": solids[0]}
    return {"type": "CompositeSolid", "lod": "1", "boundaries": solids}


def footprint_to_solid_draped(pool, rings_xy, elevation_fn, height):
    """Like footprint_to_solid, but the bottom face follows local terrain
    elevation at each ring vertex (elevation_fn(x, y) -> z) instead of one
    flat base_z — for conforming-mesh use, where the same elevation_fn is
    also used for the terrain triangulation, so boundary vertices land on
    identical (x, y, z) and dedupe to the same VertexPool index, closing the
    seam by construction.

    The roof stays flat (real buildings have flat roofs, not raked ones), set
    to the *highest* sampled base elevation plus `height`, so no wall inverts
    on the uphill side. Wall faces are triangulated (2 triangles each)
    instead of quads, since a sloped base edge + flat top edge isn't planar.
    """
    clean_rings = [_dedupe_closing_point(list(r)) for r in rings_xy]
    clean_rings = [r for r in clean_rings if len(r) >= 3]
    if not clean_rings:
        return None

    base_z_cache = {}

    def base_z(x, y):
        key = (x, y)
        z = base_z_cache.get(key)
        if z is None:
            z = elevation_fn(x, y)
            base_z_cache[key] = z
        return z

    top_z = max(base_z(x, y) for ring in clean_rings for x, y in ring) + height

    bottom_rings = [[pool.add(x, y, base_z(x, y)) for x, y in ring] for ring in clean_rings]
    top_rings = [[pool.add(x, y, top_z) for x, y in ring] for ring in clean_rings]

    faces = []
    faces.append([list(reversed(r)) for r in bottom_rings])
    faces.append(top_rings)

    for ring in clean_rings:
        n = len(ring)
        for i in range(n):
            x0, y0 = ring[i]
            x1, y1 = ring[(i + 1) % n]
            v0b = pool.add(x0, y0, base_z(x0, y0))
            v1b = pool.add(x1, y1, base_z(x1, y1))
            v1t = pool.add(x1, y1, top_z)
            v0t = pool.add(x0, y0, top_z)
            faces.append([[v0b, v1b, v1t]])
            faces.append([[v0b, v1t, v0t]])

    return [faces]


def geometry_from_rings_draped(pool, polygons_rings_xy, elevation_fn, height):
    """Draped-base counterpart of geometry_from_rings (Polygon/MultiPolygon sources)."""
    solids = [footprint_to_solid_draped(pool, rings, elevation_fn, height) for rings in polygons_rings_xy]
    solids = [s for s in solids if s is not None]
    if not solids:
        return None
    if len(solids) == 1:
        return {"type": "Solid", "lod": "1", "boundaries": solids[0]}
    return {"type": "CompositeSolid", "lod": "1", "boundaries": solids}
