"""Same stages as fuse_stl.py, but exports a full-domain snapshot after EACH
stage (import, terrain-solidify, join, voxel-remesh) so we can crop/inspect
what's actually happening around MD1 and the neighbor building at each point
in the REAL pipeline (not an isolated single-building test)."""
import sys
import bpy

argv = sys.argv[sys.argv.index("--") + 1:]
out_dir = argv[0]
VOXEL = 2.0
SOLIDIFY = 60.0  # matches production SOLIDIFY_M

bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete()
bpy.ops.wm.ply_import(filepath=f"{out_dir}/terrain.ply", up_axis="Z", forward_axis="Y")
terrain = list(bpy.context.scene.objects)[0]
terrain.name = "terrain"
before = set(bpy.context.scene.objects)
bpy.ops.wm.ply_import(filepath=f"{out_dir}/buildings.ply", up_axis="Z", forward_axis="Y")
buildings = [o for o in bpy.context.scene.objects if o not in before]
print(f"terrain + {len(buildings)} building object(s)", file=sys.stderr)

# --- stage 0: raw import ---
bpy.ops.object.select_all(action="DESELECT")
for o in [terrain] + buildings:
    o.select_set(True)
bpy.ops.wm.stl_export(filepath=f"{out_dir}/stage0_import.stl", export_selected_objects=True)
print("STAGE 0 done: raw import", file=sys.stderr)

# --- stage 1: terrain solidify ---
mod = terrain.modifiers.new(name="solidify", type="SOLIDIFY")
mod.thickness = SOLIDIFY
mod.offset = -1.0
mod.use_rim = True
bpy.context.view_layer.objects.active = terrain
bpy.ops.object.modifier_apply(modifier=mod.name)

bpy.ops.object.select_all(action="DESELECT")
for o in [terrain] + buildings:
    o.select_set(True)
bpy.ops.wm.stl_export(filepath=f"{out_dir}/stage1_terrain_solidified.stl", export_selected_objects=True)
print("STAGE 1 done: terrain solidified", file=sys.stderr)

# --- stage 2: join (soup, pre-remesh) ---
bpy.ops.object.select_all(action="DESELECT")
terrain.select_set(True)
for o in buildings:
    o.select_set(True)
bpy.context.view_layer.objects.active = terrain
bpy.ops.object.join()
soup = bpy.context.view_layer.objects.active
print(f"  joined: {len(soup.data.polygons)} faces", file=sys.stderr)
bpy.ops.wm.stl_export(filepath=f"{out_dir}/stage2_joined_soup.stl", export_selected_objects=False)
print("STAGE 2 done: joined soup", file=sys.stderr)

# --- stage 3: voxel remesh ---
rm = soup.modifiers.new(name="remesh", type="REMESH")
rm.mode = "VOXEL"
rm.voxel_size = VOXEL
bpy.context.view_layer.objects.active = soup
bpy.ops.object.modifier_apply(modifier=rm.name)
print(f"  remeshed: {len(soup.data.polygons)} faces", file=sys.stderr)
bpy.ops.wm.stl_export(filepath=f"{out_dir}/stage3_voxel_remeshed.stl", export_selected_objects=False)
print("STAGE 3 done: voxel remeshed", file=sys.stderr)

print("ALL STAGES DONE", file=sys.stderr)
