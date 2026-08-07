"""The literal thing asked for: solidify each piece (meshlib offsetMesh, VDB-
based single offset, guaranteed closed regardless of input openness) then
EXACT boolean union (meshlib's real CSG, not a voxel remesh) -- testing
whether meshlib's boolean fares better than Blender's, which historically
fragmented terrain into 77+ pieces on just 27 buildings."""
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
    t0 = time.perf_counter()
    params = mr.OffsetParameters()
    params.voxelSize = voxel_size
    result = mr.offsetMesh(mr.MeshPart(mesh), offset, params)
    t = time.perf_counter() - t0
    return result, t

def check(mesh, name):
    v = mn.getNumpyVerts(mesh)
    f = mn.getNumpyFaces(mesh.topology)
    m = trimesh.Trimesh(v, f, process=False)
    fn = f"{out_dir}/{name}.stl"
    m.export(fn)
    m2 = trimesh.load(fn, process=False)
    m2.merge_vertices(digits_vertex=6)
    comps = m2.split(only_watertight=False)
    real = [c for c in comps if len(c.faces) > 100]
    debris = [c for c in comps if len(c.faces) <= 100]
    print(f"  [{name}] faces={len(f):,} bbox={m.bounds[1]-m.bounds[0]} "
          f"real_bodies={len(real)} (vols={[round(abs(c.volume)) for c in real]}) "
          f"debris_specks={len(debris)}")
    return m

# --- Step 1: solidify MD1 ---
v, f = load_obj(f"{out_dir}/00_tile9_0_batch0_dedup_oriented.obj")
print(f"MD1: {len(v)} verts {len(f)} faces (open, 3 base rims)")
bld_solid, t_bld = solidify(v, f, voxel_size=2.0, offset=2.0)
print(f"solidify MD1: {t_bld:.2f}s")
check(bld_solid, "meshlib_bld_solid")

# --- Step 2: build + solidify a terrain slab under it ---
bld_v_np = mn.getNumpyVerts(bld_solid)
xmin, ymin = bld_v_np[:,0].min()-20, bld_v_np[:,1].min()-20
xmax, ymax = bld_v_np[:,0].max()+20, bld_v_np[:,1].max()+20
terrain_box = mr.makeCube(mr.Vector3f(xmax-xmin, ymax-ymin, 10.0), mr.Vector3f(xmin, ymin, -10.0))
print(f"terrain slab: box {xmin:.0f}-{xmax:.0f}, {ymin:.0f}-{ymax:.0f}")

# --- Step 3: EXACT boolean union ---
t0 = time.perf_counter()
res = mr.boolean(bld_solid, terrain_box, mr.BooleanOperation.Union)
t_union = time.perf_counter() - t0
print(f"exact boolean union: {t_union:.2f}s, valid={res.valid()}")
if res.valid():
    check(res.mesh, "meshlib_boolean_union")
else:
    print(f"  ERROR: {res.errorString}")
