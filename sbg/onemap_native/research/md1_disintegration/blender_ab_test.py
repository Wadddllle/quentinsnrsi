"""Blender headless: import raw / weld-only / dedup+oriented variants of MD1
(and piece03), voxel-remesh each at 2m with NO solidify (isolating exactly
what today's band-aid papers over), export STL + a render for visual
comparison. Run via:
  <blender> --background --python blender_ab_test.py -- <out_dir>
"""
import sys
import bpy

argv = sys.argv[sys.argv.index("--") + 1:]
out_dir = argv[0]

VOXEL = 2.0

variants = {
    "00_raw": "00_tile9_0_batch0.obj",
    "00_weldonly": "00_tile9_0_batch0_weldonly.obj",
    "00_dedup": "00_tile9_0_batch0_dedup_oriented.obj",
    "03_raw": "03_tile18_0_batch2.obj",
    "03_weldonly": "03_tile18_0_batch2_weldonly.obj",
    "03_dedup": "03_tile18_0_batch2_dedup_oriented.obj",
}

for name, fn in variants.items():
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete()
    bpy.ops.wm.obj_import(filepath=f"{out_dir}/{fn}", forward_axis="Y", up_axis="Z")
    obj = bpy.context.selected_objects[0]
    bpy.context.view_layer.objects.active = obj

    # weld (all variants need this -- even "dedup" was welded in numpy but
    # OBJ export/reimport can reintroduce float precision splits)
    mod = obj.modifiers.new("weld", "WELD")
    mod.merge_threshold = 0.02
    bpy.ops.object.modifier_apply(modifier=mod.name)

    n_faces_before = len(obj.data.polygons)

    rm = obj.modifiers.new("remesh", "REMESH")
    rm.mode = "VOXEL"
    rm.voxel_size = VOXEL
    bpy.ops.object.modifier_apply(modifier=rm.name)

    n_faces_after = len(obj.data.polygons)

    # volume via bpy (approx, mesh may be non-manifold but good enough for A/B)
    import bmesh
    bm = bmesh.new()
    bm.from_mesh(obj.data)
    vol = bm.calc_volume(signed=True)
    bm.free()

    bpy.ops.wm.stl_export(filepath=f"{out_dir}/ab_{name}.stl", export_selected_objects=False)
    print(f"AB_RESULT {name}: faces_before_remesh={n_faces_before} faces_after={n_faces_after} "
          f"volume={vol:.1f} bbox={[round(x,1) for x in obj.dimensions]}", file=sys.stderr)

print("DONE", file=sys.stderr)
