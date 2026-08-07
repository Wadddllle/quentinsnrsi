"""Fully vectorized voxelization: sample points on the mesh surface (density
matched to pitch), compute voxel indices via numpy arithmetic (no per-element
Python loop), dedupe via np.unique. Compare speed+correctness against open3d's
VoxelGrid.create_from_triangle_mesh + get_voxels() (which needs a Python loop
to extract indices)."""
import time
import numpy as np
import trimesh
import open3d as o3d

trace_dir = "/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583/scratchpad/trace"
mesh = trimesh.load(f"{trace_dir}/stage2_joined_soup.stl", process=False)
print(f"mesh: {len(mesh.faces)} faces, area={mesh.area:.0f} m^2", flush=True)

for PITCH in [1.0, 0.5, 0.25]:
    origin = mesh.bounds[0]

    # --- open3d baseline (create + Python-loop extract) ---
    o3d_mesh = o3d.geometry.TriangleMesh()
    o3d_mesh.vertices = o3d.utility.Vector3dVector(np.array(mesh.vertices))
    o3d_mesh.triangles = o3d.utility.Vector3iVector(np.array(mesh.faces))
    t0 = time.perf_counter()
    vg = o3d.geometry.VoxelGrid.create_from_triangle_mesh(o3d_mesh, voxel_size=PITCH)
    voxels = vg.get_voxels()
    idx_o3d = np.array([v.grid_index for v in voxels])
    t_o3d = time.perf_counter() - t0
    occ_o3d = set(map(tuple, idx_o3d))

    # --- vectorized: sample surface, floor-div, unique (all numpy, no loop) ---
    t0 = time.perf_counter()
    oversample = 3.0
    n_samples = int(mesh.area / (PITCH * PITCH) * oversample)
    pts, _ = trimesh.sample.sample_surface(mesh, n_samples)
    idx_vec = np.floor((pts - origin) / PITCH).astype(np.int64)
    idx_vec = np.unique(idx_vec, axis=0)
    t_vec = time.perf_counter() - t0
    occ_vec = set(map(tuple, idx_vec))

    overlap = len(occ_o3d & occ_vec)
    print(f"pitch={PITCH}: o3d={t_o3d:.2f}s ({len(occ_o3d):,} vox)  "
          f"vectorized={t_vec:.2f}s ({len(occ_vec):,} vox, {n_samples:,} samples)  "
          f"speedup={t_o3d/max(t_vec,1e-6):.1f}x  "
          f"jaccard_overlap={overlap/len(occ_o3d | occ_vec)*100:.1f}%", flush=True)
