"""Test: give every part of MD1's mesh that doesn't touch the ground its own
synthetic vertical support strut, straight down to base_z, instead of relying
on the near-base footprint clustering (which misses elevated spans entirely --
confirmed: the bridge region has only 6 near-base verts, all one point).

Grid the piece's full XY extent into cells; for each occupied cell, find the
LOCAL minimum z. If that local min is well above the piece's true base_z
(i.e. this part of the building never comes near the ground), drop a thin
vertical strut from base_z up to that local min -- a synthetic support column
directly under wherever the real structure actually is, regardless of whether
it belongs to a recognized footprint cluster."""
import sys
sys.path.insert(0, "/home/quentin/snrsi")
import numpy as np

out_dir = "/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583/scratchpad/md1_area"

def load_obj(path):
    v, f = [], []
    for line in open(path):
        if line.startswith("v "): v.append([float(x) for x in line.split()[1:4]])
        elif line.startswith("f "): f.append([int(x.split("/")[0])-1 for x in line.split()[1:4]])
    return np.array(v), np.array(f)

def write_obj(path, v, f):
    with open(path, "w") as fh:
        for p in v: fh.write(f"v {p[0]:.4f} {p[1]:.4f} {p[2]:.4f}\n")
        for tri in f: fh.write(f"f {tri[0]+1} {tri[1]+1} {tri[2]+1}\n")

v, f = load_obj(f"{out_dir}/00_tile9_0_batch0_dedup_oriented.obj")
base_z = v[:, 2].min()
CELL = 4.0       # m, grid cell size for local-min sampling
ELEV_THRESH = 5.0  # m above base_z -> considered "unsupported", needs a strut
STRUT_HALF = 0.5   # m, half-width of each synthetic strut column

xi = np.floor((v[:, 0] - v[:, 0].min()) / CELL).astype(int)
yi = np.floor((v[:, 1] - v[:, 1].min()) / CELL).astype(int)
cells = {}
for i in range(len(v)):
    key = (xi[i], yi[i])
    cells.setdefault(key, []).append(i)

struts = []  # (cx, cy, local_min_z)
for key, idxs in cells.items():
    zmin = v[idxs, 2].min()
    if zmin > base_z + ELEV_THRESH:
        pts = v[idxs, :2]
        cx, cy = pts.mean(axis=0)
        struts.append((cx, cy, zmin))

print(f"piece base_z={base_z:.2f}, {len(cells)} occupied cells, {len(struts)} need a synthetic strut")

# build strut geometry: simple square-prism columns from base_z to local_min_z
extra_v = []
extra_f = []
vcount = len(v)
for cx, cy, ztop in struts:
    corners_bottom = [
        (cx - STRUT_HALF, cy - STRUT_HALF, base_z),
        (cx + STRUT_HALF, cy - STRUT_HALF, base_z),
        (cx + STRUT_HALF, cy + STRUT_HALF, base_z),
        (cx - STRUT_HALF, cy + STRUT_HALF, base_z),
    ]
    corners_top = [(x, y, ztop) for x, y, _ in corners_bottom]
    base_idx = vcount + len(extra_v)
    extra_v.extend(corners_bottom)
    extra_v.extend(corners_top)
    # 4 side walls (2 tris each), no caps needed (mesh will be joined/welded anyway)
    for i in range(4):
        j = (i + 1) % 4
        b0, b1 = base_idx + i, base_idx + j
        t0, t1 = base_idx + 4 + i, base_idx + 4 + j
        extra_f.append([b0, b1, t1])
        extra_f.append([b0, t1, t0])

v_out = np.vstack([v, np.array(extra_v)])
f_out = np.vstack([f, np.array(extra_f)])
write_obj(f"{out_dir}/00_with_struts.obj", v_out, f_out)
print(f"wrote 00_with_struts.obj: {len(v_out)} verts, {len(f_out)} faces ({len(struts)} struts added)")
