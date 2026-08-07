"""Approach 2: Poisson surface reconstruction. Feed MD1's (welded, deduped,
consistently-oriented) mesh's vertices + real face normals to Open3D's Poisson
reconstructor -- it solves for an implicit indicator function and extracts a
smooth, closed, watertight surface directly, without any manual thickening
step at all."""
import sys
sys.path.insert(0, "/home/quentin/snrsi")
import numpy as np
import open3d as o3d
import trimesh

out_dir = "/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583/scratchpad/md1_area"

def load_obj(path):
    v, f = [], []
    for line in open(path):
        if line.startswith("v "): v.append([float(x) for x in line.split()[1:4]])
        elif line.startswith("f "): f.append([int(x.split("/")[0])-1 for x in line.split()[1:4]])
    return np.array(v), np.array(f)

v, f = load_obj(f"{out_dir}/00_tile9_0_batch0_dedup_oriented.obj")
mesh = trimesh.Trimesh(v, f, process=False)
mesh.fix_normals()  # trimesh's own consistency pass, belt-and-suspenders

# Sample points + normals densely from the mesh surface (Poisson wants a point
# cloud with per-point normals, not raw mesh faces) -- use face centroids +
# face normals directly since we already have real geometry, no need to
# re-sample randomly.
face_centroids = np.array(mesh.triangles_center)
face_normals = np.array(mesh.face_normals)

pcd = o3d.geometry.PointCloud()
pcd.points = o3d.utility.Vector3dVector(face_centroids)
pcd.normals = o3d.utility.Vector3dVector(face_normals)

print(f"input: {len(face_centroids)} oriented points (one per face)")

for depth in [8, 9, 10]:
    o3d_mesh, densities = o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(
        pcd, depth=depth)
    verts = np.asarray(o3d_mesh.vertices)
    faces = np.asarray(o3d_mesh.triangles)
    out = trimesh.Trimesh(verts, faces, process=False)
    out.export(f"{out_dir}/poisson_depth{depth}.stl")
    out2 = trimesh.load(f"{out_dir}/poisson_depth{depth}.stl", process=False)
    out2.merge_vertices(digits_vertex=4)
    print(f"depth={depth}: {len(verts)} verts, {len(faces)} faces, "
          f"bbox={out.bounds[1]-out.bounds[0]}, volume={abs(out.volume):.1f}, "
          f"watertight(merged)={out2.is_watertight}")
