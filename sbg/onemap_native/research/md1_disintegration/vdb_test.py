import time
import numpy as np
import trimesh
import openvdb

out_dir = "/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583/scratchpad/md1_area"

def load_obj(path):
    v, f = [], []
    for line in open(path):
        if line.startswith("v "): v.append([float(x) for x in line.split()[1:4]])
        elif line.startswith("f "): f.append([int(x.split("/")[0])-1 for x in line.split()[1:4]])
    return np.array(v, dtype=np.float32), np.array(f, dtype=np.uint32)

v, f = load_obj(f"{out_dir}/00_tile9_0_batch0_dedup_oriented.obj")
print(f"input: {len(v)} verts, {len(f)} faces")

VOXEL_SIZE = 2.0  # match Blender's own voxel size for a fair comparison
xform = openvdb.createLinearTransform(voxelSize=VOXEL_SIZE)

for half_width in [3.0, 5.0, 8.0, 12.0]:
    t0 = time.perf_counter()
    grid = openvdb.FloatGrid.createLevelSetFromPolygons(v, triangles=f, transform=xform, halfWidth=half_width)
    t_build = time.perf_counter() - t0

    t0 = time.perf_counter()
    pts, tris, quads = grid.convertToPolygons(isovalue=0.0)
    t_extract = time.perf_counter() - t0

    # assemble a trimesh for volume/watertight check (quads -> triangulate if any)
    all_faces = list(tris)
    if len(quads):
        for q in quads:
            all_faces.append([q[0], q[1], q[2]])
            all_faces.append([q[0], q[2], q[3]])
    m = trimesh.Trimesh(pts, np.array(all_faces), process=False)
    m.export(f"{out_dir}/vdb_hw{int(half_width)}.stl")
    m2 = trimesh.load(f"{out_dir}/vdb_hw{int(half_width)}.stl", process=False)
    m2.merge_vertices(digits_vertex=4)

    print(f"halfWidth={half_width}: build={t_build:.2f}s extract={t_extract:.2f}s  "
          f"verts={len(pts)} tris={len(tris)} quads={len(quads)}  "
          f"bbox={m.bounds[1]-m.bounds[0]}  volume={abs(m.volume):.0f}  "
          f"watertight={m2.is_watertight}", flush=True)
