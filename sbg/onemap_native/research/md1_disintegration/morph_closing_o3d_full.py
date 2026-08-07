"""Full pipeline, fast: open3d voxelize (0.1s, not trimesh's 33s) -> dense
numpy bool grid -> scipy dilate/fill/erode closing -> skimage marching_cubes.
On the REAL raw joined scene (stage2_joined_soup.stl -- terrain + both
buildings + skirts, never touched by Blender), at finer pitch than before
since we can now afford it."""
import time
import numpy as np
import trimesh
import open3d as o3d
from scipy import ndimage
from skimage import measure

trace_dir = "/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583/scratchpad/trace"
out_dir = "/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583/scratchpad/md1_area"

mesh_tm = trimesh.load(f"{trace_dir}/stage2_joined_soup.stl", process=False)
print(f"input: {len(mesh_tm.vertices)} verts, {len(mesh_tm.faces)} faces, "
      f"extent={mesh_tm.bounds[1]-mesh_tm.bounds[0]}", flush=True)

PITCH = 1.0            # finer than the earlier 2m test, affordable now
CLOSE_RADIUS_VOX = 3    # ~3m closing radius at this pitch

t_total0 = time.perf_counter()

# --- voxelize via open3d (the fast part) ---
t0 = time.perf_counter()
o3d_mesh = o3d.geometry.TriangleMesh()
o3d_mesh.vertices = o3d.utility.Vector3dVector(np.array(mesh_tm.vertices))
o3d_mesh.triangles = o3d.utility.Vector3iVector(np.array(mesh_tm.faces))
o3d_vox = o3d.geometry.VoxelGrid.create_from_triangle_mesh(o3d_mesh, voxel_size=PITCH)
voxels = o3d_vox.get_voxels()
idx = np.array([v.grid_index for v in voxels])
origin = o3d_vox.origin
mn = idx.min(axis=0)
shape = tuple(idx.max(axis=0) - mn + 1)
occ = np.zeros(shape, dtype=bool)
occ[idx[:,0]-mn[0], idx[:,1]-mn[1], idx[:,2]-mn[2]] = True
t_voxelize = time.perf_counter() - t0
print(f"[voxelize, open3d] {t_voxelize:.2f}s, grid={occ.shape}, occupied={occ.sum()}", flush=True)

# world-coordinate transform for this grid: world = origin + (index+mn) * PITCH
grid_origin_world = origin + mn * PITCH

# --- morphological closing ---
t0 = time.perf_counter()
struct = ndimage.generate_binary_structure(3, 1)
dilated = ndimage.binary_dilation(occ, structure=struct, iterations=CLOSE_RADIUS_VOX)
solid = ndimage.binary_fill_holes(dilated)
closed = ndimage.binary_erosion(solid, structure=struct, iterations=CLOSE_RADIUS_VOX)
t_morph = time.perf_counter() - t0
print(f"[morph close] {t_morph:.2f}s, final={closed.sum()} (dilated={dilated.sum()}, filled={solid.sum()})", flush=True)

# --- marching cubes, map back to world coords ---
t0 = time.perf_counter()
verts, faces, normals, values = measure.marching_cubes(closed.astype(np.float32), level=0.5)
verts_world = verts * PITCH + grid_origin_world
out_mesh = trimesh.Trimesh(verts_world, faces, process=False)
t_mc = time.perf_counter() - t0
print(f"[marching_cubes] {t_mc:.2f}s, {len(verts_world)} verts, {len(faces)} faces", flush=True)

t_total = time.perf_counter() - t_total0
out_mesh.export(f"{out_dir}/morph_o3d_full.stl")

check = trimesh.load(f"{out_dir}/morph_o3d_full.stl", process=False)
check.merge_vertices(digits_vertex=4)
print(f"watertight(merged)={check.is_watertight}, volume={abs(check.volume):.0f}, bodies={check.body_count}")
print(f"TOTAL pipeline time: {t_total:.2f}s (voxelize {t_voxelize:.2f}s + morph {t_morph:.2f}s + mc {t_mc:.2f}s)")
