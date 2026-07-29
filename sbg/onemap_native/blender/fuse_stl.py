"""Blender-side fuse for the tile-native pipeline (runs in Blender's bundled
Python, invoked via --background --python ... --).

Input OBJ has an object 'terrain' (a DTM surface, pads pre-flattened) plus one
'buildings' object (real OneMap meshes already sat on the terrain, with plunge
skirts). This: solidifies the terrain into a slab, joins everything, voxel
remeshes the whole scene once (no boolean union -- proven not to work at this
scale; no Sharp pre-pass -- not needed once buildings embed in the slab), drops
loose debris slivers, and exports an STL. Decimation + watertight repair happen
afterward in the .venv via sbg.blender.repair_stl.

Usage:
  blender --background --python fuse_stl.py -- \
      --input scene.obj --output out.stl --solidify 60 --voxel-size 2.0
"""
import sys
import time

import bpy


def _arg(name, default=None, cast=str):
    argv = sys.argv[sys.argv.index("--") + 1:]
    return cast(argv[argv.index(name) + 1]) if name in argv else default


def _clip_to_box(obj, xmin, ymin, xmax, ymax):
    """Trim obj to the exact domain box with clean vertical walls, via 4 plane
    bisects with fill -- done in Blender's C (cheap) instead of loading the full
    mesh into trimesh (which held 4 extra copies and was OOMing the box). Gives
    the CFD domain planar inlet/outlet/lateral faces and removes the ragged
    terrain-margin 'teeth'."""
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    bpy.ops.object.mode_set(mode="EDIT")
    # (plane point, plane normal, clear which side). Keep interior of the box.
    for co, no in [((xmin, 0, 0), (1, 0, 0)), ((xmax, 0, 0), (-1, 0, 0)),
                   ((0, ymin, 0), (0, 1, 0)), ((0, ymax, 0), (0, -1, 0))]:
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.mesh.bisect(plane_co=co, plane_no=no, use_fill=True,
                            clear_inner=True, clear_outer=False)
    bpy.ops.object.mode_set(mode="OBJECT")


def main():
    terrain_path = _arg("--terrain")
    buildings_path = _arg("--buildings")
    inp = _arg("--input")  # legacy single-OBJ path (still supported)
    out = _arg("--output")
    solidify = _arg("--solidify", 60.0, float)
    voxel = _arg("--voxel-size", 2.0, float)
    debris_faces = _arg("--debris-faces", 200, int)
    skip_debris = "--skip-debris" in sys.argv[sys.argv.index("--") + 1:]
    clip_bbox = _arg("--clip-bbox", None)  # "xmin,ymin,xmax,ymax"

    t0 = [time.perf_counter()]

    def lap(label):
        now = time.perf_counter()
        print(f"  [{label}] {now - t0[0]:.1f}s", file=sys.stderr)
        t0[0] = now

    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()
    if terrain_path and buildings_path:
        # Two binary PLYs (fast path): terrain first into an empty scene, then
        # buildings -- the file split stands in for the OBJ's object markers.
        bpy.ops.wm.ply_import(filepath=terrain_path, up_axis="Z", forward_axis="Y")
        terrain = list(bpy.context.scene.objects)[0]
        terrain.name = "terrain"
        before = set(bpy.context.scene.objects)
        bpy.ops.wm.ply_import(filepath=buildings_path, up_axis="Z", forward_axis="Y")
        buildings = [o for o in bpy.context.scene.objects if o not in before]
    else:  # legacy single-OBJ path
        bpy.ops.wm.obj_import(filepath=inp, forward_axis="Y", up_axis="Z")
        objs = list(bpy.context.scene.objects)
        terrain = next((o for o in objs if o.name.startswith("terrain")), None)
        if terrain is None:
            raise RuntimeError("no 'terrain' object found in input OBJ")
        buildings = [o for o in objs if o is not terrain]
    print(f"terrain + {len(buildings)} building object(s)", file=sys.stderr)
    lap("import")

    mod = terrain.modifiers.new(name="solidify", type="SOLIDIFY")
    mod.thickness = solidify
    mod.offset = -1.0
    mod.use_rim = True
    bpy.context.view_layer.objects.active = terrain
    bpy.ops.object.modifier_apply(modifier=mod.name)
    lap("solidify terrain")

    bpy.ops.object.select_all(action="DESELECT")
    terrain.select_set(True)
    for o in buildings:
        o.select_set(True)
    bpy.context.view_layer.objects.active = terrain
    bpy.ops.object.join()
    soup = bpy.context.view_layer.objects.active
    print(f"  joined: {len(soup.data.polygons)} faces", file=sys.stderr)
    lap("join")

    rm = soup.modifiers.new(name="remesh", type="REMESH")
    rm.mode = "VOXEL"
    rm.voxel_size = voxel
    bpy.context.view_layer.objects.active = soup
    bpy.ops.object.modifier_apply(modifier=rm.name)
    print(f"  remeshed: {len(soup.data.polygons)} faces", file=sys.stderr)
    lap("voxel remesh")

    # Clip FIRST (a bisect can leave a sliver disconnected at the boundary), then
    # drop debris -- so the debris pass cleans up both remesh slivers and any
    # clip fragments, guaranteeing a single clean body for the repair stage.
    if clip_bbox:
        xmin, ymin, xmax, ymax = (float(v) for v in clip_bbox.split(","))
        _clip_to_box(soup, xmin, ymin, xmax, ymax)
        print(f"  clipped to box, {len(soup.data.polygons)} faces", file=sys.stderr)
        lap("clip to domain box")

    keep = soup
    if not skip_debris:
        # separate(LOOSE) is a slow (~30s at 5M faces) connected-component split;
        # skip it when a downstream step already keeps the largest component (the
        # trimesh clip does). Left available for standalone use.
        bpy.ops.object.select_all(action="DESELECT")
        soup.select_set(True)
        bpy.context.view_layer.objects.active = soup
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.mesh.separate(type="LOOSE")
        bpy.ops.object.mode_set(mode="OBJECT")
        parts = list(bpy.context.selected_objects)
        sizes = sorted(((o, len(o.data.polygons)) for o in parts), key=lambda x: -x[1])
        print(f"  parts: {len(parts)}, top: {[n for _, n in sizes[:5]]}", file=sys.stderr)
        keep = sizes[0][0]
        for o, n in sizes[1:]:
            if n > debris_faces:
                print(f"  WARNING dropping a {n}-face piece (> debris threshold)", file=sys.stderr)
            bpy.data.objects.remove(o, do_unlink=True)
        lap("drop debris")

    bpy.ops.object.select_all(action="DESELECT")
    keep.select_set(True)
    bpy.context.view_layer.objects.active = keep
    # Export PLY (indexed, welded) not STL (per-triangle soup) when asked -- STL
    # un-welds the voxel-remesh output, and re-welding it in trimesh at a rounded
    # precision manufactures hundreds of SPURIOUS non-manifold edges (the mesh is
    # really ~2 defects). PLY carries Blender's own clean welding straight
    # through, so downstream decimation/repair sees the real (tiny) defect set.
    if out.lower().endswith(".ply"):
        bpy.ops.wm.ply_export(filepath=out, export_selected_objects=True, ascii_format=False)
    else:
        bpy.ops.wm.stl_export(filepath=out, export_selected_objects=True)
    print(f"Exported {out}", file=sys.stderr)
    lap("export")


if __name__ == "__main__":
    main()
