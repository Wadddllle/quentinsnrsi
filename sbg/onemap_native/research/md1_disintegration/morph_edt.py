"""Test 1: EDT-based closing vs iterative binary_dilation, for speed AND
correctness-equivalence -- claim was iterative dilation is slow at larger
radius because it recomputes the neighborhood every iteration; EDT does it
in one pass regardless of radius."""
import time
import numpy as np
import trimesh
from scipy import ndimage
from skimage import measure

out_dir = "/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583/scratchpad/md1_area"

def load_obj(path):
    v, f = [], []
    for line in open(path):
        if line.startswith("v "): v.append([float(x) for x in line.split()[1:4]])
        elif line.startswith("f "): f.append([int(x.split("/")[0])-1 for x in line.split()[1:4]])
    return np.array(v), np.array(f)

v, f = load_obj(f"{out_dir}/00_tile9_0_batch0_dedup_oriented.obj")
mesh = trimesh.Trimesh(v, f, process=False)

PITCH = 1.0
vox = mesh.voxelized(pitch=PITCH)
occ = vox.matrix
print(f"grid shape={occ.shape}, occupied={occ.sum()}")

def close_iterative(occ, radius_vox):
    struct = ndimage.generate_binary_structure(3, 1)
    t0 = time.perf_counter()
    dilated = ndimage.binary_dilation(occ, structure=struct, iterations=radius_vox)
    solid = ndimage.binary_fill_holes(dilated)
    closed = ndimage.binary_erosion(solid, structure=struct, iterations=radius_vox)
    return closed, time.perf_counter() - t0

def close_edt(occ, radius_vox):
    t0 = time.perf_counter()
    dist_out = ndimage.distance_transform_edt(~occ)
    dilated = dist_out <= radius_vox
    solid = ndimage.binary_fill_holes(dilated)
    dist_in = ndimage.distance_transform_edt(solid)
    closed = solid & (dist_in > radius_vox)
    return closed, time.perf_counter() - t0

for radius in [2, 5, 10]:
    c_iter, t_iter = close_iterative(occ, radius)
    c_edt, t_edt = close_edt(occ, radius)
    same = np.array_equal(c_iter, c_edt)
    print(f"radius={radius}vox: iterative={t_iter:.3f}s ({c_iter.sum()} vox), "
          f"EDT={t_edt:.3f}s ({c_edt.sum()} vox), IDENTICAL_RESULT={same}, "
          f"speedup={t_iter/max(t_edt,1e-6):.1f}x")
