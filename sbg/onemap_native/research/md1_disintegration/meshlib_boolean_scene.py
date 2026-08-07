"""Solidify + exact boolean union on the REAL multi-building scene (MD1 +
neighbor + terrain), sequential unions building-by-building (the pattern
that would generalize to N buildings), to see if it holds up beyond the
single-building case."""
import time
import numpy as np
import trimesh
import meshlib.mrmeshpy as mr
import meshlib.mrmeshnumpy as mn

out_dir = "/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583/scratchpad/md1_area"
trace_dir = "/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583/scratchpad/trace"

def load_obj(path):
    v, f = [], []
    for line in open(path):
        if line.startswith("v "): v.append([float(x) for x in line.split()[1:4]])
        elif line.startswith("f "): f.append([int(x.split("/")[0])-1 for x in line.split()[1:4]])
    return np.array(v, dtype=np.float32), np.array(f, dtype=np.int32)

def solidify(v, f, voxel_size, offset):
    mesh = mn.meshFromFacesVerts(f, v)
    params = mr.OffsetParameters()
    params.voxelSize = voxel_size
    return mr.offsetMesh(mr.MeshPart(mesh), offset, params)

# MD1 + neighbor buildings
buildings = [
    f"{out_dir}/00_tile9_0_batch0_dedup_oriented.obj",  # MD1
]
import glob
nb = glob.glob(f"{out_dir}/nb00_17_0_b4.obj")
if nb:
    buildings.append(nb[0])

t0 = time.perf_counter()
solids = []
for path in buildings:
    v, f = load_obj(path)
    s = solidify(v, f, voxel_size=2.0, offset=2.0)
    solids.append(s)
t_solidify = time.perf_counter() - t0
print(f"solidified {len(solids)} buildings: {t_solidify:.2f}s total", flush=True)

# terrain slab spanning both
all_v = np.vstack([mn.getNumpyVerts(s) for s in solids])
xmin, ymin = all_v[:,0].min()-20, all_v[:,1].min()-20
xmax, ymax = all_v[:,0].max()+20, all_v[:,1].max()+20
terrain_box = mr.makeCube(mr.Vector3f(xmax-xmin, ymax-ymin, 10.0), mr.Vector3f(xmin, ymin, -10.0))
print(f"terrain slab: {xmax-xmin:.0f} x {ymax-ymin:.0f} m", flush=True)

t0 = time.perf_counter()
result = terrain_box
for i, s in enumerate(solids):
    res = mr.boolean(result, s, mr.BooleanOperation.Union)
    if not res.valid():
        print(f"  UNION {i} FAILED: {res.errorString}")
        break
    result = res.mesh
t_union = time.perf_counter() - t0
print(f"sequential union ({len(solids)} buildings + terrain): {t_union:.2f}s", flush=True)

v_out = mn.getNumpyVerts(result)
f_out = mn.getNumpyFaces(result.topology)
m = trimesh.Trimesh(v_out, f_out, process=False)
m.export(f"{out_dir}/meshlib_boolean_scene.stl")
m2 = trimesh.load(f"{out_dir}/meshlib_boolean_scene.stl", process=False)
m2.merge_vertices(digits_vertex=6)
comps = m2.split(only_watertight=False)
real = [c for c in comps if len(c.faces) > 100]
debris = [c for c in comps if len(c.faces) <= 100]
print(f"TOTAL: faces={len(f_out):,}  real_bodies={len(real)} vols={[round(abs(c.volume)) for c in real]}  "
      f"debris_specks={len(debris)}", flush=True)
