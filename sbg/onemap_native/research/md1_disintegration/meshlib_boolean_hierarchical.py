"""Fix the sequential-fold O(n^2) anti-pattern: union in a binary tree
(pairwise merge, halving the count each round) instead of folding one piece
at a time into an ever-growing accumulator. Should give O(n log n)."""
import time
import numpy as np
import trimesh
import meshlib.mrmeshpy as mr
import meshlib.mrmeshnumpy as mn

out_dir = "/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583/scratchpad/md1_area"

def load_obj(path):
    v, f = [], []
    for line in open(path):
        if line.startswith("v "): v.append([float(x) for x in line.split()[1:4]])
        elif line.startswith("f "): f.append([int(x.split("/")[0])-1 for x in line.split()[1:4]])
    return np.array(v, dtype=np.float32), np.array(f, dtype=np.int32)

base_buildings = [
    load_obj(f"{out_dir}/00_tile9_0_batch0_dedup_oriented.obj"),
    load_obj(f"{out_dir}/nb00_17_0_b4.obj"),
]
tile_w, tile_h = 300.0, 300.0

def solidify(v, f, voxel_size, offset):
    mesh = mn.meshFromFacesVerts(f, v)
    params = mr.OffsetParameters()
    params.voxelSize = voxel_size
    return mr.offsetMesh(mr.MeshPart(mesh), offset, params)

def hierarchical_union(meshes):
    current = list(meshes)
    while len(current) > 1:
        nxt = []
        for i in range(0, len(current) - 1, 2):
            res = mr.boolean(current[i], current[i + 1], mr.BooleanOperation.Union)
            nxt.append(res.mesh if res.valid() else current[i])
        if len(current) % 2 == 1:
            nxt.append(current[-1])
        current = nxt
    return current[0]

for n in [1, 2, 3]:
    solids = []
    t0 = time.perf_counter()
    for i in range(n):
        for j in range(n):
            shift = np.array([i * tile_w, j * tile_h, 0], dtype=np.float32)
            for v, f in base_buildings:
                s = solidify((v + shift).astype(np.float32), f, voxel_size=2.0, offset=2.0)
                solids.append(s)
    t_solidify = time.perf_counter() - t0

    all_v = np.vstack([mn.getNumpyVerts(s) for s in solids])
    xmin, ymin = all_v[:,0].min()-20, all_v[:,1].min()-20
    xmax, ymax = all_v[:,0].max()+20, all_v[:,1].max()+20
    terrain_box = mr.makeCube(mr.Vector3f(xmax-xmin, ymax-ymin, 10.0), mr.Vector3f(xmin, ymin, -10.0))

    t0 = time.perf_counter()
    result = hierarchical_union([terrain_box] + solids)
    t_union = time.perf_counter() - t0

    n_buildings = len(solids)
    print(f"n={n} ({n_buildings} buildings): solidify={t_solidify:.2f}s union={t_union:.2f}s "
          f"total={t_solidify+t_union:.2f}s  final_faces={result.topology.numValidFaces():,}", flush=True)

    if n == 3:
        v_out = mn.getNumpyVerts(result)
        f_out = mn.getNumpyFaces(result.topology)
        m = trimesh.Trimesh(v_out, f_out, process=False)
        m.export(f"{out_dir}/meshlib_hierarchical_n3.stl")
