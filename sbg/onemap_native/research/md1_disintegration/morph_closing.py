"""Approach 3: morphological surface voxelization. Surface-voxelize MD1's
mesh, binary-dilate to bridge small gaps (< 2*radius), fill the enclosed
interior, then binary-erode back by the SAME amount ("closing") -- unlike
Solidify, closing only changes geometry where a real gap got bridged; a
feature that was already thick enough is untouched by the round trip.
Extract the result via marching cubes and compare to solidify/ground truth."""
import sys
sys.path.insert(0, "/home/quentin/snrsi")
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

PITCH = 1.0          # voxel size for the closing grid (finer than the 2m Blender voxel,
                      # so we can resolve+repair sub-2m features before matching the final res)
CLOSE_RADIUS_VOX = 2  # dilate/erode by 2 voxels = close gaps up to ~2*PITCH = 2m

print(f"surface-voxelizing at pitch={PITCH}m...")
vox = mesh.voxelized(pitch=PITCH)
occ = vox.matrix.copy()
print(f"grid shape={occ.shape}, occupied (surface) voxels={occ.sum()}")

# "closing" = dilate then erode by the same structuring element
struct = ndimage.generate_binary_structure(3, 1)  # 6-connectivity
dilated = ndimage.binary_dilation(occ, structure=struct, iterations=CLOSE_RADIUS_VOX)
print(f"after dilate({CLOSE_RADIUS_VOX}): {dilated.sum()} voxels")

# fill enclosed interior (flood-fill from outside the padded volume, invert)
padded = np.pad(dilated, 1, mode="constant", constant_values=False)
filled_outside = ndimage.binary_fill_holes(~padded)  # True = outside+shell after fill_holes trick
# simpler: binary_fill_holes fills holes in the True region directly
solid = ndimage.binary_fill_holes(dilated)
print(f"after fill_holes: {solid.sum()} voxels (vs {dilated.sum()} shell-only)")

closed = ndimage.binary_erosion(solid, structure=struct, iterations=CLOSE_RADIUS_VOX)
print(f"after erode({CLOSE_RADIUS_VOX}) [final closed volume]: {closed.sum()} voxels")

# extract surface via marching cubes
verts, faces, normals, values = measure.marching_cubes(closed.astype(np.float32), level=0.5)
# marching_cubes verts are in voxel-index space; map back to world coords via vox transform
verts_world = trimesh.transform_points(verts, vox.transform)
out_mesh = trimesh.Trimesh(verts_world, faces, process=False)
out_mesh.export(f"{out_dir}/morph_closed.stl")
print(f"exported morph_closed.stl: {len(out_mesh.vertices)} verts, {len(out_mesh.faces)} faces, "
      f"volume={out_mesh.volume:.1f}, watertight={out_mesh.is_watertight}")
print(f"bbox: {out_mesh.bounds[1]-out_mesh.bounds[0]}")
