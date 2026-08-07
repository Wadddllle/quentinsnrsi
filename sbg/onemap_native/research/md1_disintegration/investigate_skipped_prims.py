import sys
sys.path.insert(0, "/home/quentin/snrsi")
from pyproj import Transformer
from sbg.onemap_native.tiles import domain_leaf_tiles, fetch_tile, feature_table, load_gltf

svy_to_wgs = Transformer.from_crs("EPSG:3414", "EPSG:4326", always_xy=True)
xmin, ymin, xmax, ymax = 22000, 30550, 22280, 30820
lon_min, lat_min = svy_to_wgs.transform(xmin, ymin)
lon_max, lat_max = svy_to_wgs.transform(xmax, ymax)
leaves = domain_leaf_tiles(lon_min, lat_min, lon_max, lat_max)
print(f"{len(leaves)} leaf tiles")

for uri in leaves:
    data = fetch_tile(uri)
    ft = feature_table(data)
    if ft.get("RTC_CENTER") is None:
        continue
    gltf, blob = load_gltf(data)
    n_mesh = len(gltf.meshes)
    n_prim_total = 0
    n_prim_draco = 0
    n_prim_draco_batchid = 0
    skipped_reasons = []
    for mi, mesh in enumerate(gltf.meshes):
        for prim in mesh.primitives:
            n_prim_total += 1
            ext = (prim.extensions or {}).get("KHR_draco_mesh_compression")
            if ext is None:
                skipped_reasons.append("no_draco_ext")
                continue
            n_prim_draco += 1
            if "_BATCHID" not in ext.get("attributes", {}):
                skipped_reasons.append("no_batchid_attr")
                continue
            n_prim_draco_batchid += 1
    print(f"{uri}")
    print(f"  meshes={n_mesh} prims_total={n_prim_total} draco={n_prim_draco} draco+batchid={n_prim_draco_batchid} "
          f"skipped={n_prim_total-n_prim_draco_batchid} reasons={set(skipped_reasons)}")
