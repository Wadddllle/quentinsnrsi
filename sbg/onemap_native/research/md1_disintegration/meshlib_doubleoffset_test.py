"""Test meshlib's native doubleOffsetVdb (dilate then erode via VDB, with
generalized winding number for non-closed meshes) on MD1's mesh AND the
whole real scene. This is meshlib's OWN closing operation -- already
installed, already in production for the domain clip -- potentially the
real answer to tonight's whole search."""
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

def run_test(name, v, f, voxel_size, offset_a, offset_b):
    mesh = mn.meshFromFacesVerts(f, v)
    t0 = time.perf_counter()
    settings = mr.DoubleOffsetSettings()
    settings.voxelSize = voxel_size
    settings.offsetA = offset_a
    settings.offsetB = offset_b
    result = mr.doubleOffsetVdb(mr.MeshPart(mesh), settings)
    t_elapsed = time.perf_counter() - t0

    out_v = mn.getNumpyVerts(result)
    out_f = mn.getNumpyFaces(result.topology)
    out_mesh = trimesh.Trimesh(out_v, out_f, process=False)
    fn = f"{out_dir}/meshlib_{name}.stl"
    out_mesh.export(fn)
    check = trimesh.load(fn, process=False)
    check.merge_vertices(digits_vertex=4)
    print(f"[{name}] voxel={voxel_size} offsetA={offset_a} offsetB={offset_b}: "
          f"{t_elapsed:.2f}s  verts={len(out_v)} faces={len(out_f)}  "
          f"bbox={out_mesh.bounds[1]-out_mesh.bounds[0]}  volume={abs(out_mesh.volume):.0f}  "
          f"watertight={check.is_watertight} bodies={check.body_count}", flush=True)

# Test 1: MD1 alone, raw (non-closed) mesh -- does GWN handle the open base?
v, f = load_obj(f"{out_dir}/00_tile9_0_batch0_dedup_oriented.obj")
print(f"MD1 alone: {len(v)} verts {len(f)} faces (NOT closed -- 3 open base rims)", flush=True)
run_test("md1_close_2m", v, f, voxel_size=2.0, offset_a=3.0, offset_b=-3.0)
run_test("md1_close_1m", v, f, voxel_size=1.0, offset_a=3.0, offset_b=-3.0)

# Test 2: the whole real scene (terrain+skirts+both buildings, IS closed)
m = trimesh.load(f"{trace_dir}/stage2_joined_soup.stl", process=False)
v2, f2 = np.array(m.vertices, dtype=np.float32), np.array(m.faces, dtype=np.int32)
print(f"\nfull scene: {len(v2)} verts {len(f2)} faces", flush=True)
run_test("scene_close_2m", v2, f2, voxel_size=2.0, offset_a=3.0, offset_b=-3.0)
