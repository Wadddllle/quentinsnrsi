"""Tile-native OneMap domain -> watertight CFD STL, end to end.

Real SLA LiDAR building meshes (not LoD1 boxes) + real terrain, for any
Singapore domain. Pipeline:

  1. domain_leaf_tiles: finest leaf tiles covering the domain (tiles.py)
  2. extract_domain_buildings: whole-mesh, correct 3D-Tiles transform, clipped
     to domain, split into per-building pieces (extract.py)
  3. build_domain_dtm + place_on_terrain + flatten_pads + terrain_surface_mesh:
     sit each building on graded terrain with a plunge skirt (terrain.py)
  4. write a terrain+buildings OBJ, then Blender fuse (blender/fuse_stl.py):
     solidify terrain slab -> join -> voxel remesh -> drop debris -> STL
  5. sbg.blender.repair_stl.verify_and_repair: fast_simplification decimate +
     pymeshfix -> watertight STL

Usage:
  python -m sbg.onemap_native.build \
      --bbox 25300,28500,27700,30500 -o data/queenstown_native.stl
  python -m sbg.onemap_native.build \
      --domain-geojson domain.geojson -o out.stl --voxel-size 2.0
"""
import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
from pyproj import Transformer

from sbg.config import BLENDER_PATH
from sbg.cutout import load_domain_polygon
from sbg.onemap_native.extract import extract_domain_buildings
from sbg.onemap_native.terrain import (
    COUPLING_LAMBDA,
    DtmSampler, build_domain_dtm, place_on_terrain_conforming, terrain_flat_base_solid,
)
from sbg.onemap_native.tiles import domain_leaf_tiles

_svy_to_wgs = Transformer.from_crs("EPSG:3414", "EPSG:4326", always_xy=True)
_FUSE_SCRIPT = Path(__file__).parent / "blender" / "fuse_stl.py"
# Floating-debris removal. A disconnected component is a floating remesh sliver
# (not a real building) if its BOTTOM sits above the local ground AND it's small.
# Area alone doesn't discriminate (measured slivers are thin 17-56m^2 sheets, same
# scale as a small building footprint) -- floating-vs-grounded is the real signal.
DEBRIS_FLOAT_GAP_M = 2.0   # component z-min this far above local DTM ground => floating
DEBRIS_MAX_FACES = 1000    # only drop SMALL floating bits; a large floating structure is kept


def _fuse_meshlib(terr_v, terr_f, bld_v, bld_f, voxel_size, log):
    """Voxel-remesh the terrain+buildings soup with meshlib instead of Blender.

    Blender's REMESH(VOXEL) modifier and meshlib's offsetMesh(offset=0) both call
    OpenVDB, so this is the same algorithm without the ~300 MB dependency. A/B on a
    real production intermediate (Kent Ridge 900 m, terrain.ply + buildings.ply from
    this very function's inputs) -- see research_2/scripts/fuse_ab.py:

        Blender REMESH        3.4s  1,300,768 faces  holes 0  comps 5  selfX 0  37,385,263 m^3
        offsetMesh OpenVDB    0.8s  1,300,768 faces  holes 0  comps 5  selfX 0  37,385,264 m^3

    Identical face count; volume differs by 1 m^3 in 37.4 million (3e-6%). 4.25x faster.

    signDetectionMode MUST be OpenVDB. Measured on the same input: Unsigned returns
    ZERO faces; HoleWindingRule takes 159 s and fragments into 429 components with a
    12% volume error. mcOffsetMesh (standard rather than dual marching cubes) yields
    1 component instead of 5 but introduces 75 self-intersections -- not worth it.

    The input soup is deliberately NOT closed (buildings plunged into a terrain slab,
    overlaps left in -- that is the whole point of remeshing rather than booleaning),
    which is exactly why sign detection has to come from OpenVDB's flood fill.
    """
    import meshlib.mrmeshpy as mr
    import meshlib.mrmeshnumpy as mn

    verts = np.vstack([terr_v, bld_v])
    faces = np.vstack([terr_f, bld_f + len(terr_v)])
    src = mn.meshFromFacesVerts(np.ascontiguousarray(faces, np.int32),
                                np.ascontiguousarray(verts, float))
    log(f"[fuse] meshlib voxel remesh {voxel_size}m over {len(faces):,} soup faces...")
    p = mr.OffsetParameters()
    p.voxelSize = voxel_size
    p.signDetectionMode = mr.SignDetectionMode.OpenVDB
    out = mr.offsetMesh(mr.MeshPart(src), 0.0, p)
    log(f"[fuse] meshlib fuse done: {out.topology.numValidFaces():,} faces")
    return out


def _write_scene_ply(terr_path, bld_path, terr_v, terr_f, bld_v, bld_f):
    """Write terrain + buildings as two BINARY PLYs (~18x faster than an ASCII OBJ
    at domain scale -- 40s->2s -- and exact-roundtrip; the ASCII float formatting was
    the whole cost). fuse_stl.py imports both; the two-file split replaces the OBJ
    'o terrain'/'o buildings' object markers, which binary PLY has no equivalent of."""
    import trimesh
    trimesh.Trimesh(terr_v, terr_f, process=False).export(str(terr_path), encoding="binary")
    trimesh.Trimesh(bld_v, bld_f, process=False).export(str(bld_path), encoding="binary")


def _polygon_clip_solid(domain_polygon, z_lo, z_hi):
    """A closed vertical prism from the domain polygon (EPSG:3414), spanning
    [z_lo, z_hi], as a meshlib Mesh -- the exact volume the fused mesh is boolean-
    intersected against. Rectangle/point-buffer domains give a box; an ARBITRARY
    polygon gives the true polygon domain (vertical CFD walls along each edge), not
    just its bounding box. Falls back to the bbox for a non-Polygon (e.g. a stray
    MultiPolygon)."""
    import trimesh
    import meshlib.mrmeshnumpy as mn
    if domain_polygon.geom_type != "Polygon":
        xmin, ymin, xmax, ymax = domain_polygon.bounds
        return mr.makeCube(mr.Vector3f(xmax - xmin, ymax - ymin, z_hi - z_lo),
                           mr.Vector3f(xmin, ymin, z_lo))
    prism = trimesh.creation.extrude_polygon(domain_polygon, height=z_hi - z_lo)
    prism.apply_translation([0.0, 0.0, z_lo])  # extrude starts at z=0
    return mn.meshFromFacesVerts(prism.faces, prism.vertices)




def _provenance(store_dir=None, opts=None):
    """Fields identifying WHAT produced an STL, for every sidecar.

    An STL carries no metadata (80-byte header, then triangles), so a shipped mesh is
    otherwise untraceable: a year later nobody can say which store, which options or
    which commit made it. Cheap to record, impossible to reconstruct after the fact.
    """
    import subprocess
    from datetime import datetime, timezone

    def _git(*args):
        try:
            return subprocess.run(("git", *args), cwd=Path(__file__).resolve().parent,
                                  capture_output=True, text=True, timeout=5,
                                  check=True).stdout.strip() or None
        except Exception:
            return None

    store = None
    if store_dir and Path(store_dir).is_dir():
        # mtime of the store root is a cheap stand-in for "which tile download built
        # this" -- hashing a 2.6 GB tree on every build is not worth it.
        store = {"path": str(store_dir),
                 "mtime_utc": datetime.fromtimestamp(Path(store_dir).stat().st_mtime,
                                                     timezone.utc).isoformat()}
    return {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git("rev-parse", "HEAD"),
        "git_dirty": bool(_git("status", "--porcelain")),
        "store": store,
        "options": dict(opts or {}),
    }


def _write_build_sidecar(stl_path, domain_poly, core_poly, h_exact, log,
                         store_dir=None, opts=None, dem_name=None):
    """`<stem>.build.json` for a plain (non-wind) build.

    A wind build already gets `<stem>.wind.json`, which carries the same provenance plus
    the rotation. This is the equivalent for everything else -- without it a normal build
    ships a bare STL with no record of the domain that produced it, which is the single
    thing a scientist needs to reproduce or cite a result.
    """
    import json
    import meshlib.mrmeshpy as mr
    import meshlib.mrmeshnumpy as mn

    doc = {"version": 1, "kind": "sbg.build", "crs": "EPSG:3414", "units": "m",
           "domain_ring_m": np.asarray(domain_poly.exterior.coords,
                                       dtype=float)[:, :2].tolist(),
           "core_roi_ring_m": np.asarray(core_poly.exterior.coords,
                                         dtype=float)[:, :2].tolist(),
           "tallest_building_m": float(h_exact), "wind": None,
           **_provenance(store_dir, opts)}
    try:
        ml = mr.loadMesh(str(stl_path))
        v = mn.getNumpyVerts(ml)
        holes = len(ml.topology.findHoleRepresentiveEdges())
        doc.update(watertight=holes == 0, holes=holes,
                   faces=int(ml.topology.numValidFaces()),
                   mesh_bounds_m={"xmin": float(v[:, 0].min()), "xmax": float(v[:, 0].max()),
                                  "ymin": float(v[:, 1].min()), "ymax": float(v[:, 1].max()),
                                  "zmin": float(v[:, 2].min()), "zmax": float(v[:, 2].max())})
    except Exception as e:                       # a raw/soup export may not load cleanly
        doc["mesh_stats_error"] = str(e)

    _r = _raw_path(stl_path)
    if _r.exists():
        doc["raw_stl"] = _r.name
        doc["raw_note"] = _RAW_NOTE
    if dem_name:
        doc["dem_tif"] = dem_name
        doc["dem_note"] = _DEM_NOTE

    p = Path(stl_path).with_suffix(".build.json")
    p.write_text(json.dumps(doc, indent=2))
    log(f"[sidecar] {p.name}")
    return p


def _write_wind_sidecar(stl_path, wind_from_deg, psi_deg, centre, core_poly,
                        rect_rot, wind, h_exact, log, clipped=True,
                        store_dir=None, opts=None, dem_name=None):
    """Write `<stem>.wind.json` next to an STL. Returns a dict describing the output.

    Written HERE and not in the UI job layer, because the rotation is a property of the
    geometry -- a CLI run must produce it too. Deliberately a different suffix from
    dose/stl_to_h5m.py's `<stem>.transform.json`, so the two compose by stem without
    clashing: wind_225.stl -> wind_225.wind.json (geo frame, this file) plus
    wind_225.transform.json (units/origin, written later in the pymoab env).

    THE FRAME, stated once so nobody has to re-derive it: the STL vertices are ALREADY
    rotated. The .h5m, Fluent, the particle tracks and any tally mesh therefore all live
    in the ROTATED frame. Apply `inverse` ONLY when producing a georeferenced product --
    un-rotating tracks before binning would place the source where the geometry is not.
    """
    import json
    import meshlib.mrmeshpy as mr
    import meshlib.mrmeshnumpy as mn
    from sbg.onemap_native.wind import rot2, rotate_xy

    ml = mr.loadMesh(str(stl_path))
    v = mn.getNumpyVerts(ml)
    holes = len(ml.topology.findHoleRepresentiveEdges())
    R = rot2(psi_deg)
    x0, y0, x1, y1 = rect_rot.bounds
    core_ring = np.asarray(core_poly.exterior.coords, dtype=float)[:, :2]
    core_rot = rotate_xy(core_ring, psi_deg, centre)

    doc = {
        "version": 1, "kind": "sbg.wind_domain", "crs": "EPSG:3414", "units": "m",
        **_provenance(store_dir, opts),
        "wind_from_deg": float(wind_from_deg),
        "psi_deg": float(psi_deg),
        "convention": ("wind_from_deg is METEOROLOGICAL: the compass bearing the wind "
                       "blows FROM, clockwise from grid north. psi_deg = "
                       "(wind_from_deg + 180) % 360 is the bearing the flow travels "
                       "TOWARD, and is the CCW rotation applied to world XY so that the "
                       "flow ends up along +Y."),
        "rotation": {
            "centre_m": [float(centre[0]), float(centre[1])],
            "axis": "z",
            "forward_matrix": [[float(R[0, 0]), float(R[0, 1])],
                               [float(R[1, 0]), float(R[1, 1])]],
            "inverse_matrix": [[float(R[0, 0]), float(R[1, 0])],
                               [float(R[0, 1]), float(R[1, 1])]],
            "forward": "p_rot_xy = forward_matrix @ (p_world_xy - centre_m) + centre_m; z unchanged",
            "inverse": "p_world_xy = inverse_matrix @ (p_rot_xy - centre_m) + centre_m; z unchanged",
            "note": ("The STL vertices are ALREADY in the rotated frame, and so are the "
                     ".h5m, the Fluent case and the particle tracks. Apply `inverse` only "
                     "when producing a georeferenced product (e.g. a dose map on a real "
                     "basemap). Do NOT un-rotate tracks before binning them -- the OpenMC "
                     "geometry lives in this rotated frame."),
        },
        "clipped": bool(clipped),
        "mesh_bounds_rotated": {
            "xmin": float(v[:, 0].min()), "xmax": float(v[:, 0].max()),
            "ymin": float(v[:, 1].min()), "ymax": float(v[:, 1].max()),
            "zmin": float(v[:, 2].min()), "zmax": float(v[:, 2].max()),
        },
        "core_roi": {
            "ring_world_m": core_ring.tolist(),
            "rotated_bounds": {"xmin": float(core_rot[:, 0].min()),
                               "xmax": float(core_rot[:, 0].max()),
                               "ymin": float(core_rot[:, 1].min()),
                               "ymax": float(core_rot[:, 1].max())},
        },
        "buffer": {
            "upwind_m": float(wind["up_m"]), "downwind_m": float(wind["down_m"]),
            "lateral_m": float(wind["lat_m"]),
            "H_m": float(h_exact),
            "H_source": "tallest extracted building (max vertex z - piece base_z)",
            "contents": "terrain only, no buildings",
            "guidance": "COST 732 / Franke et al. (2007) urban CFD best practice",
        },
        "watertight": holes == 0, "holes": holes, "faces": int(ml.topology.numValidFaces()),
    }
    if clipped:
        doc["domain_rotated"] = {
            "xmin": float(x0), "xmax": float(x1), "ymin": float(y0), "ymax": float(y1),
            "inlet": {"face": "ymin", "y": float(y0), "flow_normal": [0, 1, 0]},
            "outlet": {"face": "ymax", "y": float(y1)},
            "lateral": ["xmin", "xmax"],
        }
        doc["boundary_conditions"] = {
            "inlet": {"face": "ymin",
                      "fluent": "Velocity Inlet, Magnitude Normal to Boundary",
                      "note": ("Geometry is pre-rotated so the wind is always +Y. The "
                               "Fluent setup is IDENTICAL for every direction.")},
            "ground_roughness": {
                "z0_m": float(wind.get("z0_m", 0.7)),
                "advisory": True,
                "basis": ("urban/suburban aerodynamic roughness length. The buffer is bare "
                          "terrain with no buildings, so upstream roughness must be "
                          "supplied as a wall BC. The mesh does NOT encode this."),
                "fluent_hint": ("sand-grain Ks = 9.793 * z0 / Cs (Cs default 0.5). Check "
                                "the first cell height exceeds Ks or the rough-wall law "
                                "is invalid."),
            },
        }
    else:
        doc["note_unclipped"] = (
            "The mesh extent is the BUILD ENVELOPE, not the wind rectangle, so "
            "'inlet = ymin' is not a claim about its walls. This is fine for Ansys "
            "fault-tolerant meshing and snappyHexMesh, which build their own enclosure / "
            "background mesh; it is not a watertight CFD domain.")
    if holes:
        doc["note_open"] = (
            "RAW mode: this is an OPEN triangle soup, not a closed solid. The extent is "
            "the wind rectangle above, but the domain walls themselves are open cuts -- "
            "the consumer builds its own enclosure (Ansys fault-tolerant meshing, "
            "snappyHexMesh). The Ansys watertight workflow will reject it; use a voxel "
            "size > 0 for that.")

    _r = _raw_path(stl_path)
    if _r.exists():
        doc["raw_stl"] = _r.name
        doc["raw_note"] = _RAW_NOTE
    if dem_name:
        doc["dem_tif"] = dem_name
        doc["dem_note"] = _DEM_NOTE

    p = Path(stl_path).with_suffix(".wind.json")
    p.write_text(json.dumps(doc, indent=2))
    log(f"[sidecar] {p.name}  (wind from {wind_from_deg:g}, "
        f"{'watertight' if holes == 0 else f'{holes} holes'})")
    return {"wind_from_deg": float(wind_from_deg), "stl": str(stl_path),
            "sidecar": str(p), "watertight": holes == 0, "holes": holes,
            "faces": int(ml.topology.numValidFaces())}


_RAW_NOTE = ("Same geometry, same domain, same rotation as this STL -- built in the same "
             "run, so they cannot disagree. The raw file is the un-fused OneMap soup at "
             "full LiDAR detail: use it for Ansys fault-tolerant meshing and "
             "snappyHexMesh, which build their own enclosure. Use the watertight file for "
             "DAGMC/OpenMC, which must ray-trace a closed solid. Do not mix a raw file "
             "from one run with a watertight file from another.")


_DEM_NOTE = ("A GeoTIFF where every pixel is a height (terrain with buildings baked in), "
             "sampled from the same un-fused geometry as this STL, in the same run. Always "
             "north-up in TRUE world coordinates -- under a wind rose there is ONE DEM for "
             "the whole build, not one per direction, because the geometry is identical for "
             "every bearing and only the clip differs. So it does NOT share the rotated "
             "frame of the wind-aligned STLs: un-rotate rotated-frame coordinates (see the "
             "`rotation` block of the .wind.json sidecar) before georeferencing against it.")


def _raw_path(stl_path):
    """`domain.stl` -> `domain.raw.stl`. One place, so the two writers cannot drift."""
    p = Path(stl_path)
    return p.with_name(p.stem + ".raw" + p.suffix)


def _export_raw(verts, faces, out_path, log, *, rect_rot=None, label=""):
    """Write the un-fused OneMap soup to STL, optionally trimmed to an axis-aligned rect.

    NOT a boolean. When a rect is given the mesh is already rotated, so the rectangle is
    axis-aligned and four per-triangle plane cuts do the job with no CSG and no
    watertightness assumption. `cap=False` is deliberate -- raw's contract is an open
    soup, so the cut leaves an open wall exactly like every other raw boundary.
    """
    import trimesh
    m = trimesh.Trimesh(verts, faces, process=False)
    if rect_rot is not None:
        x0, y0, x1, y1 = rect_rot.bounds
        for origin, normal in (((x0, 0, 0), (1, 0, 0)), ((x1, 0, 0), (-1, 0, 0)),
                               ((0, y0, 0), (0, 1, 0)), ((0, y1, 0), (0, -1, 0))):
            m = m.slice_plane(plane_origin=origin, plane_normal=normal, cap=False)
            if m is None or len(m.faces) == 0:
                raise RuntimeError(
                    f"raw clip left an empty mesh{label}; the build envelope does not "
                    f"cover this direction's rectangle")
    m.export(str(out_path))
    log(f"[raw] {Path(out_path).name}: {len(m.faces):,} faces (open soup, full LiDAR detail)")
    return len(m.faces)


def _finish_one(mesh_v, mesh_f, clip_polygon, out_stl, log, *,
                expect_disconnected=False):
    """Clip -> debris drop -> polish -> shipped-format verify -> optional strip-base
    -> export. Returns the output path.

    Takes NUMPY ARRAYS, not a meshlib Mesh, and rebuilds the mesh internally. That is
    deliberate and load-bearing for the per-direction wind loop: mr.boolean,
    deleteFaces, pack and resolveMeshDegenerations all mutate IN PLACE, so handing the
    same Mesh object to successive directions would corrupt direction 2 onward. Each
    call owns its geometry. Do not "optimise" this into reuse.
    """
    import meshlib.mrmeshpy as mr
    import meshlib.mrmeshnumpy as mn
    mesh = mn.meshFromFacesVerts(np.ascontiguousarray(mesh_f, np.int32),
                                 np.ascontiguousarray(mesh_v, float))
    # local aliases so the body below is unchanged from when it lived inline
    domain_polygon = clip_polygon
    log(f"[clip] meshlib boolean intersect with exact domain polygon...")
    bb = mesh.computeBoundingBox()
    # Nudge the clip polygon OFF the voxel grid before cutting. The remesh puts
    # vertices on a voxel_size grid and domain bounds are round numbers, so the cut
    # plane lands exactly on grid-aligned vertices and the retriangulation emits
    # collinear zero-area slivers there -- measured as bad edges of exactly 8.00 m
    # and 16.00 m (4 and 8 voxels) on the clip walls. Same coincident-surface class
    # the include_base=False terrain subtraction already dodges with a scale nudge.
    # 1 mm is ~3 orders below the CFD cell and below STL's own float32 resolution
    # at SVY21 coords, so the domain is unchanged for any practical purpose.
    _nudge = float(os.environ.get("SBG_CLIP_NUDGE_M", "0.001"))
    _clip_poly = domain_polygon.buffer(-_nudge) if _nudge else domain_polygon
    if _clip_poly.is_empty or _clip_poly.geom_type != "Polygon":
        _clip_poly = domain_polygon
    clip_solid = _polygon_clip_solid(_clip_poly, bb.min.z - 10.0, bb.max.z + 10.0)
    res = mr.boolean(mesh, clip_solid, mr.BooleanOperation.Intersection)
    if not res.valid():
        raise RuntimeError("meshlib boolean clip failed (invalid result)")
    clipped = res.mesh
    holes = len(clipped.topology.findHoleRepresentiveEdges())
    log(f"[clip] watertight={holes == 0} (meshlib holes={holes}), "
        f"{clipped.topology.numValidFaces()} faces")

    # Drop floating debris: the voxel remesh + boolean cut leave a few
    # DISCONNECTED thin slivers floating in the air (measured ~4-5 per domain,
    # 6-26 faces, 17-56m^2, hovering 6-60m up). Harmless for CFD (far below the ~3m
    # grid) but dirty. A component is a floating sliver if its BOTTOM sits above the
    # local DTM ground (grounded buildings penetrate the slab, so their z-min is at
    # or below ground) AND it's small (a large floating structure -- rare, e.g. a
    # captured skybridge -- is kept). Done HERE on the clean boolean output (real
    # connected components), NOT on the reloaded STL soup below (whose components
    # are welding artifacts). Deleting a whole disconnected component opens no
    # boundary in the rest, so watertightness is preserved.
    # ...UNLESS there is no terrain. `expect_disconnected` is include_base=False, where
    # EVERY building is legitimately its own component because there is no slab to plunge
    # into -- so the "connected by construction" premise above is simply false and the
    # size rule deletes the whole domain except its single largest building. (Measured:
    # exactly that, one building out no matter the domain size.) There, only drop what
    # provably cannot be a closed volume: fewer than 4 faces, since the minimum closed
    # solid is a tetrahedron.
    comps = mr.MeshComponents.getAllComponents(mr.MeshPart(clipped))
    _max_debris = 3 if expect_disconnected else DEBRIS_MAX_FACES
    if len(comps) > 1:
        largest = max(comps, key=lambda cc: cc.count())
        drop = mr.FaceBitSet()
        ndrop = 0
        for comp in comps:
            if comp is largest or comp.count() > _max_debris:
                continue
            # The terrain slab is part of this mesh and every building plunges
            # PLUNGE_M into it, so a real building is connected to the main body BY
            # CONSTRUCTION. Any small DISCONNECTED component is therefore debris
            # whatever its elevation -- the older "floating AND small" rule missed
            # ground-level fragments (a real CBD run left 4 components of 4-28 faces
            # sitting at z~17-23m in one 20m patch, which broke strict
            # watertightness). Elevation is still logged for the rare large
            # floating structure, which DEBRIS_MAX_FACES already protects.
            drop |= comp
            ndrop += 1
        if drop.count():
            clipped.deleteFaces(drop)
            clipped.pack()
            log(f"[debris] dropped {ndrop} disconnected slivers ({drop.count()} faces), "
                f"{clipped.topology.numValidFaces()} faces remain, "
                f"holes={len(clipped.topology.findHoleRepresentiveEdges())}")

    # Final polish: edge-collapse the handful of zero-area sliver faces the voxel
    # remesh + boolean cut leave behind (measured ~3-6 in >1.3M faces, at interior
    # remesh pinch points and the clip boundary). meshlib's hole count already reads
    # 0 (no boundary), but these slivers make a STRICT exact-position re-weld (trimesh
    # process=True, and CFD meshers like snappyHexMesh / Ansys watertight-geometry)
    # see spurious non-manifold edges. resolveMeshDegenerations collapses them, so the
    # STL is clean even under a strict weld -- NOT the aggressive fixMeshDegeneracies,
    # which remeshes (tripled the face count + introduced non-manifold edges in test).
    # Final polish for STRICT watertightness. meshlib already reads holes=0 /
    # multiEdges=False, but the voxel remesh + boolean cut leave a few zero-area
    # collinear sliver faces (~3-13 in >1.3M). meshlib keeps them in a valid
    # manifold topology, but an exact-position RE-WELD (trimesh process=True, and
    # strict CFD meshers -- snappyHexMesh / Ansys watertight-geometry) collapses each
    # sliver's collinear verts into a non-manifold edge -> "not watertight".
    # The fix: save+reload (the STL weld EXPOSES those coincident pinch verts, which
    # meshlib kept separate) then iterate resolveMeshDegenerations at a CONSERVATIVE
    # 1cm budget until it stops making progress. Iterating (not one pass) is what
    # clears the pinches; a bigger maxDeviation reaches 0 in one pass but collapses
    # ~12-15% of LEGITIMATE thin triangles (measured), so keep it tight and accept
    # the odd residual collinear zero-area face (still manifold, doesn't break
    # watertightness). Net: strictly watertight even under a re-weld, ~150 faces lost.
    mr.saveMesh(clipped, str(out_stl))
    polished = mr.loadMesh(str(out_stl))
    # The STL round-trip can OPEN a hole: `clipped` carries a couple of hundred
    # zero-area degenerate faces, and an STL write/read drops degenerate triangles,
    # so one that was bridging the clip wall leaves a planar gap. Measured on a real
    # 2km CBD domain: holes=0 before the save, holes=1 straight after the reload (a
    # 31x30m gap in the x=30500 wall). NOT a float32-precision effect -- recentring
    # the mesh on the origin before saving was tried and changed nothing. The region
    # is a flat wall, so filling is exact, not a patch.
    n_reload_holes = len(polished.topology.findHoleRepresentiveEdges())
    if n_reload_holes:
        for _ in range(3):
            edges = polished.topology.findHoleRepresentiveEdges()
            if not len(edges):
                break
            for e in edges:
                try:
                    mr.fillHole(polished, e, mr.FillHoleParams())
                except Exception:
                    pass
    log(f"[polish] after save+reload: holes={n_reload_holes} -> "
        f"{len(polished.topology.findHoleRepresentiveEdges())} (filled)")
    n0 = mr.findDegenerateFaces(mr.MeshPart(polished)).count()
    if n0:
        rs = mr.ResolveMeshDegenSettings()
        rs.maxDeviation = 1e-2
        rs.tinyEdgeLength = 1e-2
        n_deg = n0
        # Iterate the WHOLE save+reload+resolve cycle, not just resolve. The reload
        # is what exposes coincident pinch verts (meshlib keeps them separate), so
        # each round-trip surfaces a new generation of them; resolving alone stalls.
        # Measured on a 2km CBD domain: resolve-only stalled at 57 degenerate faces
        # and the output failed a strict re-weld, while NUH (10 residual) passed.
        for _cycle in range(4):
            for _ in range(5):
                mr.resolveMeshDegenerations(polished, rs)
                new = mr.findDegenerateFaces(mr.MeshPart(polished)).count()
                if new >= n_deg:
                    n_deg = new
                    break
                n_deg = new
                if n_deg == 0:
                    break
            if n_deg == 0:
                break
            mr.saveMesh(polished, str(out_stl))
            polished = mr.loadMesh(str(out_stl))
            for e in polished.topology.findHoleRepresentiveEdges():
                try:
                    mr.fillHole(polished, e, mr.FillHoleParams())
                except Exception:
                    pass
            after = mr.findDegenerateFaces(mr.MeshPart(polished)).count()
            log(f"[polish] cycle {_cycle + 1}: {n_deg} -> {after} degenerate, "
                f"holes={len(polished.topology.findHoleRepresentiveEdges())}")
            if after >= n_deg:
                n_deg = after
                break
            n_deg = after
        log(f"[polish] after resolveMeshDegenerations: holes="
            f"{len(polished.topology.findHoleRepresentiveEdges())}")
        # resolveMeshDegenerations DISCONNECTS tiny slivers as it collapses the
        # faces around them, so a second component sweep is needed HERE, after the
        # polish -- the earlier one (on `clipped`) runs before these exist. A real
        # end-to-end run finished with five 2-face components alongside the one
        # real body: meshlib still read holes=0, but a strict re-weld (and any CFD
        # mesher doing the same) called the result non-watertight. Anything under 4
        # faces provably cannot be a closed volume, so this is exact, not a
        # heuristic, and it needs no floating/elevation test.
        pcomps = mr.MeshComponents.getAllComponents(mr.MeshPart(polished))
        if len(pcomps) > 1:
            biggest = max(pcomps, key=lambda cc: cc.count())
            slivers = mr.FaceBitSet()
            nsliv = 0
            for comp in pcomps:
                if comp is not biggest and comp.count() < 4:
                    slivers |= comp
                    nsliv += 1
            if slivers.count():
                polished.deleteFaces(slivers)
                polished.pack()
                log(f"[polish] dropped {nsliv} post-polish sliver components "
                    f"({slivers.count()} faces)")
        log(f"[polish] round-trip resolveMeshDegenerations: {n0} -> {n_deg} degenerate, "
            f"multiEdges={mr.hasMultipleEdges(polished.topology)}, "
            f"{polished.topology.numValidFaces()} faces")

    # Drop EXACTLY-zero-area faces, then close what that opens. This is what
    # actually makes the output pass a strict re-weld.
    #
    # Ground truth on a real 2km CBD domain: the output had 4 non-manifold edges
    # (of 505,007) and every one of them was shared by COLLINEAR ZERO-AREA faces
    # lying along x=30500 / z=-10. resolveMeshDegenerations cannot remove them --
    # it is driven by tinyEdgeLength (1cm) and these degenerate triangles have
    # 16-48m edges, which is exactly why it stalls (233 -> 57 and then no further
    # progress no matter how many cycles are run).
    #
    # An exact area test is used rather than meshlib's own findDegenerateFaces:
    # meshlib counted 57 where the real zero-area count was 15, so its criterion
    # is looser and would delete genuinely thin (but real) faces. Deleting these
    # opened 3 holes -- some degenerate faces were the only thing spanning an
    # edge -- so the fill is required, not optional. Verified: volume unchanged to
    # 1e-6 m^3, edge histogram becomes {2: all}, strict watertight True.
    # ITERATED, deliberately: mr.fillHole closes the openings the drop creates,
    # but the patch it emits along a collinear clip-wall boundary can itself be
    # zero-area, so a single pass leaves some behind. Caught on the Kent Ridge
    # domain -- one pass shipped 3 non-manifold edges (4 faces each, all exactly
    # on the x=22850 clip wall, 16-25.6m long, i.e. the documented coincident-
    # surface artifact); iterating clears them. Capped, and stops early on no
    # progress, so a boundary that cannot be cleaned can't spin here.
    import meshlib.mrmeshnumpy as _mn
    _dropped = 0
    for _pass in range(4):
        _v = _mn.getNumpyVerts(polished)
        _f = _mn.getNumpyFaces(polished.topology)
        # Test degeneracy at the precision the SHIPPED FORMAT stores, not float64.
        # STL is float32; at SVY21 coords (~30,000 m) that is ~2 mm spacing, so a
        # 40 m x 2 mm collinear sliver has area 0.04 m^2 in memory -- 1e8 times above
        # any sane threshold -- and collapses to EXACTLY zero only once written.
        # Measured with fuse_backend="meshlib" on Kent Ridge: two such slivers on the
        # ymax clip wall (4 edges, 80 m perimeter, all four vertices at y=31150 AND
        # z=8.0, i.e. collinear) passed this test in float64, were written, and
        # reopened as 2 holes on reload. Rounding to float32 first catches them.
        _tri = _v[_f].astype(np.float32).astype(np.float64)
        _area = 0.5 * np.linalg.norm(
            np.cross(_tri[:, 1] - _tri[:, 0], _tri[:, 2] - _tri[:, 0]), axis=1)
        _bad = _area < 1e-9
        if not _bad.any():
            break
        _dropped += int(_bad.sum())
        polished = _mn.meshFromFacesVerts(
            np.asarray(_f[~_bad], dtype=np.int32), np.asarray(_v, dtype=np.float64))
        # STITCH BEFORE FILLING. Dropping a collinear sliver on the clip wall can
        # leave a DEGENERATE FLAP -- a boundary that runs out along a line and back
        # along the same line, pinching at a repeated vertex, enclosing zero area.
        # Measured on Kent Ridge (fuse_backend="meshlib"): loop
        # (22090,31150,8) -> (22106) -> (22090) -> (22066) -> back, v0 == v2 exactly.
        # fillHole on such a loop emits MORE zero-area triangles, which the next
        # pass drops, which reopens it -- an oscillation that hits the pass cap and
        # ships a mesh that is closed in memory only by degenerate faces, so an STL
        # reload (which discards them) reopens the hole. Uniting the coincident
        # boundary vertices closes the flap properly, adding no geometry.
        try:
            mr.uniteCloseVertices(polished, 1e-6, True)
        except Exception:
            pass
        for _ in range(4):
            edges = polished.topology.findHoleRepresentiveEdges()
            if not len(edges):
                break
            for e in edges:
                try:
                    mr.fillHole(polished, e, mr.FillHoleParams())
                except Exception:
                    pass
    if _dropped:
        log(f"[polish] dropped {_dropped} zero-area faces over {_pass + 1} pass(es), "
            f"refilled -> holes="
            f"{len(polished.topology.findHoleRepresentiveEdges())}, "
            f"{polished.topology.numValidFaces()} faces")
    # Rewrite unconditionally: the file on disk is the pre-polish `clipped` save,
    # so skipping this when n0 == 0 would ship the unpolished, hole-filled-less mesh.
    mr.saveMesh(polished, str(out_stl))

    # Report closure IN THE SHIPPED FORMAT, not just in memory. STL stores float32
    # per-triangle soup, so a re-weld can OPEN holes that were absent in memory --
    # measured on Kent Ridge with fuse_backend="meshlib": every in-memory stage
    # logged holes=0 and the written STL reloads with 2. (Same class as the
    # manifold/README warning that STL loses topology; here it costs closure.)
    # This is a CHECK, not a repair: iterating fillHole on the reloaded mesh was
    # tried and oscillates (2 -> 1 -> 1 -> 2) while introducing a multi-edge, so
    # it is deliberately not attempted. See the fuse_backend note.
    _shipped = len(mr.loadMesh(str(out_stl)).topology.findHoleRepresentiveEdges())
    if _shipped:
        log(f"[verify] WARNING: the written STL reloads with {_shipped} hole(s) "
            f"(in-memory holes=0). Not strictly watertight as shipped.")
    else:
        log(f"[verify] shipped STL reloads closed (holes=0)")

    return out_stl


def build_domain_stl(domain_polygon, out_stl, step=5.0, voxel_size=2.0,
                     target_reduction=0.97, decimate_error=2.5, workers=6,
                     store_dir=None, workdir=None, log=None, include_base=True,
                     fuse_backend="meshlib", core_polygon=None, wind=None,
                     placement="drape", coupling_lambda=None, check_contours=False,
                     crossing="auto", include_raw=False, dem=False, dem_px_m=4.0,
                     dem_crs=3414, dem_agg="median", dem_overhang="keep",
                     dem_supersample=4, dem_source="surface", dem_only=False):
    """Run the full tile-native pipeline for one domain polygon (EPSG:3414).

    `core_polygon` is the region of interest -- the polygon that decides which BUILDINGS
    are kept. `domain_polygon` is the terrain extent and the mesh clip. They are the same
    object unless a wind buffer was built, in which case domain_polygon is the (larger)
    build envelope and core_polygon is the ROI the user actually drew.

    `wind` (optional) turns this into a build-once/rotate-and-clip-per-direction run:
    every heavy stage runs ONCE and each extra bearing costs only a rotate + clip +
    polish. Returns a list of per-direction dicts instead of a single path.

    `include_raw` additionally writes the un-fused soup as `<stem>.raw.stl` beside each
    watertight output. The dose pipeline needs BOTH -- raw for Ansys fault-tolerant
    meshing (the CFD side), watertight for DAGMC (which has to ray-trace a closed solid)
    -- and they must describe the same geometry or the particle tracks and the .h5m
    silently disagree. Producing them from one run makes that mismatch impossible;
    building them as two separate jobs makes it invisible. Nearly free, because
    everything up to the fuse is shared and the raw export is just the soup already in
    memory. Ignored when voxel_size <= 0, where the raw file IS the output.

    `dem` additionally writes `<stem>.dem.tif` -- a GeoTIFF where every pixel is a height,
    which is what the JAEA dose code ingests. It is sampled from the SAME un-fused arrays
    the raw export uses, before the fuse: a top-down max never asks whether a mesh is
    closed, so the voxel staircase and decimation error of the finished STL are pure loss
    here. Under wind it is written ONCE, in the true world frame: geometry is built once
    and only the clip differs per direction, so one north-up raster serves every bearing --
    and north-up is the only thing ModelPixelScale + ModelTiepoint can express anyway.
    See sbg/onemap_native/dem.py for the two sampling knobs (`dem_agg`, `dem_overhang`).
    """
    import time
    _sink = log
    _t = [time.perf_counter()]

    def log(msg):  # flushing + per-phase timing wrapper
        now = time.perf_counter()
        line = f"{msg}  (+{now - _t[0]:.1f}s)"
        _t[0] = now
        if _sink is None:
            print(line, flush=True)
        else:
            _sink(line)

    workdir = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="onemap_native_"))
    workdir.mkdir(parents=True, exist_ok=True)

    core_polygon = domain_polygon if core_polygon is None else core_polygon
    # Asking for the GeoTIFF alone implies asking for the GeoTIFF.
    if dem_only:
        dem = True

    # What happens to a building straddling the ROI edge. "auto" is the only setting
    # that is right in both worlds, so it is the default:
    #
    #   no buffer  -> the ROI *is* the clip line, so a straddler gets sliced flat.
    #                 Drop it (Deliverable 3 semantics, matches the preview's "kept").
    #   buffer on  -> the clip is at the wind rectangle, hundreds of metres out, so
    #                 nothing slices it. Keep it whole, or the ROI perimeter ends up a
    #                 ring of bare ground exactly where the roughness transition is.
    #
    # Explicit "drop"/"keep" override it. Note `core_polygon is domain_polygon` is an
    # identity test on purpose -- they are the SAME object when no buffer was built.
    crossing = crossing or "auto"          # None == "not specified"
    if crossing not in ("auto", "drop", "keep"):
        raise ValueError(f"crossing must be auto|drop|keep, got {crossing!r}")
    buffered = core_polygon is not domain_polygon
    require_full = (not buffered) if crossing == "auto" else (crossing == "drop")

    # Fail before doing 30s of work, not after. include_base=False puts every building on a
    # common z=0 datum with no terrain at all (see the FLAT-GROUND branch below), so a
    # terrain raster there would be neither ground nor buildings-at-their-real-elevation --
    # it would be a plausible-looking lie. Refuse rather than emit one.
    if dem and not include_base:
        raise ValueError("--dem needs terrain, but include_base=False is the flat-ground "
                         "mode: buildings sit on a common z=0 datum and no terrain is built, "
                         "so a height raster would be meaningless. Drop --no-base, or build "
                         "the DEM from a separate run.")
    if dem:
        from sbg.onemap_native import dem as _demmod
        if dem_agg not in _demmod.AGGS:
            raise ValueError(f"dem_agg must be one of {_demmod.AGGS}, got {dem_agg!r}")
        if dem_overhang not in _demmod.OVERHANGS:
            raise ValueError(f"dem_overhang must be one of {_demmod.OVERHANGS}, "
                             f"got {dem_overhang!r}")
        if int(dem_crs) not in _demmod.CRS_CHOICES:
            raise ValueError(f"dem_crs must be one of {_demmod.CRS_CHOICES}, got {dem_crs!r}")
        if not (dem_px_m and float(dem_px_m) > 0):
            raise ValueError(f"dem_px_m must be > 0, got {dem_px_m!r}")

    # Recorded verbatim into every sidecar. These are exactly the knobs that change the
    # output geometry, so a mesh can be reproduced from its sidecar alone.
    _opts = dict(step=step, voxel_size=voxel_size, target_reduction=target_reduction,
                 decimate_error=decimate_error, include_base=include_base,
                 fuse_backend=fuse_backend, placement=placement,
                 coupling_lambda=coupling_lambda, crossing=crossing,
                 crossing_effective="drop" if require_full else "keep",
                 include_raw=bool(include_raw), dem=bool(dem))
    if dem:
        _opts.update(dem_px_m=float(dem_px_m), dem_crs=int(dem_crs), dem_agg=dem_agg,
                     dem_overhang=dem_overhang, dem_supersample=int(dem_supersample),
                     dem_source=dem_source)

    xmin, ymin, xmax, ymax = domain_polygon.bounds
    cxmin, cymin, cxmax, cymax = core_polygon.bounds
    lons, lats = _svy_to_wgs.transform([cxmin, cxmax], [cymin, cymax])
    log(f"[tiles] walking tileset for the core ROI...")
    leaves = domain_leaf_tiles(min(lons), min(lats), max(lons), max(lats))
    log(f"[tiles] {len(leaves)} leaf tiles cover the core ROI")

    log(f"[extract] crossing buildings: {'DROPPED' if require_full else 'KEPT WHOLE'} "
        f"(--crossing {crossing}"
        + (f" -> {'drop' if require_full else 'keep'}, buffer "
           f"{'on' if buffered else 'off'})" if crossing == "auto" else ")"))

    src = "precomputed store" if store_dir else f"{workers} live workers"
    log(f"[extract] whole-mesh extraction ({src}, correct transform, clip)...")
    _t_ext = [time.perf_counter()]
    _last_pct = [-1]

    def _prog(done, total):
        filled = int(28 * done / total)
        bar = "\u2588" * filled + "\u2591" * (28 - filled)
        rate = done / max(time.perf_counter() - _t_ext[0], 1e-6)
        eta = (total - done) / rate if rate else 0
        end = "\n" if done == total else "\r"
        print(f"    [{bar}] {done}/{total} tiles ({100*done//total}%)  ~{eta:4.0f}s left ",
              end=end, flush=True)
        # Also into the JOB log, or extraction is a long silent stretch in the UI.
        pct = 100 * done // total
        if _sink is not None and (pct // 10 > _last_pct[0] // 10 or done == total):
            _last_pct[0] = pct
            _sink(f"[extract] {done}/{total} tiles ({pct}%)"
                  + (f"  ~{eta:.0f}s left" if done < total else ""))

    pieces = extract_domain_buildings(leaves, core_polygon, workers=workers,
                                      progress=_prog, store_dir=store_dir,
                                      require_full=require_full)
    log(f"[extract] {len(pieces)} building pieces")

    # Exact tallest-building height, straight off the extracted geometry. This is what
    # "5H/15H/5H from the tallest building actually extracted" means, and it costs
    # nothing -- the pieces are already in hand. The UI's pre-submit estimate comes from
    # onemap_buildings.jsonl instead (the footprint index is 72.8% zero-height and
    # cannot be used for this).
    if pieces:
        h_exact = max(float(np.asarray(p["verts"])[:, 2].max()) - float(p["base_z"])
                      for p in pieces)
        log(f"[extract] tallest extracted building: {h_exact:.1f} m")
    else:
        h_exact = 0.0
        log(f"[extract] no buildings in the core ROI")

    log(f"[terrain] building DTM + conforming terrain (footprint-constrained)...")
    grid_z, affine = build_domain_dtm((xmin, ymin, xmax, ymax), step=step)
    dtm = DtmSampler(grid_z, affine)

    # A LOUD FAILURE THAT WENT QUIET, RESTORED.
    #
    # v1 built the DTM per-domain from contour_points.npz via griddata, so a domain with
    # zero 20 m-contour crossings had nothing to interpolate and raised. v2 crops the
    # pre-built whole-island data/dtm.tif instead (a 42x speedup), and that raster is
    # filled EVERYWHERE by nearest-fill, so the same domain now silently returns a
    # plausible-looking surface. The griddata path still exists but only as a fallback
    # for a missing cache, i.e. effectively never.
    #
    # What relief actually tells you, measured rather than guessed:
    #   relief ~ 0    -- the interpolator had NO local data and returned one constant
    #                    (Jurong, 1.5 km box: 0 contour points even within a 400 m halo,
    #                    DTM constant at 20.00 m, itself a contour VALUE).
    #   relief > 0    -- may still be entirely extrapolated: over open water, with 0
    #                    contour points, nearest-fill pulled from several DIFFERENT
    #                    distant contours and produced 8 m of FAKE relief.
    # So flatness is NOT a reliable tell for missing data, and --check-contours is the
    # only real signal. On genuinely flat reclaimed land the two coincide, because a
    # contour map draws no line where there is no elevation change.
    #
    # Source resolution: dtm.tif is 20 m native (Contour_250K, 1:250,000), so `step`
    # only interpolates -- below ~60 m of domain span there is no local detail at all.
    relief = float(np.nanmax(grid_z) - np.nanmin(grid_z)) if grid_z.size else 0.0
    span = max(xmax - xmin, ymax - ymin)
    if include_base and relief < 1.0:
        log(f"[terrain] !! WARNING: DTM relief is {relief:.2f} m across {span:.0f} m. "
            f"That is the interpolator returning a single constant, not measured "
            f"ground -- there is no contour data here. Correct on flat reclaimed land; "
            f"elsewhere it means the terrain is extrapolated. Use --check-contours.")
    if include_base and span < 60.0:
        log(f"[terrain] !! WARNING: domain spans {span:.0f} m against a 20 m native DTM, "
            f"so the terrain is a single interpolated patch with no local detail.")
    if check_contours:
        try:
            from sbg.topo.dtm import load_points
            cx, _cy, _cz = load_points(bbox=(xmin, ymin, xmax, ymax))
            log(f"[terrain] {len(cx):,} source contour points inside the domain"
                + ("  -- NONE: this terrain is entirely extrapolated from outside the "
                   "domain and is not measured ground." if len(cx) == 0 else ""))
        except Exception as e:
            log(f"[terrain] contour check unavailable ({e})")

    # include_base=False is the FLAT-GROUND assumption: the solver supplies a flat floor
    # (snappyHexMesh's background blockMesh, Fluent's enclosure), and the STL carries
    # obstacles only. So every building must sit on a COMMON datum -- leaving them at
    # their own terrain-following pad_z would ship N solids at N different elevations
    # with no ground between them, which no flat solver floor can fit.
    #
    # Pieces arrive from OneMap already on a z=0 datum (confirmed base_z == 0.000 across
    # 1,061 pieces in 5 areas), so a zero DTM places them exactly there with no shift.
    # Skirts go too: a skirt exists ONLY to overlap the terrain slab, and with no terrain
    # it is just a 30 m spike hanging under each building.
    _flat = not include_base
    # The datum is offset half a voxel, NOT left at 0. Landing every base exactly on a
    # voxel grid plane is degenerate -- measured: 34 fragments instead of 12, which the
    # debris filter then silently DELETES. Same class as SBG_CLIP_NUDGE_M. The offset is
    # subtracted back out after the fuse, so the shipped file still has its base at 0.
    _flat_datum = 0.5 * voxel_size if (_flat and voxel_size and voxel_size > 0) else 0.0
    bld_v, bld_f, terr_v, terr_f = place_on_terrain_conforming(
        pieces,
        DtmSampler(np.full_like(grid_z, _flat_datum), affine) if _flat else dtm,
        domain_polygon, skirt=voxel_size > 0 and not _flat,
        mode=placement, coupling_lambda=coupling_lambda)
    _lam = ("" if placement != "laplacian"
            else f" lambda={coupling_lambda if coupling_lambda is not None else COUPLING_LAMBDA:g}")
    if _flat:
        log(f"[terrain] FLAT-GROUND mode: {len(pieces)} buildings on a common z=0 datum, "
            f"no terrain, no skirts (the solver supplies the floor)")
        if relief > 5.0:
            log(f"[terrain] note: this domain has {relief:.0f} m of real relief, which a "
                f"flat datum discards. Fine if that is what you want.")
        terr_v, terr_f = terr_v[:0], terr_f[:0]
    else:
        log(f"[terrain] DTM {grid_z.shape} elev {np.nanmin(grid_z):.0f}-{np.nanmax(grid_z):.0f}m, "
            f"{len(pieces)} pieces placed, {len(terr_f):,} terrain triangles "
            f"(placement={placement}{_lam})")

        # Extrude the terrain surface down to a FLAT plane (not a fixed-thickness slab --
        # a hilltop gets a tall column, low ground a short one, "to scale"). Replaces
        # Blender's SOLIDIFY for terrain entirely. base_z is NOT hardcoded 0: a plunge
        # skirt can reach PLUNGE_M below its own pad_z, and on low ground that goes
        # negative (measured -10 m on a real domain), which left skirts poking through
        # the solid's own sealed bottom cap -- a real, visible hole.
        base_z = min(0.0, float(bld_v[:, 2].min()) - 1.0) if len(bld_v) else 0.0
        terr_v, terr_f = terrain_flat_base_solid(terr_v, terr_f, domain_polygon,
                                                 base_z=base_z)
        log(f"[terrain] flat-base solid: z {base_z:.0f}-{terr_v[:, 2].max():.0f}m, "
            f"{len(terr_f):,} faces")

    # JAEA mode. This is the ONLY point where terrain and buildings are both live AND in the
    # true world frame: the raw branch below returns early in raw-only mode, and after the
    # fuse both arrays are del'd (only the rotated _V/_F survive into the wind loop). Under
    # wind this therefore writes exactly one world-frame raster, which is correct rather
    # than merely convenient -- the geometry is identical for every bearing.
    #
    # The terrain SOLID is passed as-is rather than its top surface: the bottom cap sits at
    # base_z (<= 0, always under the ground) and the side walls are vertical, so a top-down
    # max ignores both. That avoids coupling this to terrain_flat_base_solid's face ordering.
    _dem_path = None
    if dem:
        _dem_t0 = time.perf_counter()
        from sbg.onemap_native.dem import build_dem, write_dem
        _g, _a, _meta = build_dem(terr_v, terr_f, bld_v, bld_f, domain_polygon, dtm,
                                  px_m=float(dem_px_m), crs=int(dem_crs), agg=dem_agg,
                                  overhang=dem_overhang, supersample=int(dem_supersample),
                                  source=dem_source, log=log)
        _meta["domain_ring_m"] = np.asarray(domain_polygon.exterior.coords,
                                            dtype=float)[:, :2].tolist()
        _dem_path, _ = write_dem(_g, _a, _meta,
                                 Path(out_stl).with_suffix(".dem.tif"), log=log)
        del _g
        if dem_only:
            # Everything past this point exists to make a MESH. The DEM is already on
            # disk, so stop -- skipping the fuse, decimate, clip and polish entirely.
            log(f"[done] {_dem_path.name} in {time.perf_counter() - _dem_t0:.1f}s "
                f"(GeoTIFF only -- no mesh built)")
            return _dem_path

    # High-fidelity raw mode (voxel_size <= 0): skip the whole watertight machinery and
    # dump the real OneMap meshes straight to STL. Preserves full LiDAR detail and is
    # fast, but is a non-watertight triangle soup -- ACCEPTED by Ansys fault-tolerant
    # meshing (already the production path there) and by snappyHexMesh, which meshed it
    # at 1,030,259 cells with no errors; rejected only by the Ansys *watertight* workflow.
    _raw_only = voxel_size is not None and voxel_size <= 0
    if _raw_only or include_raw:
        if include_base:
            _rv = np.vstack([terr_v, bld_v])
            _rf = np.vstack([terr_f, bld_f + len(terr_v)])
        else:
            _rv, _rf = bld_v, bld_f

        # Where the raw file lands. Raw-only mode owns the requested name; the combined
        # mode is a SECOND artifact alongside the watertight one, so it takes the `.raw`
        # variant and the watertight keeps the plain name (that is the file most tools
        # want, and the one existing download links already point at).
        _raw_names = ({} if not wind else
                      {wf: Path(out_stl).parent /
                       (f"wind_{int(round(wf)) % 360:03d}" + ("" if _raw_only else ".raw") + ".stl")
                       for wf in wind["dirs"]})

        if wind:
            # Each direction is trimmed to ITS OWN wind rectangle, not left at the build
            # envelope. Without this, N directions produce N rotated copies of the whole
            # envelope hull -- visibly wrong, and it silently makes "inlet = ymin" false.
            from sbg.onemap_native.wind import flow_bearing_deg, rotate_xy, wind_rect
            centre = np.asarray(wind["centre"], dtype=float)[:2]
            out_dir = Path(out_stl).parent
            out_dir.mkdir(parents=True, exist_ok=True)
            outs = []
            for i, wf in enumerate(wind["dirs"], 1):
                psi = flow_bearing_deg(wf)
                log(f"[direction] {i}/{len(wind['dirs'])} wind from {wf:g} deg (raw)")
                rect_rot, _ = wind_rect(core_polygon, centre, psi,
                                        wind["up_m"], wind["down_m"], wind["lat_m"])
                _export_raw(rotate_xy(_rv, psi, centre), _rf, _raw_names[wf], log,
                            rect_rot=rect_rot, label=f" for wind_from={wf:g}")
                if _raw_only:
                    outs.append(_write_wind_sidecar(
                        _raw_names[wf], wf, psi, centre, core_polygon, rect_rot, wind,
                        h_exact, log, clipped=True, store_dir=store_dir, opts=_opts,
                        dem_name=_dem_path.name if _dem_path else None))
            if _raw_only:
                log(f"[done] {len(outs)} raw direction(s) in {out_dir}  "
                    f"(trimmed to each wind rectangle; open soup, not watertight)")
                return outs
        else:
            _raw_out = out_stl if _raw_only else _raw_path(out_stl)
            _export_raw(_rv, _rf, _raw_out, log,
                        label=f" ({'with' if include_base else 'without'} ground plane)")
            if _raw_only:
                log(f"[done] {out_stl}  (raw high-fidelity soup -- not watertight by design)")
                _write_build_sidecar(out_stl, domain_polygon, core_polygon, h_exact, log,
                                     store_dir=store_dir, opts=_opts,
                                     dem_name=_dem_path.name if _dem_path else None)
                return out_stl
        del _rv, _rf

    import meshlib.mrmeshpy as mr

    if fuse_backend == "meshlib":
        # No subprocess, no PLY round-trip, no Blender install. Same OpenVDB
        # algorithm Blender's REMESH modifier uses -- see _fuse_meshlib.
        mesh = _fuse_meshlib(terr_v, terr_f, bld_v, bld_f, voxel_size, log)
        del terr_v, terr_f, bld_v, bld_f, pieces
        import gc
        gc.collect()
    else:
        terr_ply = workdir / "terrain.ply"
        bld_ply = workdir / "buildings.ply"
        _write_scene_ply(terr_ply, bld_ply, terr_v, terr_f, bld_v, bld_f)
        log(f"[scene] wrote terrain+buildings PLY "
            f"({(terr_ply.stat().st_size + bld_ply.stat().st_size) / 1e6:.0f} MB)")
        # free the big geometry arrays before spawning Blender -- keeps this
        # process's RSS low while Blender does its memory-heavy remesh.
        del terr_v, terr_f, bld_v, bld_f, pieces
        import gc
        gc.collect()

        # Fuse to PLY (welded/indexed) not STL (per-triangle soup). The voxel-remesh
        # output is perfectly watertight; STL un-welds it and re-welding in trimesh
        # manufactures hundreds of SPURIOUS non-manifold edges. PLY carries Blender's
        # clean welding straight through.
        raw_ply = workdir / "fused.ply"
        log(f"[blender] fusing (join + voxel remesh {voxel_size}m -> watertight PLY)...")
        subprocess.run(
            [str(BLENDER_PATH), "--background", "--python", str(_FUSE_SCRIPT), "--",
             "--terrain", str(terr_ply), "--buildings", str(bld_ply), "--output", str(raw_ply),
             "--voxel-size", str(voxel_size), "--skip-terrain-solidify",
             "--skip-debris"],  # clip's keep-largest handles debris; skip the ~30s Blender split
            check=True,
        )
        log(f"[blender] fuse done")
        mesh = mr.loadMesh(str(raw_ply))

    # meshlib manifold-preserving decimation -- replaces fast_simplification +
    # pymeshfix ENTIRELY. meshlib's half-edge topology literally cannot represent
    # a non-manifold edge, so decimation stays watertight BY CONSTRUCTION (FQMS/
    # meshopt broke it -> 351 non-manifold + 613 holes -> forced a slow ~45s
    # pymeshfix global rebuild). Same face count, watertight, no repair needed.
    log(f"[decimate] meshlib manifold-preserving decimation (stays watertight, no pymeshfix)...")
    nf0 = mesh.topology.numValidFaces()
    ds = mr.DecimateSettings()
    ds.maxDeletedFaces = int(nf0 * target_reduction)
    ds.maxError = decimate_error
    mr.decimateMesh(mesh, ds)
    log(f"[decimate] {nf0} -> {mesh.topology.numValidFaces()} faces, "
        f"holes={len(mesh.topology.findHoleRepresentiveEdges())} (watertight by construction)")

    # Clip to the exact domain box via meshlib's robust boolean intersect. Stays
    # watertight (0 holes) with clean vertical CFD walls -- no fill_holes /
    # keep-largest / debris juggling, and ~4x faster than a trimesh slice_plane
    # clip (3.5s vs 14s). The box spans the full mesh z-range so only the XY walls
    # cut; drops the terrain margin and any floating remesh debris in one step.
    import meshlib.mrmeshnumpy as _mn_out
    _V = _mn_out.getNumpyVerts(mesh)
    if _flat_datum:
        _V = _V.copy()
        _V[:, 2] -= _flat_datum      # shift the base back to 0; see _flat_datum above
    _F = _mn_out.getNumpyFaces(mesh.topology)
    del mesh

    if not wind:
        p = _finish_one(_V, _F, domain_polygon, out_stl, log,
                        expect_disconnected=_flat)
        _write_build_sidecar(p, domain_polygon, core_polygon, h_exact, log,
                             store_dir=store_dir, opts=_opts,
                             dem_name=_dem_path.name if _dem_path else None)
        return p

    # ---- BUILD ONCE, ROTATE + CLIP PER DIRECTION -----------------------------------
    # Everything above ran exactly once and is direction-agnostic. Only the clip depends
    # on the wind, so each extra direction costs a rotate + clip + polish (~2 s) instead
    # of a whole build. Measured 7.0x less total work than N separate domains at N=16.
    from sbg.onemap_native.wind import flow_bearing_deg, rotate_xy, wind_rect

    dirs = list(wind["dirs"])
    centre = np.asarray(wind["centre"], dtype=float)[:2]
    up_m, down_m, lat_m = wind["up_m"], wind["down_m"], wind["lat_m"]
    core = core_polygon
    out_dir = Path(out_stl).parent
    out_dir.mkdir(parents=True, exist_ok=True)

    outputs = []
    for i, wf in enumerate(dirs, 1):
        psi = flow_bearing_deg(wf)
        log(f"[direction] {i}/{len(dirs)} wind from {wf:g} deg (psi {psi:g})")

        # Rotate BEFORE clipping, not after. The polish's whole job is float32-exactness
        # at the shipped precision; rotating a polished mesh re-perturbs every vertex and
        # invalidates the check that was just performed. Rotating first also makes the
        # clip solid an exactly axis-aligned box rather than an extruded rotated quad.
        Vr = rotate_xy(_V, psi, centre)
        rect_rot, _ = wind_rect(core, centre, psi, up_m, down_m, lat_m)

        p = out_dir / f"wind_{int(round(wf)) % 360:03d}.stl"
        _finish_one(Vr, _F, rect_rot, p, log, expect_disconnected=_flat)
        outputs.append(_write_wind_sidecar(p, wf, psi, centre, core, rect_rot,
                                           wind, h_exact, log, store_dir=store_dir,
                                           opts=_opts,
                                           dem_name=_dem_path.name if _dem_path else None))

    log(f"[done] {len(outputs)} direction(s) written to {out_dir}")
    return outputs


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bbox", help="xmin,ymin,xmax,ymax in EPSG:3414 meters")
    ap.add_argument("--domain-geojson", help="Polygon GeoJSON (WGS84 unless --domain-crs)")
    ap.add_argument("--domain-crs", type=int, default=4326)
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--step", type=float, default=5.0, help="DTM grid step (m)")
    ap.add_argument("--voxel-size", type=float, default=2.0)
    ap.add_argument("--target-reduction", type=float, default=0.97,
                    help="face-removal cap for meshlib decimation (0.97 = 'as much as the "
                         "error budget allows'). Deliberately high so --decimate-error is the "
                         "real governor -- a frontal-area sweep showed the old 0.85 cap bound "
                         "before the error cap, leaving ~2x reduction unused for free.")
    ap.add_argument("--decimate-error", type=float, default=2.5,
                    help="max geometric error (m) meshlib decimation may introduce -- the real "
                         "quality knob. Tie to the CFD cell size (~3m target -> 2.5m keeps every "
                         "vertex within a cell): ~480k faces, watertight, ~0.3%% frontal-area loss. "
                         "Raising it shrinks the mesh further at ~flat frontal error (measured down "
                         "to 1.7%% of GT faces) but starts moving corners more than one cell.")
    ap.add_argument("--workers", type=int, default=6, help="parallel tile-decode workers")
    ap.add_argument("--store", default=None,
                    help="precomputed placed-building store (see sbg.onemap_native.precompute); "
                         "when set, cutout loads from it instead of fetching/decoding tiles")
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--wind-from", default=None,
                    help="METEOROLOGICAL bearings the wind blows FROM, clockwise from "
                         "north: a comma list ('0,45,90'), or 'rose4'/'rose8'/'rose16'. "
                         "Turns on the wind-aligned CFD domain: geometry is emitted "
                         "already rotated so the flow is always +Y, which means the Fluent "
                         "setup never changes (inlet always ymin, outlet always ymax) and "
                         "45 degrees is as easy as 0. Writes wind_<bearing>.stl plus a "
                         "wind_<bearing>.wind.json sidecar per direction into -o's parent "
                         "directory. Everything heavy runs ONCE; each extra direction is "
                         "just a rotate + clip.")
    ap.add_argument("--buffer-h", type=float, default=None,
                    help="--wind-from only. Building height H (m) to size the CFD buffer "
                         "from, per COST 732: 5H upwind / 15H downwind / 5H lateral. "
                         "Default: the tallest building actually extracted from the ROI. "
                         "Auto sizing is capped at 60 m -- use --buffer-m above that.")
    ap.add_argument("--buffer-m", default=None,
                    help="--wind-from only. Explicit buffer as 'up,down,lateral' in metres, "
                         "overriding --buffer-h and every cap.")
    ap.add_argument("--z0", type=float, default=0.7,
                    help="--wind-from only. Advisory ground roughness length (m) recorded "
                         "in the sidecar. The buffer is bare terrain with no buildings, so "
                         "upstream roughness must be supplied as a Fluent wall BC -- the "
                         "mesh does NOT encode it.")
    ap.add_argument("--fuse-backend", choices=("meshlib", "blender"), default="meshlib",
                    help="voxel-remesh backend. Both call OpenVDB and are equivalent "
                         "end-to-end: measured on 3 real domains, both give holes=0 / 0 open "
                         "/ 0 non-manifold / 0 zero-area / strictly watertight / 1 body, with "
                         "volume agreeing to 0.001-0.004%%. The fuse step itself is identical "
                         "too (1,300,768 faces either way, volume differing by 1 m3 in 37.4M). "
                         "meshlib is 4.25x faster on the fuse (0.8s vs 3.4s) and needs no "
                         "Blender install, so it is the default; 'blender' is an escape hatch "
                         "requiring SBG_BLENDER_PATH.")
    ap.add_argument("--placement", choices=("group", "drape", "laplacian"),
                    default="drape",
                    help="how connected buildings spanning relief are levelled: "
                         "group=one flat level per 3D-connected structure "
                         "(bridges stay coplanar, hillside strings get carved in); "
                         "drape=no grouping at all (hillside strings terrace, "
                         "bridges shear); laplacian=soft coupling, both resolve "
                         "without classification (see --coupling-lambda)")
    ap.add_argument("--crossing", choices=("auto", "drop", "keep"), default="auto",
                    help="buildings straddling the area-of-interest edge. auto (default) "
                         "drops them when there is no buffer (they would be sliced flat by "
                         "the clip) and keeps them whole when a wind buffer exists (the "
                         "clip is hundreds of metres away, so nothing slices them). "
                         "drop/keep force it.")
    ap.add_argument("--check-contours", action="store_true",
                    help="count the source contour points inside the domain and report "
                         "them. The DTM cache is filled everywhere by nearest-fill, so a "
                         "domain with no real contour data still returns a plausible "
                         "surface; this is the only way to see that. Reads a 285 MB npz, "
                         "hence opt-in.")
    ap.add_argument("--coupling-lambda", type=float, default=None,
                    help=f"--placement laplacian only (default {COUPLING_LAMBDA:g}). "
                         "Higher = flatter/more grouped, lower = more terracing; "
                         "inf == --placement group, 0 == --placement drape")
    ap.add_argument("--also-raw", action="store_true",
                    help="additionally write the un-fused full-detail soup as "
                         "<stem>.raw.stl beside each watertight output. The dose pipeline "
                         "needs both (raw -> Ansys fault-tolerant meshing for the CFD, "
                         "watertight -> DAGMC/OpenMC), and producing them in one run is "
                         "what guarantees they describe the same geometry. Ignored with "
                         "--voxel-size 0, where the raw file is already the output.")
    ap.add_argument("--no-base", action="store_true",
                    help="exclude the ground/terrain plane from the exported STL (buildings only). "
                         "Terrain still backs the voxel remesh internally -- an A/B test showed "
                         "excluding it from the JOIN makes ordinary ground-level buildings visibly "
                         "worse (shattered vs. solid); this only strips it from the final file, as a "
                         "late boolean subtraction, matching a normal CFD obstacle-only export.")
    ap.add_argument("--dem", action="store_true",
                    help="JAEA mode: additionally write <stem>.dem.tif, a north-up GeoTIFF "
                         "where every pixel is a height (terrain with buildings baked in), "
                         "plus a .dem.json sidecar carrying the tie point. Sampled from the "
                         "un-fused geometry, so it has no voxel staircase or decimation "
                         "error. Under a wind rose it is written ONCE in the true world "
                         "frame -- the geometry is identical for every bearing.")
    ap.add_argument("--dem-only", action="store_true",
                    help="write ONLY the GeoTIFF -- no STL. Stops after the DEM, skipping "
                         "the fuse, decimate, clip and polish. Implies --dem.")
    ap.add_argument("--dem-px", type=float, default=4.0, dest="dem_px",
                    help="DEM pixel size in metres (default 4). In EPSG:4326 the degree "
                         "pixel is derived so both axes are still exactly this on the ground.")
    ap.add_argument("--dem-crs", type=int, default=3414, choices=(3414, 4326),
                    help="DEM output CRS (default 3414, the pipeline's native metric grid)")
    ap.add_argument("--dem-agg", default="median",
                    choices=("min", "p10", "median", "p50", "p90", "p95", "max", "mean"),
                    help="how the sub-samples in one pixel collapse to one height. For a "
                         "cell straddling a building edge the distribution is bimodal, so "
                         "percentile p IS a coverage threshold at (100-p)%%: median = 'more "
                         "than half this cell is inside', which measured +4.9%% footprint "
                         "area against truth. max measured +34.8%% (the half-pixel dilation) "
                         "and mean invents heights halfway up walls. Default median.")
    ap.add_argument("--dem-overhang", default="keep", choices=("keep", "drop"),
                    help="a heightfield cannot express solid/void/solid, so under a bridge "
                         "or canopy the max fills to the ground. 'keep' is the classic DSM "
                         "(conservative for shielding); 'drop' reverts columns whose lowest "
                         "building geometry floats clear of the terrain back to ground "
                         "(correct for flow). The affected fraction is logged either way.")
    ap.add_argument("--dem-supersample", type=int, default=4, dest="dem_supersample",
                    help="samples per pixel axis (default 4, i.e. 16 per pixel). 1 = plain "
                         "pixel-centre sampling, which drops sub-pixel structure entirely.")
    ap.add_argument("--dem-source", default="surface", choices=("surface", "terrain"),
                    help="'surface' = terrain with buildings (a DSM, the default); "
                         "'terrain' = bare ground as built, including the graded pads.")
    args = ap.parse_args()

    domain = load_domain_polygon(bbox=args.bbox, domain_geojson=args.domain_geojson,
                                 domain_crs=args.domain_crs)

    core, wind = domain, None
    if args.wind_from:
        from sbg.onemap_native.wind import (
            build_envelope, buffer_distances, check_envelope, normalise_dirs,
            roi_centre, rose)
        spec = args.wind_from.strip().lower()
        dirs = (rose(int(spec[4:])) if spec.startswith("rose")
                else normalise_dirs(spec.split(",")))

        override = None
        if args.buffer_m:
            u, d, l = (float(v) for v in args.buffer_m.split(","))
            override = {"upwind": u, "downwind": d, "lateral": l}
            h = float("nan")
        elif args.buffer_h is not None:
            h = args.buffer_h
        else:
            # Estimate H from SLA's own batch-table heights. The footprint index cannot
            # be used for this -- 72.8% of its heights are zero (see heights.py). The
            # EXACT H is logged during extraction for cross-checking.
            from sbg.onemap_native.heights import roi_height_stats
            st = roi_height_stats(core)
            h = st["max"]
            print(f"[wind] {st['count']} buildings in the ROI: max {st['max']:.1f} m, "
                  f"p90 {st['p90']:.1f} m, median {st['median']:.1f} m")
            print(f"[wind] sizing from max; --buffer-h {st['p90']:.0f} would use p90 "
                  f"and give a much smaller domain")

        up, down, lat, warns = buffer_distances(h, override_m=override)
        for w in warns:
            print(f"[wind] warning: {w}")

        c = roi_centre(core)
        domain = build_envelope(core, c, dirs, up, down, lat)
        check_envelope(domain, core, c, dirs, up, down, lat)
        print(f"[wind] {len(dirs)} direction(s); buffer {up:.0f}/{down:.0f}/{lat:.0f} m; "
              f"ROI {core.area/1e6:.2f} km^2 -> build envelope {domain.area/1e6:.2f} km^2")
        wind = {"dirs": dirs, "centre": c, "up_m": up, "down_m": down,
                "lat_m": lat, "z0_m": args.z0}

    build_domain_stl(domain, args.output, step=args.step, voxel_size=args.voxel_size,
                     target_reduction=args.target_reduction, decimate_error=args.decimate_error,
                     workers=args.workers, store_dir=args.store, workdir=args.workdir,
                     fuse_backend=args.fuse_backend, core_polygon=core, wind=wind,
                     include_base=not args.no_base, placement=args.placement,
                     coupling_lambda=args.coupling_lambda,
                     check_contours=args.check_contours,
                     crossing=args.crossing, include_raw=args.also_raw,
                     dem=args.dem, dem_px_m=args.dem_px, dem_crs=args.dem_crs,
                     dem_agg=args.dem_agg, dem_overhang=args.dem_overhang,
                     dem_supersample=args.dem_supersample, dem_source=args.dem_source,
                     dem_only=args.dem_only)


if __name__ == "__main__":
    main()
