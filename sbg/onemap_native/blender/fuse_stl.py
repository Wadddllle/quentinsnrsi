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


def main():
    inp = _arg("--input")
    out = _arg("--output")
    solidify = _arg("--solidify", 60.0, float)
    voxel = _arg("--voxel-size", 2.0, float)
    debris_faces = _arg("--debris-faces", 200, int)

    t0 = [time.perf_counter()]

    def lap(label):
        now = time.perf_counter()
        print(f"  [{label}] {now - t0[0]:.1f}s", file=sys.stderr)
        t0[0] = now

    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()
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
    bpy.ops.wm.stl_export(filepath=out, export_selected_objects=True)
    print(f"Exported {out}", file=sys.stderr)
    lap("export")


if __name__ == "__main__":
    main()
