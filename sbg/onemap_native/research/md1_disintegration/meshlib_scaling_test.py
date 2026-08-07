"""Controlled scaling test: tile the real scene mesh into NxN copies (offset
in XY, same real geometry/thin-bridge characteristics repeated) to get
genuinely bigger bbox + surface area, and time doubleOffsetVdb at each scale.
If it's truly sparse/surface-bound (like real OpenVDB should be), time should
scale roughly linearly with N^2 (face count). If it's secretly bbox-volume-
bound, time would blow up much faster (~N^2 in XY only if voxel depth stays
fixed, but worse if it also grows) -- this distinguishes the two."""
import time
import numpy as np
import trimesh
import meshlib.mrmeshpy as mr
import meshlib.mrmeshnumpy as mn

trace_dir = "/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583/scratchpad/trace"
mesh = trimesh.load(f"{trace_dir}/stage2_joined_soup.stl", process=False)
base_v = np.array(mesh.vertices, dtype=np.float32)
base_f = np.array(mesh.faces, dtype=np.int32)
extent = mesh.bounds[1] - mesh.bounds[0]
print(f"base scene: {len(base_f)} faces, extent={extent}", flush=True)

for n in [1, 2, 3]:
    all_v = []
    all_f = []
    offset = 0
    for i in range(n):
        for j in range(n):
            shift = np.array([i * extent[0] * 1.05, j * extent[1] * 1.05, 0], dtype=np.float32)
            all_v.append(base_v + shift)
            all_f.append(base_f + offset)
            offset += len(base_v)
    v = np.vstack(all_v)
    f = np.vstack(all_f)
    tiled_mesh = mn.meshFromFacesVerts(f, v)
    bbox_extent = v.max(axis=0) - v.min(axis=0)

    t0 = time.perf_counter()
    settings = mr.DoubleOffsetSettings()
    settings.voxelSize = 2.0
    settings.offsetA = 3.0
    settings.offsetB = -3.0
    result = mr.doubleOffsetVdb(mr.MeshPart(tiled_mesh), settings)
    t_elapsed = time.perf_counter() - t0

    print(f"n={n} ({n*n} tiles): faces_in={len(f):,} bbox={bbox_extent}  "
          f"time={t_elapsed:.2f}s  faces_out={result.topology.numValidFaces():,}", flush=True)
