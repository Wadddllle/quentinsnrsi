import sys, os; sys.path.insert(0,"/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np
from shapely.geometry import box
from pyproj import Transformer
from sbg.onemap_native.tiles import domain_leaf_tiles
from sbg.onemap_native.extract import extract_domain_buildings, seal_piece

B=(28941,28758,29341,29158)
tr=Transformer.from_crs("EPSG:3414","EPSG:4326",always_xy=True)
lo,la=tr.transform([B[0],B[2]],[B[1],B[3]])
P=extract_domain_buildings(domain_leaf_tiles(min(lo),min(la),max(lo),max(la)),
                           box(*B),store_dir="data/onemap_store")
tot=0.0; nf=0; n=0
for p in P:
    try: v,f=seal_piece(p["verts"],p["faces"])
    except Exception: continue
    v=np.asarray(v,float); f=np.asarray(f)
    if len(f)<4: continue
    a=np.linalg.norm(np.cross(v[f[:,1]]-v[f[:,0]], v[f[:,2]]-v[f[:,0]]),axis=1)*0.5
    tot+=a.sum(); nf+=len(f); n+=1
print(f"pieces={n}  faces={nf}  building surface area = {tot:,.0f} m^2")
dom=(B[2]-B[0])*(B[3]-B[1])
print(f"domain footprint = {dom:,} m^2   (terrain adds ~{dom:,} m^2 top + sides)")
tot_all = tot + dom
for s in (0.15, 0.10, 0.022, 0.011):
    print(f"  sample spacing {s:6.3f} m -> N = {tot_all/s**2:15,.0f} samples")
