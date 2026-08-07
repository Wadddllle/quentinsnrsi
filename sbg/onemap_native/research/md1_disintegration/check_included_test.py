import time
import numpy as np
import trimesh
import open3d as o3d

trace_dir = "/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583/scratchpad/trace"
mesh = trimesh.load(f"{trace_dir}/stage2_joined_soup.stl", process=False)

for PITCH in [2.0, 1.0]:
    o3d_mesh = o3d.geometry.TriangleMesh()
    o3d_mesh.vertices = o3d.utility.Vector3dVector(np.array(mesh.vertices))
    o3d_mesh.triangles = o3d.utility.Vector3iVector(np.array(mesh.faces))

    t0 = time.perf_counter()
    vg = o3d.geometry.VoxelGrid.create_from_triangle_mesh(o3d_mesh, voxel_size=PITCH)
    t_create = time.perf_counter() - t0

    # baseline: get_voxels() python loop
    t0 = time.perf_counter()
    voxels = vg.get_voxels()
    idx = np.array([v.grid_index for v in voxels])
    t_loop = time.perf_counter() - t0
    grid_shape = tuple(idx.max(axis=0) - idx.min(axis=0) + 1)
    mn = idx.min(axis=0)
    occ_loop = np.zeros(grid_shape, dtype=bool)
    occ_loop[idx[:,0]-mn[0], idx[:,1]-mn[1], idx[:,2]-mn[2]] = True

    # candidate: build the FULL dense grid of query points, call check_if_included once
    t0 = time.perf_counter()
    origin = vg.origin
    nx, ny, nz = grid_shape
    xs = origin[0] + (np.arange(nx) + mn[0] + 0.5) * PITCH
    ys = origin[1] + (np.arange(ny) + mn[1] + 0.5) * PITCH
    zs = origin[2] + (np.arange(nz) + mn[2] + 0.5) * PITCH
    gx, gy, gz = np.meshgrid(xs, ys, zs, indexing="ij")
    pts = np.stack([gx.ravel(), gy.ravel(), gz.ravel()], axis=1)
    t_grid_build = time.perf_counter() - t0

    t0 = time.perf_counter()
    included = vg.check_if_included(o3d.utility.Vector3dVector(pts))
    t_query = time.perf_counter() - t0

    t0 = time.perf_counter()
    occ_check = np.array(included, dtype=bool).reshape(grid_shape)
    t_reshape = time.perf_counter() - t0

    match = np.array_equal(occ_loop, occ_check)
    print(f"pitch={PITCH}: grid={grid_shape} total_cells={np.prod(grid_shape):,} occupied={idx.shape[0]:,}")
    print(f"  get_voxels()-loop path: create={t_create:.2f}s + loop_extract={t_loop:.2f}s = {t_create+t_loop:.2f}s")
    print(f"  check_if_included path: create={t_create:.2f}s + grid_build={t_grid_build:.2f}s + "
          f"query={t_query:.2f}s + reshape={t_reshape:.2f}s = "
          f"{t_create+t_grid_build+t_query+t_reshape:.2f}s")
    print(f"  results identical: {match}", flush=True)
