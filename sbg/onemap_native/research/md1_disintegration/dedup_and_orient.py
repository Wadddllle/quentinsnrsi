"""Weld + dedupe the double-sided MD1 mesh down to ONE triangle per real face,
then propagate a globally consistent winding via BFS across shared edges
(the standard "orient mesh" algorithm) -- something the earlier
normals_make_consistent attempt couldn't do because the input was unwelded
soup with no shared edges to propagate across. This gives a clean, single-
layer, consistently-oriented mesh to test against the raw double-sided one."""
import sys, os
sys.path.insert(0, "/home/quentin/snrsi")
import numpy as np
from collections import defaultdict, deque

out_dir = "/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583/scratchpad/md1_area"

def load_obj(path):
    v, f = [], []
    for line in open(path):
        if line.startswith("v "):
            v.append([float(x) for x in line.split()[1:4]])
        elif line.startswith("f "):
            f.append([int(x.split("/")[0]) - 1 for x in line.split()[1:4]])
    return np.array(v), np.array(f)

def write_obj(path, v, f):
    with open(path, "w") as fh:
        for p in v:
            fh.write(f"v {p[0]:.4f} {p[1]:.4f} {p[2]:.4f}\n")
        for tri in f:
            fh.write(f"f {tri[0]+1} {tri[1]+1} {tri[2]+1}\n")

WELD = 3
def weld(v, f):
    key = np.round(v, WELD)
    _, inv = np.unique(key, axis=0, return_inverse=True)
    inv = inv.reshape(-1)
    v_w = np.zeros((inv.max() + 1, 3))
    v_w[inv] = v
    f_w = inv[f]
    keep = (f_w[:,0] != f_w[:,1]) & (f_w[:,1] != f_w[:,2]) & (f_w[:,0] != f_w[:,2])
    return v_w, f_w[keep]

def dedup_faces(f):
    """Keep exactly one triangle per unique vertex-set (drops the mirrored
    opposite-winding duplicate)."""
    seen = {}
    out = []
    for tri in f:
        key = frozenset(tri.tolist())
        if key not in seen:
            seen[key] = True
            out.append(tri)
    return np.array(out)

def orient_consistently(f):
    """BFS-based consistent orientation: build face adjacency via shared
    undirected edges, then flip a face if its shared edge with an already-
    oriented neighbor is traversed in the SAME direction (should be opposite
    for consistent winding)."""
    n = len(f)
    edge_to_faces = defaultdict(list)
    for i, tri in enumerate(f):
        for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
            key = (a, b) if a < b else (b, a)
            edge_to_faces[key].append((i, a, b))

    f = f.copy()
    visited = np.zeros(n, dtype=bool)
    flips = 0
    for start in range(n):
        if visited[start]:
            continue
        visited[start] = True
        q = deque([start])
        while q:
            i = q.popleft()
            tri = f[i]
            for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
                key = (a, b) if a < b else (b, a)
                for (j, ja, jb) in edge_to_faces[key]:
                    if j == i or visited[j]:
                        continue
                    visited[j] = True
                    # consistent orientation: neighbor should traverse this
                    # shared edge in the OPPOSITE direction (b, a) not (a, b)
                    if (ja, jb) == (a, b):
                        f[j] = f[j][::-1]
                        flips += 1
                    q.append(j)
    print(f"  orient_consistently: flipped {flips}/{n} faces")
    return f

for fn in ["00_tile9_0_batch0.obj", "03_tile18_0_batch2.obj"]:
    v, f = load_obj(os.path.join(out_dir, fn))
    v_w, f_w = weld(v, f)
    print(f"{fn}: welded {len(v_w)} verts, {len(f_w)} faces")
    f_dedup = dedup_faces(f_w)
    print(f"  deduped to {len(f_dedup)} faces (was {len(f_w)})")
    f_oriented = orient_consistently(f_dedup)
    base = fn.replace(".obj", "")
    write_obj(os.path.join(out_dir, f"{base}_weldonly.obj"), v_w, f_w)
    write_obj(os.path.join(out_dir, f"{base}_dedup_oriented.obj"), v_w, f_oriented)
    print(f"  wrote {base}_weldonly.obj, {base}_dedup_oriented.obj")
