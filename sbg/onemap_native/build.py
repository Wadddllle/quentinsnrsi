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


def build_domain_stl(domain_polygon, out_stl, step=5.0, voxel_size=2.0,
                     target_reduction=0.97, decimate_error=2.5, workers=6,
                     store_dir=None, workdir=None, log=None, include_base=True,
                     placement="drape", coupling_lambda=None):
    """Run the full tile-native pipeline for one domain polygon (EPSG:3414)."""
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

    xmin, ymin, xmax, ymax = domain_polygon.bounds
    lons, lats = _svy_to_wgs.transform([xmin, xmax], [ymin, ymax])
    log(f"[tiles] walking tileset for domain...")
    leaves = domain_leaf_tiles(min(lons), min(lats), max(lons), max(lats))
    log(f"[tiles] {len(leaves)} leaf tiles cover the domain")

    src = "precomputed store" if store_dir else f"{workers} live workers"
    log(f"[extract] whole-mesh extraction ({src}, correct transform, clip)...")
    _t_ext = [__import__("time").perf_counter()]

    def _prog(done, total):
        import time as _tm
        filled = int(28 * done / total)
        bar = "█" * filled + "░" * (28 - filled)
        rate = done / max(_tm.perf_counter() - _t_ext[0], 1e-6)
        eta = (total - done) / rate if rate else 0
        end = "\n" if done == total else "\r"
        print(f"    [{bar}] {done}/{total} tiles ({100*done//total}%)  ~{eta:4.0f}s left ",
              end=end, flush=True)

    pieces = extract_domain_buildings(leaves, domain_polygon, workers=workers,
                                      progress=_prog, store_dir=store_dir)
    log(f"[extract] {len(pieces)} building pieces")

    log(f"[terrain] building DTM + conforming terrain (footprint-constrained)...")
    grid_z, affine = build_domain_dtm((xmin, ymin, xmax, ymax), step=step)
    dtm = DtmSampler(grid_z, affine)
    # Skirts exist ONLY to guarantee volume overlap into the slab so the voxel
    # remesh fuses building and terrain. The raw (voxel_size <= 0) export does no
    # fusion, and there the skirt actively HURTS: it is built from the same
    # footprint ring as the building it wraps, so the two share vertices and weld
    # into each other, leaving both open. Measured on the 2km CBD domain (component
    # closure, no cross-object weld): with skirts 2,921/2,973 closed (52 open);
    # without, 1,301/1,303 closed (2 open -- the pieces seal_piece cannot fully
    # close). The building already sits exactly on its pad regardless (base at
    # pad_z, pad flattened to pad_z), so nothing floats without the skirt.
    # `placement` picks how a connected structure spanning real relief is levelled
    # -- "group" (default, bridges stay coplanar), "drape" (no grouping, hillside
    # strings terrace but bridges shear), or "laplacian" (soft coupling, one lambda
    # knob, both cases resolve without being classified). See
    # place_on_terrain_conforming's docstring and TERRAIN.md section 3.
    bld_v, bld_f, terr_v, terr_f = place_on_terrain_conforming(
        pieces, dtm, domain_polygon, skirt=voxel_size > 0,
        mode=placement, coupling_lambda=coupling_lambda)
    _lam = ("" if placement != "laplacian"
            else f" lambda={coupling_lambda if coupling_lambda is not None else COUPLING_LAMBDA:g}")
    log(f"[terrain] DTM {grid_z.shape} elev {np.nanmin(grid_z):.0f}-{np.nanmax(grid_z):.0f}m, "
        f"{len(pieces)} pieces placed, {len(terr_f):,} terrain triangles "
        f"(placement={placement}{_lam})")

    # Extrude the terrain surface down to a FLAT plane (not a fixed-thickness
    # slab -- a hilltop gets a tall column, low ground gets a short one, "to
    # scale"). Replaces Blender's SOLIDIFY step for terrain entirely (a
    # uniform-thickness modifier can't express "extrude to an absolute Z
    # plane"). base_z is NOT hardcoded 0 -- a building's plunge skirt can
    # reach PLUNGE_M below its own pad_z, and on low-lying ground that can go
    # negative (measured: -10m on a real domain); a hard z=0 floor left those
    # skirts poking through the terrain solid's own sealed bottom cap (a real,
    # visible hole -- confirmed both numerically, via bld_v's own min z, and
    # visually in a raw render). base_z instead tracks the lowest point any
    # building actually reaches, with a margin, so the flat floor always sits
    # AT OR BELOW every skirt -- still exactly 0 on ordinary terrain (nothing
    # dips below), only drops lower where something genuinely needs it to.
    base_z = min(0.0, float(bld_v[:, 2].min()) - 1.0) if len(bld_v) else 0.0
    terr_v, terr_f = terrain_flat_base_solid(terr_v, terr_f, domain_polygon, base_z=base_z)
    log(f"[terrain] flat-base solid: z {base_z:.0f}-{terr_v[:, 2].max():.0f}m, {len(terr_f):,} faces")

    # High-fidelity raw mode (voxel_size <= 0): skip the whole watertight machinery
    # (Blender voxel remesh + meshlib decimate/clip/polish) and dump the terrain +
    # real OneMap building meshes straight to STL. Preserves full LiDAR detail, is
    # fast (no Blender), but is a non-watertight triangle soup -- for eyeballing the
    # real geometry, not for CFD meshing.
    if voxel_size is not None and voxel_size <= 0:
        import trimesh
        # include_base is honoured here too -- raw mode used to always write the
        # terrain, which is why the UI had to grey the checkbox out. There is no
        # fuse step to back the buildings, so dropping terrain is just not writing
        # it (unlike the watertight path, where terrain stays in the Blender join
        # and is boolean-subtracted at the end).
        if include_base:
            verts = np.vstack([terr_v, bld_v])
            faces = np.vstack([terr_f, bld_f + len(terr_v)])
        else:
            verts, faces = bld_v, bld_f
        trimesh.Trimesh(verts, faces, process=False).export(str(out_stl))
        log(f"[raw] high-fidelity NON-watertight mesh: {len(faces):,} faces "
            f"({'with' if include_base else 'without'} ground plane; "
            f"skipped fuse/decimate/clip/polish)")
        log(f"[done] {out_stl}  (raw high-fidelity soup -- not watertight by design)")
        return out_stl

    terr_ply = workdir / "terrain.ply"
    bld_ply = workdir / "buildings.ply"
    _write_scene_ply(terr_ply, bld_ply, terr_v, terr_f, bld_v, bld_f)
    log(f"[scene] wrote terrain+buildings PLY "
        f"({(terr_ply.stat().st_size + bld_ply.stat().st_size) / 1e6:.0f} MB)")
    # free the big geometry arrays before spawning Blender -- keeps this process's
    # RSS low while Blender does its (memory-heavy) voxel remesh in a subprocess.
    # Terrain is the one exception when include_base=False: it's needed again
    # at the very end (boolean-subtracted from the final export) and MUST come
    # from these original in-memory arrays, not a PLY round-trip -- reloading
    # the exported terrain.ply back from disk was tried first and came back
    # with real defects (holes=4, multiEdges=True) that were never present in
    # the in-memory mesh, apparently introduced by the binary PLY export/
    # reload's float32 precision loss on coincident boundary/wall vertices.
    _terr_v_keep, _terr_f_keep = (terr_v, terr_f) if not include_base else (None, None)
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
         "--skip-debris"],  # trimesh clip's keep-largest handles debris; skip the ~30s Blender split
        check=True,
    )
    log(f"[blender] fuse done")

    # meshlib manifold-preserving decimation -- replaces fast_simplification +
    # pymeshfix ENTIRELY. meshlib's half-edge topology literally cannot represent
    # a non-manifold edge, so decimation stays watertight BY CONSTRUCTION (FQMS/
    # meshopt broke it -> 351 non-manifold + 613 holes -> forced a slow ~45s
    # pymeshfix global rebuild). Same face count, watertight, no repair needed.
    log(f"[decimate] meshlib manifold-preserving decimation (stays watertight, no pymeshfix)...")
    import meshlib.mrmeshpy as mr
    mesh = mr.loadMesh(str(raw_ply))
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
    log(f"[clip] meshlib boolean intersect with exact domain polygon...")
    bb = mesh.computeBoundingBox()
    clip_solid = _polygon_clip_solid(domain_polygon, bb.min.z - 10.0, bb.max.z + 10.0)
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
    comps = mr.MeshComponents.getAllComponents(mr.MeshPart(clipped))
    if len(comps) > 1:
        largest = max(comps, key=lambda cc: cc.count())
        drop = mr.FaceBitSet()
        ndrop = 0
        for comp in comps:
            if comp is largest or comp.count() > DEBRIS_MAX_FACES:
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
        _tri = _v[_f]
        _area = 0.5 * np.linalg.norm(
            np.cross(_tri[:, 1] - _tri[:, 0], _tri[:, 2] - _tri[:, 0]), axis=1)
        _bad = _area < 1e-9
        if not _bad.any():
            break
        _dropped += int(_bad.sum())
        polished = _mn.meshFromFacesVerts(
            np.asarray(_f[~_bad], dtype=np.int32), np.asarray(_v, dtype=np.float64))
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

    # include_base=False: strip the ground/terrain plane from the EXPORTED file
    # only -- terrain stayed in the Blender join/voxel-remesh above the whole
    # time, since an A/B test (fuse the same domain with vs. without terrain in
    # the join, MD1's known-disintegrating span) showed terrain backing makes a
    # large, real difference for ordinary ground-level buildings (solid grounded
    # mass vs. a shattered wireframe lattice) even though it does nothing for a
    # genuinely elevated thin span either way. So this is a late, separate
    # boolean subtraction of the terrain solid already computed earlier in this
    # function (kept in memory as `_terr_v_keep`/`_terr_f_keep` specifically for
    # this -- reloading the exported terrain.ply back from disk was tried first
    # and came back with real defects the in-memory mesh never had, see above)
    # -- matches a normal CFD obstacle-only export (buildings as separate
    # solids, no ground plate), not a different/riskier path through the remesh.
    if not include_base:
        log("[strip-base] subtracting terrain solid from the final export...")
        import meshlib.mrmeshnumpy as mn
        # The terrain solid's own outer wall is EXACTLY the domain-boundary
        # clip wall the building mesh was already cut to (_polygon_clip_solid
        # uses the same domain_polygon) -- an exact coincident/coplanar shared
        # surface is a degenerate case for exact-CSG intersection ("contours
        # ... are not closed or consistent"). A VDB offsetMesh was tried first
        # to inflate the subtractor -- WAY too slow/hung at domain scale (voxel
        # size has to be finer than the offset itself, e.g. 0.05m over a
        # ~250m-wide mesh is millions of voxels). Fixed cheaply instead: scale
        # the whole terrain solid by a tiny factor (1.0005, ~0.05% -- well under
        # a centimeter at these building heights) about its own centroid, just
        # enough that its wall/cap no longer exactly coincides with mesh A's
        # own boundary anywhere, with no meaningful shift to the real cut shape.
        centroid = _terr_v_keep.mean(axis=0)
        scaled_v = centroid + (_terr_v_keep - centroid) * 1.0005
        terrain_solid = mn.meshFromFacesVerts(_terr_f_keep, scaled_v)
        res = mr.boolean(polished if n0 else clipped, terrain_solid, mr.BooleanOperation.DifferenceAB)
        if not res.valid():
            raise RuntimeError(f"meshlib boolean subtract (strip-base) failed: {res.errorString}")
        stripped = res.mesh
        mr.saveMesh(stripped, str(out_stl))
        holes = len(stripped.topology.findHoleRepresentiveEdges())
        log(f"[strip-base] watertight={holes == 0} (meshlib holes={holes}), "
            f"{stripped.topology.numValidFaces()} faces")

    log(f"[done] {out_stl}  (strictly watertight -- verify with meshlib holes/"
        f"hasMultipleEdges, or a trimesh process=False + merge_vertices load)")
    return out_stl


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
    ap.add_argument("--placement", choices=("group", "drape", "laplacian"),
                    default="drape",
                    help="how connected buildings spanning relief are levelled: "
                         "group=one flat level per 3D-connected structure "
                         "(bridges stay coplanar, hillside strings get carved in); "
                         "drape=no grouping at all (hillside strings terrace, "
                         "bridges shear); laplacian=soft coupling, both resolve "
                         "without classification (see --coupling-lambda)")
    ap.add_argument("--coupling-lambda", type=float, default=None,
                    help=f"--placement laplacian only (default {COUPLING_LAMBDA:g}). "
                         "Higher = flatter/more grouped, lower = more terracing; "
                         "inf == --placement group, 0 == --placement drape")
    ap.add_argument("--no-base", action="store_true",
                    help="exclude the ground/terrain plane from the exported STL (buildings only). "
                         "Terrain still backs the voxel remesh internally -- an A/B test showed "
                         "excluding it from the JOIN makes ordinary ground-level buildings visibly "
                         "worse (shattered vs. solid); this only strips it from the final file, as a "
                         "late boolean subtraction, matching a normal CFD obstacle-only export.")
    args = ap.parse_args()

    domain = load_domain_polygon(bbox=args.bbox, domain_geojson=args.domain_geojson,
                                 domain_crs=args.domain_crs)
    build_domain_stl(domain, args.output, step=args.step, voxel_size=args.voxel_size,
                     target_reduction=args.target_reduction, decimate_error=args.decimate_error,
                     workers=args.workers, store_dir=args.store, workdir=args.workdir,
                     include_base=not args.no_base, placement=args.placement,
                     coupling_lambda=args.coupling_lambda)


if __name__ == "__main__":
    main()
