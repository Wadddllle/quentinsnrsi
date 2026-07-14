"""Deliverable 5: fuse an SBG town-scale OBJ (terrain + buildings, exported
via `cjio ... export obj`) into one solid and export STL.

Runs inside Blender's own Python (bpy), not the project .venv -- no sbg/
shapely/pyproj/pymeshfix imports here, plain stdlib + bpy only. Invoke via:

  blender --background --python sbg/blender/export_stl.py -- \\
      --input data/cbd_town_test.obj \\
      --deep-extrude 30 \\
      --voxel-size 2.0 \\
      --decimate-ratio 0.15 \\
      --output data/cbd_town_test.stl

Then run `sbg/blender/repair_stl.py` (plain .venv Python, no Blender) on the
result to close any last tiny defect and confirm watertightness.

Pipeline (rewritten after the first design -- see below for why):
  1. Import the OBJ. cjio's obj export gives one named `o` group per
     CityObject ("terrain" + one per building), which Blender preserves as
     separate objects on import -- this is what lets us tell terrain and
     buildings apart, no custom CityJSON plugin needed. Axis conversion is
     disabled on import (forward_axis='Y', up_axis='Z') -- confirmed by
     testing that Blender's default OBJ axis convention (Y-up) silently
     swaps our real Y (northing) and Z (elevation) otherwise, since our data
     is already Z-up Cartesian EPSG:3414 meters.
  2. Solidify the terrain. The TINRelief import is a thin height-field
     *sheet* (2-manifold surface with open boundary loops: the outer domain
     edge, plus one per building footprint hole) -- not a closed volume.
     Solidify (offset=-1, i.e. extrude fully downward by --deep-extrude)
     turns it into a real slab with rim faces closing every boundary loop.
  3. Deep-extrude: push each building's base ring down by --deep-extrude
     meters so it plunges through the solid terrain slab as a pillar,
     guaranteeing real volumetric overlap instead of a knife-edge surface
     coincidence (Phase 3 already makes base and terrain vertices land on
     identical (x,y,z), which is exactly the degenerate case CAD boolean
     kernels choke on).
  4. Join everything -- terrain slab + every plunged building -- into ONE
     mesh object. Deliberately NOT a boolean union.
  5. Voxel Remesh that joined soup at --voxel-size. This is the actual key
     move (see below).
  6. Export STL.

Why not boolean union (the original design)? Tested it first: Blender's
EXACT boolean solver, run as a single batched union of all ~2600 buildings
against the terrain, catastrophically fragmented the result into 77+
disconnected pieces on just a 27-building test (confirmed the terrain's own
~8000 faces were nowhere to be found intact afterward). Doing the unions
one-at-a-time instead of batching recovered most of it (one dominant
component held ~78% of the geometry) but still left ~360 leftover
fragments, traced to buildings with near-duplicate/collinear footprint
vertices -- the same OSM data-quality pathology behind the original
Interlace "extruded blob" bug (see onemap_landmarks/NOTES.md) -- tripping up
the solver at the exact-coincidence seam. Blender's mesh boolean is simply
not robust against that much real-world data mess at this object count.

Why voxel remesh instead: it doesn't compute a topological union at all --
it samples inside/outside on a regular 3D grid and re-extracts one surface,
so it doesn't care whether the input is self-intersecting, overlapping, or
fragmented into hundreds of pieces. Confirmed empirically: the exact same
27-building test, using plain `join` (zero boolean ops, overlaps and all)
followed by Voxel Remesh, produced a single connected body with only 2 bad
edges out of ~602,000 -- pymeshfix's targeted repair (not its default
whole-mesh mode -- see repair_stl.py) closed that last gap with the volume
unchanged to 6 significant figures. This sidesteps the entire boolean-
robustness problem rather than patching it.

Why this resolution isn't a compromise: the CFD grid this feeds is expected
to be ~3m regardless, so resampling geometry to ~2-3m via remesh isn't
throwing away real fidelity -- anything finer than the CFD mesh's own
resolution would be discarded downstream anyway. Face count scales with
domain surface area / voxel_size^2, not building count, so this also
sidesteps the earlier per-building performance question entirely -- no
2600-step sequential boolean chain needed. (A too-fine voxel size, e.g.
0.3-1m, is NOT a free correctness upgrade -- it blows up face count for no
benefit, confirmed at 4.5M faces for a mere 500m x 500m test domain.)

Debris cleanup happens here, in Blender, not in repair_stl.py's .venv/
trimesh step -- confirmed on the full 2,605-building CBD domain (10.5M
faces) that trimesh's connected-component split + STL export of a mesh
that size gets OOM-killed on this box's 7.4GB RAM, whereas Blender (compiled
C++ mesh ops) already handles that same file natively without issue. Voxel
remesh at full domain scale leaves a small number of degenerate boundary
slivers (368 fragments, 1-12 faces each, sitting exactly on the domain's
own edge -- a remesh precision artifact, not lost buildings) alongside the
one real fused body; `bpy.ops.mesh.separate(type='LOOSE')` splits those out
so they can be dropped by face-count threshold before ever leaving Blender.

Decimate (--decimate-ratio, 0-1) runs last, after debris is dropped. Voxel
remesh samples uniformly by surface area, so a flat wall/roof/terrain patch
gets just as densely tessellated as anything else -- genuinely wasteful,
confirmed visually (wireframe of a flat region at 2m voxel size renders
solid from any moderately zoomed-out view) and numerically (5.26M faces for
what's still just LOD1 boxes + a coarse terrain TIN).

Uses Decimate's COLLAPSE mode (quadric edge collapse, ratio-based) --
deliberately NOT Planar/DISSOLVE mode (collapsing near-coplanar faces
within an angle threshold), despite Planar sounding like the more targeted
fit for "flat regions are oversampled." Tested Planar first and it hung:
5+ minutes with no result on just 200K faces (confirmed via `top` it wasn't
memory-bound, just algorithmically slow -- Blender's limited-dissolve is a
known-slow operation on large meshes). COLLAPSE, a completely different and
much better-optimized algorithm, decimated the same 241K-face mesh to 5-20%
in under 9 seconds at any tested ratio. It simplifies more uniformly
(doesn't specifically spare flat regions the way Planar would), but
verified the shape cost is small: at ratio=0.15 on the 27-building test,
volume after decimate+repair matched the pre-decimate volume to within
0.001% (9,760,697.7 vs 9,760,826.9 m^3) despite a 5x face reduction.

This also matters for repair_stl.py's pymeshfix step: that library's memory
use scales with face count (confirmed: 10.5M faces exceeded 8GB+ RAM and
was still climbing when killed, before decimation existed in this
pipeline). Decimating first, before any repair, keeps pymeshfix's input
small -- confirmed the 40K-60K-face post-decimate mesh repairs in ~1.3s
instead of risking OOM.
"""
import bmesh
import bpy
import sys
import time


def parse_args(argv):
    if "--" in argv:
        argv = argv[argv.index("--") + 1:]
    else:
        argv = []
    args = {"deep_extrude": 30.0, "voxel_size": 2.0, "debris_faces": 100, "decimate_ratio": 0.15}
    i = 0
    while i < len(argv):
        key = argv[i].lstrip("-").replace("-", "_")
        if key in ("input", "output"):
            args[key] = argv[i + 1]
            i += 2
        elif key in ("deep_extrude", "voxel_size", "decimate_ratio"):
            args[key] = float(argv[i + 1])
            i += 2
        elif key == "debris_faces":
            args[key] = int(argv[i + 1])
            i += 2
        else:
            raise ValueError(f"Unknown arg: {argv[i]}")
    if "input" not in args or "output" not in args:
        raise ValueError("--input and --output are required")
    return args


def clear_scene():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)
    for mesh in list(bpy.data.meshes):
        if mesh.users == 0:
            bpy.data.meshes.remove(mesh)


def plunge_base(obj, drop):
    """Moves every vertex except those at the object's max Z (the flat roof)
    down by `drop` meters, in the object's local mesh data.
    """
    mesh = obj.data
    zs = [v.co.z for v in mesh.vertices]
    top_z = max(zs)
    bm = bmesh.new()
    bm.from_mesh(mesh)
    bm.verts.ensure_lookup_table()
    for v in bm.verts:
        if v.co.z < top_z - 1e-3:
            v.co.z -= drop
    bm.to_mesh(mesh)
    bm.free()
    mesh.update()


def drop_debris(obj, debris_faces):
    """Splits obj into its disconnected loose parts and deletes any with
    <= debris_faces faces (see module docstring: full-scale voxel remesh
    output has left ~368 degenerate boundary slivers, 1-12 faces each,
    alongside the one real fused body). Errors out instead of guessing if
    more than one surviving piece is bigger than the threshold -- that's
    not debris, something needs investigating.

    Returns the single surviving object.
    """
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.separate(type="LOOSE")
    bpy.ops.object.mode_set(mode="OBJECT")

    parts = [o for o in bpy.context.selected_objects]
    sizes = [(o, len(o.data.polygons)) for o in parts]
    survivors = [o for o, n in sizes if n > debris_faces]
    debris = [(o, n) for o, n in sizes if n <= debris_faces]

    if len(survivors) != 1:
        sizes_str = sorted((n for o, n in sizes), reverse=True)[:10]
        raise RuntimeError(
            f"Expected exactly 1 surviving piece above debris_faces={debris_faces}, got {len(survivors)} "
            f"(top sizes: {sizes_str}). Could be real geometry that failed to fuse -- investigate before "
            f"discarding, not auto-dropping."
        )

    dropped_total = sum(n for o, n in debris)
    survivor_faces = len(survivors[0].data.polygons)
    print(
        f"  dropping {len(debris)} debris fragment(s), {dropped_total} faces total "
        f"(largest {max((n for o, n in debris), default=0)}) -- kept main body ({survivor_faces} faces)",
        file=sys.stderr,
    )
    for o, n in debris:
        bpy.data.objects.remove(o, do_unlink=True)

    return survivors[0]


def main():
    # Real per-step wall-clock timing -- this is exactly where "voxel remesh"
    # and "decimate" used to be bundled into one opaque job-log stage
    # (blender_export), making it impossible to tell from the log alone
    # which one actually cost the time.
    t0 = time.perf_counter()

    def _lap(label):
        nonlocal t0
        now = time.perf_counter()
        print(f"  [{label}] {now - t0:.1f}s", file=sys.stderr)
        t0 = now

    args = parse_args(sys.argv)

    clear_scene()

    print(f"Importing {args['input']}...", file=sys.stderr)
    bpy.ops.wm.obj_import(filepath=args["input"], forward_axis="Y", up_axis="Z")

    all_objs = list(bpy.context.scene.objects)
    terrain = next((o for o in all_objs if o.name == "terrain"), None)
    buildings = [o for o in all_objs if o is not terrain]
    if terrain is None:
        raise RuntimeError("No object named 'terrain' found after import")
    print(f"  terrain + {len(buildings)} buildings", file=sys.stderr)
    _lap("import")

    print(f"Solidifying terrain (thickness={args['deep_extrude']}m)...", file=sys.stderr)
    mod = terrain.modifiers.new(name="solidify", type="SOLIDIFY")
    mod.thickness = args["deep_extrude"]
    mod.offset = -1.0
    mod.use_rim = True
    bpy.context.view_layer.objects.active = terrain
    bpy.ops.object.modifier_apply(modifier=mod.name)
    print(f"  solid terrain: {len(terrain.data.vertices)} vertices, {len(terrain.data.polygons)} faces", file=sys.stderr)
    _lap("solidify terrain")

    print(f"Plunging building bases by {args['deep_extrude']}m...", file=sys.stderr)
    for obj in buildings:
        plunge_base(obj, args["deep_extrude"])
    _lap("plunge building bases")

    print("Joining terrain + buildings into one mesh (no boolean)...", file=sys.stderr)
    bpy.ops.object.select_all(action="DESELECT")
    terrain.select_set(True)
    for obj in buildings:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = terrain
    bpy.ops.object.join()
    soup = bpy.context.view_layer.objects.active
    print(f"  joined soup: {len(soup.data.vertices)} vertices, {len(soup.data.polygons)} faces", file=sys.stderr)
    _lap("join")

    print(f"Voxel remeshing (voxel_size={args['voxel_size']}m)...", file=sys.stderr)
    modr = soup.modifiers.new(name="remesh", type="REMESH")
    modr.mode = "VOXEL"
    modr.voxel_size = args["voxel_size"]
    bpy.context.view_layer.objects.active = soup
    bpy.ops.object.modifier_apply(modifier=modr.name)
    print(f"  remeshed: {len(soup.data.vertices)} vertices, {len(soup.data.polygons)} faces", file=sys.stderr)
    _lap("voxel remesh")

    print(f"Dropping debris fragments (<= {args['debris_faces']} faces)...", file=sys.stderr)
    final = drop_debris(soup, args["debris_faces"])
    _lap("drop debris")

    if args["decimate_ratio"] < 1.0:
        pre_decimate_faces = len(final.data.polygons)
        print(f"Decimating (COLLAPSE, ratio={args['decimate_ratio']})...", file=sys.stderr)
        modd = final.modifiers.new(name="decimate", type="DECIMATE")
        modd.decimate_type = "COLLAPSE"
        modd.ratio = args["decimate_ratio"]
        bpy.context.view_layer.objects.active = final
        bpy.ops.object.modifier_apply(modifier=modd.name)
        print(f"  decimated: {len(final.data.vertices)} vertices, {len(final.data.polygons)} faces", file=sys.stderr)
        _lap(f"decimate COLLAPSE ({pre_decimate_faces} -> {len(final.data.polygons)} faces)")
    else:
        print("Skipping decimate (--decimate-ratio >= 1.0)", file=sys.stderr)

    print(f"Exporting {args['output']}...", file=sys.stderr)
    bpy.ops.object.select_all(action="DESELECT")
    final.select_set(True)
    bpy.context.view_layer.objects.active = final
    bpy.ops.wm.stl_export(filepath=args["output"], export_selected_objects=True)
    _lap("export STL")
    print("Done.", file=sys.stderr)


if __name__ == "__main__":
    main()
