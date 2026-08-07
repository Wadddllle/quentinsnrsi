import sys, time
import numpy as np
import trimesh
import meshlib.mrmeshpy as mr
import meshlib.mrmeshnumpy as mn

n = int(sys.argv[1])
trace_dir = "/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583/scratchpad/trace"
mesh = trimesh.load(f"{trace_dir}/stage2_joined_soup.stl", process=False)
base_v = np.array(mesh.vertices, dtype=np.float32)
base_f = np.array(mesh.faces, dtype=np.int32)
extent = mesh.bounds[1] - mesh.bounds[0]

all_v, all_f, offset = [], [], 0
for i in range(n):
    for j in range(n):
        shift = np.array([i * extent[0] * 1.05, j * extent[1] * 1.05, 0], dtype=np.float32)
        all_v.append(base_v + shift)
        all_f.append(base_f + offset)
        offset += len(base_v)
v = np.vstack(all_v)
f = np.vstack(all_f)
tiled_mesh = mn.meshFromFacesVerts(f, v)
print(f"n={n}: faces_in={len(f):,}", flush=True)

t0 = time.perf_counter()
settings = mr.DoubleOffsetSettings()
settings.voxelSize = 2.0
settings.offsetA = 3.0
settings.offsetB = -3.0
result = mr.doubleOffsetVdb(mr.MeshPart(tiled_mesh), settings)
t_elapsed = time.perf_counter() - t0
print(f"n={n}: time={t_elapsed:.2f}s faces_out={result.topology.numValidFaces():,}", flush=True)
