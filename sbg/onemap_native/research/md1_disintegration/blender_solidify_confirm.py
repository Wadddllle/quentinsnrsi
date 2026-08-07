import sys
import bpy, bmesh

argv = sys.argv[sys.argv.index("--") + 1:]
out_dir = argv[0]

bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete()
bpy.ops.wm.obj_import(filepath=f"{out_dir}/00_tile9_0_batch0_dedup_oriented.obj", forward_axis="Y", up_axis="Z")
obj = bpy.context.selected_objects[0]
bpy.context.view_layer.objects.active = obj

mod = obj.modifiers.new("weld", "WELD")
mod.merge_threshold = 0.02
bpy.ops.object.modifier_apply(modifier=mod.name)

sol = obj.modifiers.new("solidify", "SOLIDIFY")
sol.thickness = 3.0
sol.offset = 0.0
bpy.ops.object.modifier_apply(modifier=sol.name)

rm = obj.modifiers.new("remesh", "REMESH")
rm.mode = "VOXEL"
rm.voxel_size = 2.0
bpy.ops.object.modifier_apply(modifier=rm.name)

bm = bmesh.new()
bm.from_mesh(obj.data)
vol = bm.calc_volume(signed=True)
bm.free()

bpy.ops.wm.stl_export(filepath=f"{out_dir}/ab_00_dedup_solidify3.stl", export_selected_objects=False)
print(f"AB_RESULT 00_dedup_solidify3: faces={len(obj.data.polygons)} volume={vol:.1f} "
      f"bbox={[round(x,1) for x in obj.dimensions]}", file=sys.stderr)
