"""Push morphological closing to the REAL scale that matters: the whole
joined scene (terrain + both buildings + skirts), not one isolated building.
Same technique as morph_closing.py, just fed the real stage2_joined_soup."""
import time
import numpy as np
import trimesh
from scipy import ndimage
from skimage import measure

trace_dir = "/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583/scratchpad/trace"
out_dir = "/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583/scratchpad/md1_area"

import sys
mesh = trimesh.load(f"{trace_dir}/stage2_joined_soup.stl", process=False)
print(f"input: {len(mesh.vertices)} verts, {len(mesh.faces)} faces, extent={mesh.bounds[1]-mesh.bounds[0]}", flush=True)

PITCH = 2.0
CLOSE_RADIUS_VOX = 2

t0 = time.perf_counter()
vox = mesh.voxelized(pitch=PITCH)
occ = vox.matrix
sys.stdout.flush(); print(f"voxelize: {time.perf_counter()-t0:.1f}s, grid shape={occ.shape}, occupied={occ.sum()}")

t0 = time.perf_counter()
struct = ndimage.generate_binary_structure(3, 1)
dilated = ndimage.binary_dilation(occ, structure=struct, iterations=CLOSE_RADIUS_VOX)
solid = ndimage.binary_fill_holes(dilated)
closed = ndimage.binary_erosion(solid, structure=struct, iterations=CLOSE_RADIUS_VOX)
sys.stdout.flush(); print(f"morph ops: {time.perf_counter()-t0:.1f}s, final voxels={closed.sum()} "
      f"(dilated={dilated.sum()}, filled={solid.sum()})")

t0 = time.perf_counter()
verts, faces, normals, values = measure.marching_cubes(closed.astype(np.float32), level=0.5)
verts_world = trimesh.transform_points(verts, vox.transform)
out_mesh = trimesh.Trimesh(verts_world, faces, process=False)
sys.stdout.flush(); print(f"marching_cubes: {time.perf_counter()-t0:.1f}s, {len(verts_world)} verts, {len(faces)} faces")

out_mesh.export(f"{out_dir}/morph_closed_SCENE.stl")

check = trimesh.load(f"{out_dir}/morph_closed_SCENE.stl", process=False)
check.merge_vertices(digits_vertex=4)
print(f"watertight(merged): {check.is_watertight}, volume={abs(check.volume):.0f}, "
      f"bodies={check.body_count}")
