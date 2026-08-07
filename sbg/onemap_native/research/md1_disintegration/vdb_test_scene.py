import time
import numpy as np
import trimesh
import openvdb

trace_dir = "/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583/scratchpad/trace"
out_dir = "/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583/scratchpad/md1_area"

mesh = trimesh.load(f"{trace_dir}/stage2_joined_soup.stl", process=False)
v = np.array(mesh.vertices, dtype=np.float32)
f = np.array(mesh.faces, dtype=np.uint32)
print(f"input (real scene, terrain+skirts+both buildings): {len(v)} verts, {len(f)} faces", flush=True)

VOXEL_SIZE = 2.0
xform = openvdb.createLinearTransform(voxelSize=VOXEL_SIZE)

for half_width in [3.0, 8.0]:
    t0 = time.perf_counter()
    grid = openvdb.FloatGrid.createLevelSetFromPolygons(v, triangles=f, transform=xform, halfWidth=half_width)
    t_build = time.perf_counter() - t0

    t0 = time.perf_counter()
    pts, tris, quads = grid.convertToPolygons(isovalue=0.0)
    t_extract = time.perf_counter() - t0

    all_faces = list(tris)
    if len(quads):
        for q in quads:
            all_faces.append([q[0], q[1], q[2]])
            all_faces.append([q[0], q[2], q[3]])
    m = trimesh.Trimesh(pts, np.array(all_faces), process=False)
    m.export(f"{out_dir}/vdb_scene_hw{int(half_width)}.stl")
    m2 = trimesh.load(f"{out_dir}/vdb_scene_hw{int(half_width)}.stl", process=False)
    m2.merge_vertices(digits_vertex=4)

    print(f"halfWidth={half_width}: build={t_build:.2f}s extract={t_extract:.2f}s  "
          f"verts={len(pts)} tris={len(tris)} quads={len(quads)}  "
          f"volume={abs(m.volume):.0f}  watertight={m2.is_watertight} bodies={m2.body_count}", flush=True)
