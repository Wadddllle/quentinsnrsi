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
mesh.fix_normals()

face_centroids = np.array(mesh.triangles_center)
face_normals = np.array(mesh.face_normals)

pcd = o3d.geometry.PointCloud()
pcd.points = o3d.utility.Vector3dVector(face_centroids)
pcd.normals = o3d.utility.Vector3dVector(face_normals)

depth = 9
o3d_mesh, densities = o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(pcd, depth=depth)
densities = np.asarray(densities)
print(f"depth={depth} raw: {len(o3d_mesh.vertices)} verts, density range [{densities.min():.3f},{densities.max():.3f}]")

for pct in [1, 5, 10, 20, 30]:
    thresh = np.quantile(densities, pct / 100)
    keep = densities >= thresh
    m2 = o3d.geometry.TriangleMesh(o3d_mesh)
    m2.remove_vertices_by_mask(~keep)
    verts = np.asarray(m2.vertices)
    faces = np.asarray(m2.triangles)
    if len(faces) == 0:
        print(f"  trim@{pct}%: EMPTY after trim")
        continue
    out = trimesh.Trimesh(verts, faces, process=False)
    bbox = out.bounds[1] - out.bounds[0]
    out.export(f"{out_dir}/poisson_trim{pct}.stl")
    out2 = trimesh.load(f"{out_dir}/poisson_trim{pct}.stl", process=False)
    out2.merge_vertices(digits_vertex=4)
    print(f"  trim@{pct}%: {len(verts)} verts {len(faces)} faces bbox={bbox} "
          f"volume={abs(out.volume):.1f} watertight={out2.is_watertight}")
