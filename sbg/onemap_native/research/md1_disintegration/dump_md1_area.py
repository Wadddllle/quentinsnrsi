"""Dump every building piece near a target SVY21 point as individual OBJs
(labeled by tile+batch+centroid+bbox) plus one combined context OBJ, so the
user can visually pick out which piece is actually the target building --
per their report, isolating "the" building here has been unreliable before."""
import sys
sys.path.insert(0, "/home/quentin/snrsi")
import numpy as np
from pyproj import Transformer

from sbg.onemap_native.tiles import domain_leaf_tiles, fetch_tile, feature_table, load_gltf
from sbg.onemap_native.extract import _draco_prims, FLAT_PLATE_Z
from sbg.onemap_native.transform import local_to_svy21

TARGET = np.array([22564.79267465497, 30648.979156176567])
RADIUS = 150.0  # m, generous -- want context buildings too

svy_to_wgs = Transformer.from_crs("EPSG:3414", "EPSG:4326", always_xy=True)
xmin, ymin = TARGET[0] - RADIUS, TARGET[1] - RADIUS
xmax, ymax = TARGET[0] + RADIUS, TARGET[1] + RADIUS
lon_min, lat_min = svy_to_wgs.transform(xmin, ymin)
lon_max, lat_max = svy_to_wgs.transform(xmax, ymax)
leaves = domain_leaf_tiles(lon_min, lat_min, lon_max, lat_max)
print(f"{len(leaves)} leaf tiles near target")

out_dir = "/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583/scratchpad/md1_area"
import os
os.makedirs(out_dir, exist_ok=True)

def write_obj(path, v, f):
    with open(path, "w") as fh:
        for p in v:
            fh.write(f"v {p[0]:.4f} {p[1]:.4f} {p[2]:.4f}\n")
        for tri in f:
            fh.write(f"f {tri[0]+1} {tri[1]+1} {tri[2]+1}\n")

pieces = []
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
        print("skip", uri, e)
        continue
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
            cen = v[:, :2].mean(axis=0)
            dist = np.linalg.norm(cen - TARGET)
            if dist > RADIUS:
                continue
            remap = np.zeros(len(svy), dtype=np.int64)
            remap[used] = np.arange(len(used))
            f_local = remap[sub_faces]
            pieces.append({
                "tile": uri.split("/")[-1], "batch": int(c), "v": v, "f": f_local,
                "centroid": cen, "dist": dist,
                "bbox_xy": (v[:,0].min(), v[:,1].min(), v[:,0].max(), v[:,1].max()),
                "z": (float(v[:,2].min()), float(v[:,2].max())),
            })

pieces.sort(key=lambda p: p["dist"])
print(f"\n{len(pieces)} pieces within {RADIUS}m of target ({TARGET}):\n")

combined_v = []
combined_f = []
offset = 0
for i, p in enumerate(pieces):
    name = f"{i:02d}_tile{p['tile'].replace('.b3dm','')}_batch{p['batch']}"
    fn = os.path.join(out_dir, f"{name}.obj")
    write_obj(fn, p["v"], p["f"])
    bx = p["bbox_xy"]
    span_x, span_y = bx[2]-bx[0], bx[3]-bx[1]
    print(f"  [{i:02d}] dist={p['dist']:6.1f}m  centroid=({p['centroid'][0]:.1f},{p['centroid'][1]:.1f})  "
          f"span=({span_x:.1f}x{span_y:.1f})m  z=[{p['z'][0]:.1f},{p['z'][1]:.1f}]  "
          f"nfaces={len(p['f'])}  tile={p['tile']} batch={p['batch']}  -> {name}.obj")
    combined_v.append(p["v"])
    combined_f.append(p["f"] + offset)
    offset += len(p["v"])

write_obj(os.path.join(out_dir, "_combined_context.obj"),
          np.concatenate(combined_v), np.concatenate(combined_f))
print(f"\nAll pieces + combined context written to {out_dir}/")
