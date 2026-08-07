import time
import numpy as np
import trimesh
import open3d as o3d

trace_dir = "/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583/scratchpad/trace"
mesh_tm = trimesh.load(f"{trace_dir}/stage2_joined_soup.stl", process=False)
o3d_mesh = o3d.geometry.TriangleMesh()
o3d_mesh.vertices = o3d.utility.Vector3dVector(np.array(mesh_tm.vertices))
o3d_mesh.triangles = o3d.utility.Vector3iVector(np.array(mesh_tm.faces))

for pitch in [1.0, 0.5, 0.25]:
    t0 = time.perf_counter()
    vg = o3d.geometry.VoxelGrid.create_from_triangle_mesh(o3d_mesh, voxel_size=pitch)
    t_vox = time.perf_counter() - t0

    t0 = time.perf_counter()
    voxels = vg.get_voxels()
    t_getvoxels = time.perf_counter() - t0

    t0 = time.perf_counter()
    idx = np.array([v.grid_index for v in voxels])
    t_listcomp = time.perf_counter() - t0

    n = len(voxels)
    print(f"pitch={pitch}: n_occupied={n:>10,}  create={t_vox:.2f}s  "
          f"get_voxels()={t_getvoxels:.2f}s  list-comp-extract={t_listcomp:.2f}s  "
          f"per-voxel-extract-cost={1e6*t_listcomp/max(n,1):.2f}us", flush=True)
