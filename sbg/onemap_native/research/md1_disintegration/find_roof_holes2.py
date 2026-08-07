"""Same as find_roof_holes.py but WELDS vertices by rounded position first --
these OneMap meshes are unwelded triangle soup (documented elsewhere in this
project), so raw-vertex-index boundary-edge detection is meaningless (every
edge looks single-face even when two triangles touch in real 3D space)."""
import sys
sys.path.insert(0, "/home/quentin/snrsi")
import numpy as np
from collections import defaultdict
from pyproj import Transformer
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from sbg.onemap_native.tiles import domain_leaf_tiles, fetch_tile, feature_table, load_gltf
from sbg.onemap_native.extract import _draco_prims, FLAT_PLATE_Z
from sbg.onemap_native.transform import local_to_svy21

svy_to_wgs = Transformer.from_crs("EPSG:3414", "EPSG:4326", always_xy=True)
xmin, ymin, xmax, ymax = 22000, 30550, 22280, 30820
lon_min, lat_min = svy_to_wgs.transform(xmin, ymin)
lon_max, lat_max = svy_to_wgs.transform(xmax, ymax)
leaves = domain_leaf_tiles(lon_min, lat_min, lon_max, lat_max)

WELD = 3  # decimals (~1mm)

def weld(v, f):
    key = np.round(v, WELD)
    _, inv, counts = np.unique(key, axis=0, return_inverse=True, return_counts=True)
    inv = inv.reshape(-1)
    v_w = np.zeros((inv.max() + 1, 3))
    v_w[inv] = v  # last-write wins, fine (positions identical within weld tol)
    f_w = inv[f]
    # drop degenerate faces (collapsed to <3 unique verts)
    keep = (f_w[:,0] != f_w[:,1]) & (f_w[:,1] != f_w[:,2]) & (f_w[:,0] != f_w[:,2])
    return v_w, f_w[keep]

def boundary_loops(v, f):
    edges = defaultdict(int)
    for tri in f:
        for a, b in ((tri[0], tri[1]), (tri[1], tri[2]), (tri[2], tri[0])):
            key = (a, b) if a < b else (b, a)
            edges[key] += 1
    bedges = [e for e, c in edges.items() if c == 1]
    if not bedges:
        return []
    verts_used = sorted(set(i for e in bedges for i in e))
    vid = {v_: i for i, v_ in enumerate(verts_used)}
    rows = [vid[a] for a, b in bedges]
    cols = [vid[b] for a, b in bedges]
    n = len(verts_used)
    m = coo_matrix((np.ones(len(rows)), (rows, cols)), shape=(n, n))
    ncomp, labels = connected_components(m, directed=False)
    loops = []
    for c in range(ncomp):
        idx = [verts_used[i] for i in range(n) if labels[i] == c]
        pts = v[idx]
        loops.append((len(idx), pts[:,2].min(), pts[:,2].max(), pts[:,:2].mean(axis=0)))
    return loops

results = []
for uri in leaves:
    try:
        data = fetch_tile(uri)
        ft = feature_table(data)
        rtc = ft.get("RTC_CENTER")
        if rtc is None:
            continue
        rtc = np.array(rtc)
        gltf, blob = load_gltf(data)
    except Exception as e:
        print("skip", uri, e); continue
    for pts, faces, bid, T, R in _draco_prims(gltf, blob):
        svy = local_to_svy21(pts, T, R, rtc)
        for c in np.unique(bid):
            fmask = np.all(bid[faces] == c, axis=1)
            if not np.any(fmask):
                continue
            sub_faces = faces[fmask]
            used = np.unique(sub_faces)
            v = svy[used]
            if np.ptp(v[:, 2]) < FLAT_PLATE_Z:
                continue
            remap = np.zeros(len(svy), dtype=np.int64)
            remap[used] = np.arange(len(used))
            f_local = remap[sub_faces]
            base_z = v[:, 2].min()
            top_z = v[:, 2].max()

            v_w, f_w = weld(v, f_local)
            loops = boundary_loops(v_w, f_w)
            # A real "hole" candidate: a loop with >=4 verts (not a lone sliver
            # triangle edge) whose z-range doesn't touch the base.
            suspicious = [l for l in loops if l[0] >= 4 and l[1] > base_z + 1.5]
            n_small = sum(1 for l in loops if l[0] < 4)
            if suspicious:
                results.append((uri, c, base_z, top_z, len(v_w), len(f_w), loops, suspicious, n_small))

print(f"\n{len(results)} pieces with a real (>=4-vertex) boundary loop NOT at the base:\n")
for uri, c, base_z, top_z, nv, nf, loops, susp, n_small in results:
    print(f"tile={uri.split('/')[-1]} batch={c} base_z={base_z:.1f} top_z={top_z:.1f} "
          f"nverts={nv} nfaces={nf} n_loops={len(loops)} (n_tiny_slivers={n_small})")
    for n_edges, zmin, zmax, cen in sorted(susp, key=lambda l: -l[0]):
        print(f"    HOLE candidate: {n_edges} verts, z=[{zmin:.1f},{zmax:.1f}], centroid_xy={cen}")
    print()
