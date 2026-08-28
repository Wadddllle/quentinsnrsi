#!/usr/bin/env python
"""Build a DAGMC .h5m from the fused watertight CFD STL.

Runs in the pymoab env, NOT the project .venv:
    ~/tools/mamba/root/envs/moabpy/bin/python dose/stl_to_h5m.py \
        data/openmc_data/cfd_watertight.stl -o data/openmc_data/cfd.h5m

WHAT GEOMETRY THIS BUILDS

DAGMC wants a CELL COMPLEX -- volumes that share surfaces -- not a fused manifold
solid. Our STL is one closed genus-0 body (terrain slab + every building fused by
the voxel remesh), which turns out to be the EASY case: everything collapses to a
single shared surface, so the imprint/merge problem that a per-building model would
hit never arises.

    Surface 1 = the STL          GEOM_SENSE_2 = [solid, air]   <- shared, two-sided
    Surface 2 = an outer box     GEOM_SENSE_2 = [air, 0]       <- vacuum boundary
    Volume 1  = solid  (mat:concrete)   bounded by S1
    Volume 2  = air    (mat:air)        bounded by S1 (reverse) and S2 (forward)
    outside S2 = implicit complement, terminated by boundary:vacuum

Sense convention: for GEOM_SENSE_2 = [fwd, rev] the facet normal points OUT of
`fwd` and INTO `rev`. STL normals point out of the solid, so fwd = solid.

UNITS AND ORIGIN -- easy to get silently wrong, so it is done here explicitly.
A .h5m carries no unit metadata and OpenMC assumes CENTIMETRES, but the STL is in
metres (EPSG:3414). Loaded raw, the 400 m domain would be read as 400 cm and a 30 cm
wall would become 3 mm. The geometry is also ~2.2e6 cm from the origin, where the
STL's float32 vertices only resolve to ~0.2 cm. So:

    p_local_cm = (p_world_m - origin_m) * 100      origin_m = (cx, cy, 0)

x/y are recentred (precision), z is left absolute so elevations stay readable. The
transform is written to a `.transform.json` sidecar -- the source positions and any
tally mesh MUST use the same one or the dose map is silently mis-georeferenced.

WHY NOT vertices_to_h5m: it passes the FULL shared vertex array to
create_vertices() once per volume, so cost and file size are O(volumes x vertices).
Per-volume vertices keep it linear.
"""
import argparse
import struct
import sys

import numpy as np


# --------------------------------------------------------------------------- STL

def read_stl(path):
    """-> (verts, faces), welded. Pure numpy so this runs in the bare pymoab env.

    STL stores unwelded triangle soup (every triangle owns private vertices), so
    the weld is mandatory -- without shared vertices MOAB sees 3N loose points and
    DAGMC cannot establish surface topology at all.
    """
    with open(path, "rb") as f:
        head = f.read(84)
        if head[:5].lstrip().lower().startswith(b"solid") and b"facet" in f.read(512):
            raise SystemExit("ASCII STL not supported -- re-export as binary")
        n = struct.unpack("<I", head[80:84])[0]
        f.seek(84)
        raw = np.frombuffer(f.read(50 * n), dtype=np.uint8).reshape(n, 50)
    # bytes 12:48 are the three vertices; 0:12 is the (ignored) facet normal
    tri = raw[:, 12:48].copy().view("<f4").reshape(n * 3, 3).astype(np.float64)
    verts, faces = np.unique(tri, axis=0, return_inverse=True)
    return verts, faces.reshape(n, 3).astype(np.int64)


def box_mesh(lo, hi):
    """Axis-aligned box with OUTWARD normals -> (verts, faces)."""
    (x0, y0, z0), (x1, y1, z1) = lo, hi
    v = np.array([[x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
                  [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1]], float)
    f = np.array([[0, 2, 1], [0, 3, 2],   # bottom (-z)
                  [4, 5, 6], [4, 6, 7],   # top    (+z)
                  [0, 1, 5], [0, 5, 4],   # -y
                  [2, 3, 7], [2, 7, 6],   # +y
                  [1, 2, 6], [1, 6, 5],   # +x
                  [0, 4, 7], [0, 7, 3]],  # -x
                 np.int64)
    return v, f


def _check_outward(v, f, name):
    """Divergence theorem: signed volume must be positive for outward normals."""
    a, b, c = v[f[:, 0]], v[f[:, 1]], v[f[:, 2]]
    vol = np.einsum("ij,ij->i", a, np.cross(b - a, c - a)).sum() / 6.0
    print(f"  {name}: signed volume {vol:,.1f} cm3 "
          f"({'outward' if vol > 0 else 'INWARD -- normals flipped!'})")
    return vol


# --------------------------------------------------------------------------- h5m

def build(solid, box, out, mats=("concrete", "air")):
    from pymoab import core, types

    mb = core.Core()
    t_dim = mb.tag_get_handle(types.GEOM_DIMENSION_TAG_NAME, 1, types.MB_TYPE_INTEGER,
                              types.MB_TAG_DENSE, create_if_missing=True)
    t_cat = mb.tag_get_handle(types.CATEGORY_TAG_NAME, types.CATEGORY_TAG_SIZE,
                              types.MB_TYPE_OPAQUE, types.MB_TAG_SPARSE, create_if_missing=True)
    t_name = mb.tag_get_handle(types.NAME_TAG_NAME, types.NAME_TAG_SIZE,
                               types.MB_TYPE_OPAQUE, types.MB_TAG_SPARSE, create_if_missing=True)
    t_sense = mb.tag_get_handle("GEOM_SENSE_2", 2, types.MB_TYPE_HANDLE,
                                types.MB_TAG_SPARSE, create_if_missing=True)
    t_gid = mb.tag_get_handle(types.GLOBAL_ID_TAG_NAME)

    def meshset(dim, cat, gid):
        s = mb.create_meshset()
        mb.tag_set_data(t_dim, s, dim)
        mb.tag_set_data(t_cat, s, cat)
        mb.tag_set_data(t_gid, s, gid)
        return s

    def group(name, members):
        g = meshset(4, "Group", 0)
        mb.tag_set_data(t_name, g, name)
        for m in members:
            mb.add_entity(g, m)
        return g

    def facets(surf, v, f):
        """Own vertex array per surface -- keeps cost/size linear in facet count."""
        h = mb.create_vertices(np.ascontiguousarray(v, float).flatten())
        hv = np.asarray(h, dtype=np.uint64)
        conn = hv[np.ascontiguousarray(f, np.int64)].flatten()
        mb.add_entities(surf, h)
        mb.add_entities(surf, mb.create_elements(types.MBTRI, conn.reshape(-1, 3)))

    vol_solid, vol_air = meshset(3, "Volume", 1), meshset(3, "Volume", 2)
    surf_stl, surf_box = meshset(2, "Surface", 1), meshset(2, "Surface", 2)

    # S1 is shared: normals point out of the solid, into the air.
    mb.add_parent_child(vol_solid, surf_stl)
    mb.add_parent_child(vol_air, surf_stl)
    mb.tag_set_data(t_sense, surf_stl, [vol_solid, vol_air])

    # S2 bounds air on the inside; outside is the implicit complement.
    mb.add_parent_child(vol_air, surf_box)
    mb.tag_set_data(t_sense, surf_box, [vol_air, np.uint64(0)])

    facets(surf_stl, *solid)
    facets(surf_box, *box)

    group(f"mat:{mats[0]}", [vol_solid])
    group(f"mat:{mats[1]}", [vol_air])
    group("boundary:vacuum", [surf_box])

    mb.write_file(str(out))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stl")
    ap.add_argument("-o", "--out", default="cfd.h5m")
    ap.add_argument("--pad-xy", type=float, default=50.0,
                    help="air beyond the CFD domain walls (m). Gamma mfp in air at "
                         "1 MeV is ~120 m, so some padding is more realistic than a "
                         "vacuum boundary hard against the plume edge.")
    ap.add_argument("--z-top", type=float, default=250.0, help="air box ceiling (m)")
    ap.add_argument("--z-pad", type=float, default=4.0,
                    help="air below the terrain slab bottom (m). Nonzero on purpose: "
                         "a box face exactly coplanar with the slab underside is a "
                         "ray-tracing ambiguity.")
    ap.add_argument("--mats", default="concrete,air")
    a = ap.parse_args()

    v, f = read_stl(a.stl)
    print(f"STL {a.stl}: {len(f):,} facets, {len(v):,} welded vertices")
    print("  bounds (m, world)", np.round(np.array([v.min(0), v.max(0)]), 2).tolist())

    e = np.sort(np.vstack([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]]), axis=1)
    _, cnt = np.unique(e, axis=0, return_counts=True)
    bad = (cnt != 2).sum()
    print(f"  edges used exactly twice: {(cnt == 2).sum():,}   NOT twice: {bad}")
    if bad:
        raise SystemExit("mesh is not closed+manifold; DAGMC needs it to be")

    lo = np.array([v[:, 0].min() - a.pad_xy, v[:, 1].min() - a.pad_xy, v[:, 2].min() - a.z_pad])
    hi = np.array([v[:, 0].max() + a.pad_xy, v[:, 1].max() + a.pad_xy, a.z_top])
    bv, bf = box_mesh(lo, hi)
    print(f"  air box (m, world) {np.round(lo,1).tolist()} .. {np.round(hi,1).tolist()}")

    # metres/world -> centimetres/local. See the module docstring.
    origin = np.array([0.5 * (v[:, 0].min() + v[:, 0].max()),
                       0.5 * (v[:, 1].min() + v[:, 1].max()), 0.0])
    v = (v - origin) * 100.0
    bv = (bv - origin) * 100.0
    print(f"  origin_m {np.round(origin,3).tolist()}  scale 100 (m->cm)")
    print("  bounds (cm, local)", np.round(np.array([v.min(0), v.max(0)]), 1).tolist())

    import json
    tpath = str(a.out).rsplit(".", 1)[0] + ".transform.json"
    with open(tpath, "w") as fh:
        json.dump({"origin_m": origin.tolist(), "scale_m_to_cm": 100.0, "crs": "EPSG:3414",
                   "forward": "p_local_cm = (p_world_m - origin_m) * 100"}, fh, indent=2)
    print(f"  wrote {tpath}")

    if _check_outward(v, f, "solid") <= 0:
        f = f[:, [0, 2, 1]]
        print("    -> flipped STL winding so normals point out of the solid")
    _check_outward(bv, bf, "air box")

    build((v, f), (bv, bf), a.out, tuple(a.mats.split(",")))
    import os
    print(f"\nwrote {a.out}  ({os.path.getsize(a.out)/1e6:.1f} MB)")
    print("  Volume 1 = solid (mat:%s), Volume 2 = air (mat:%s)" % tuple(a.mats.split(",")))
    print("  Surface 1 shared [solid|air], Surface 2 = boundary:vacuum")


if __name__ == "__main__":
    main()
