import numpy as np
import trimesh
from scipy import ndimage

out_dir = "/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583/scratchpad/md1_area"

def load_obj(path):
    v, f = [], []
    for line in open(path):
        if line.startswith("v "): v.append([float(x) for x in line.split()[1:4]])
        elif line.startswith("f "): f.append([int(x.split("/")[0])-1 for x in line.split()[1:4]])
    return np.array(v), np.array(f)

v, f = load_obj(f"{out_dir}/00_tile9_0_batch0_dedup_oriented.obj")
mesh = trimesh.Trimesh(v, f, process=False)
vox = mesh.voxelized(pitch=1.0)
occ = vox.matrix

def close(occ, struct, iters):
    dilated = ndimage.binary_dilation(occ, structure=struct, iterations=iters)
    solid = ndimage.binary_fill_holes(dilated)
    return ndimage.binary_erosion(solid, structure=struct, iterations=iters)

iso = ndimage.generate_binary_structure(3, 1)  # 6-connectivity cross, isotropic

# XY-biased (advice's suggestion): dilate horizontally, minimal Z
xy_biased = np.zeros((3,3,3), dtype=bool)
xy_biased[1,1,:] = True   # full X extent at center Y,Z... wait build properly below
xy_biased = np.array([
    [[0,0,0],[0,1,0],[0,0,0]],
    [[0,1,0],[1,1,1],[0,1,0]],
    [[0,0,0],[0,1,0],[0,0,0]],
], dtype=bool)  # this is just the standard 6-connectivity cross (same as iso!) -- the
                 # pasted snippet's "anisotropic" example is actually isotropic 6-conn.
                 # A REAL anisotropic bias needs unequal ITERATION COUNTS per axis instead.

for name, radius in [("isotropic_r2", 2)]:
    c = close(occ, iso, radius)
    print(f"{name}: {c.sum()} voxels")

# Real anisotropy: dilate along Z more than XY (or vice versa) by doing separate
# 1D dilations with different iteration counts per axis, combined.
def close_aniso(occ, xy_iters, z_iters):
    d = occ.copy()
    # horizontal (X,Y) dilation
    struct_xy = np.zeros((3,3,1), dtype=bool); struct_xy[:,:,0] = [[0,1,0],[1,1,1],[0,1,0]]
    if xy_iters:
        d = ndimage.binary_dilation(d, structure=struct_xy, iterations=xy_iters)
    struct_z = np.zeros((1,1,3), dtype=bool); struct_z[0,0,:] = [1,1,1]
    if z_iters:
        d = ndimage.binary_dilation(d, structure=struct_z, iterations=z_iters)
    solid = ndimage.binary_fill_holes(d)
    e = solid.copy()
    if z_iters:
        e = ndimage.binary_erosion(e, structure=struct_z, iterations=z_iters)
    if xy_iters:
        e = ndimage.binary_erosion(e, structure=struct_xy, iterations=xy_iters)
    return e

for name, xy_i, z_i in [
    ("XY-biased (advice's direction: bridge XY gaps)", 4, 1),
    ("Z-biased (opposite: bridge thin-deck Z gaps)", 1, 4),
    ("balanced", 2, 2),
]:
    c = close_aniso(occ, xy_i, z_i)
    print(f"{name}: xy_iters={xy_i} z_iters={z_i} -> {c.sum()} voxels")
