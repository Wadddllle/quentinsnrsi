"""Is open3d's VoxelGrid.create_from_triangle_mesh() actually faster than
trimesh's voxelized() for the SAME raw messy scene? trimesh took 30.5s at
pitch=2m on stage2_joined_soup.stl (318x272x172m, 14k faces) -- that's the
real bottleneck blocking morphological closing from raw geometry at
production scale. Test open3d directly, same input, same pitch."""
import time
import numpy as np
import trimesh
import open3d as o3d

trace_dir = "/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583/scratchpad/trace"

mesh_tm = trimesh.load(f"{trace_dir}/stage2_joined_soup.stl", process=False)
print(f"input: {len(mesh_tm.vertices)} verts, {len(mesh_tm.faces)} faces, "
      f"extent={mesh_tm.bounds[1]-mesh_tm.bounds[0]}", flush=True)

PITCH = 2.0

# --- trimesh baseline (already measured, re-confirm) ---
t0 = time.perf_counter()
vox_tm = mesh_tm.voxelized(pitch=PITCH)
t_tm = time.perf_counter() - t0
print(f"trimesh voxelized(): {t_tm:.1f}s, grid shape={vox_tm.matrix.shape}, "
      f"occupied={vox_tm.matrix.sum()}", flush=True)

# --- open3d ---
o3d_mesh = o3d.geometry.TriangleMesh()
o3d_mesh.vertices = o3d.utility.Vector3dVector(np.array(mesh_tm.vertices))
o3d_mesh.triangles = o3d.utility.Vector3iVector(np.array(mesh_tm.faces))

t0 = time.perf_counter()
o3d_vox = o3d.geometry.VoxelGrid.create_from_triangle_mesh(o3d_mesh, voxel_size=PITCH)
t_o3d = time.perf_counter() - t0
n_voxels = len(o3d_vox.get_voxels())
print(f"open3d create_from_triangle_mesh(): {t_o3d:.1f}s, occupied={n_voxels}", flush=True)
print(f"speedup: {t_tm/max(t_o3d,1e-6):.1f}x")

# also check open3d's own bool-grid extraction path for the full array (needed
# to actually run scipy dilate/fill/erode on it)
t0 = time.perf_counter()
voxels = o3d_vox.get_voxels()
idx = np.array([v.grid_index for v in voxels])
mn = idx.min(axis=0); mx = idx.max(axis=0)
shape = (mx - mn + 1)
grid = np.zeros(shape, dtype=bool)
grid[idx[:,0]-mn[0], idx[:,1]-mn[1], idx[:,2]-mn[2]] = True
t_extract = time.perf_counter() - t0
print(f"extract to dense numpy bool grid: {t_extract:.1f}s, shape={grid.shape}, occupied={grid.sum()}")
