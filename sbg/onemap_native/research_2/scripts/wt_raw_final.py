"""EXPERIMENT ONLY. Watertight RAW export: drop pieces seal_piece cannot close
(tiny, ~100 faces), shrink per-piece in XY to break party-wall coincidence,
sink into terrain so solids interpenetrate."""
import sys, collections, numpy as np, trimesh
sys.path.insert(0,"/home/quentin/snrsi")
from shapely.geometry import box
from pyproj import Transformer
from sbg.onemap_native import extract as EX
from sbg.onemap_native.tiles import domain_leaf_tiles
from sbg.onemap_native.terrain import (build_domain_dtm, DtmSampler, _piece_polygons,
                                       place_on_terrain_conforming, terrain_flat_base_solid)
STORE="/home/quentin/snrsi/data/onemap_store"
SP="/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583/scratchpad/"

def build(B, sink=0.05, shrink=0.01, out=None, drop_unclosed=True):
    dom=box(*B)
    tf=Transformer.from_crs("EPSG:3414","EPSG:4326",always_xy=True)
    lo,la=tf.transform([B[0],B[2]],[B[1],B[3]])
    raw=EX.extract_domain_buildings(domain_leaf_tiles(min(lo),min(la),max(lo),max(la)),
                                    dom, store_dir=STORE)
    split=[]
    for p in raw: split.extend(EX.split_piece_components(p))
    keep=[p for p,poly in zip(split,_piece_polygons(split)) if poly is not None]
    dropped=0
    if drop_unclosed:
        ok=[]
        for p in keep:
            m=trimesh.Trimesh(p["verts"],p["faces"],process=False); m.merge_vertices()
            if m.is_watertight and m.body_count==1: ok.append(p)
            else: dropped+=1
        keep=ok
    gz,af=build_domain_dtm(B,step=5.0); dtm=DtmSampler(gz,af)
    bv,bf,tv,tf_=place_on_terrain_conforming(keep,dtm,dom,skirt=False,mode="group",seal=False)
    sv,sf=terrain_flat_base_solid(tv,tf_,dom,base_z=min(0.0,float(bv[:,2].min())-1.0),
                                  terrain_step=10.0)
    bv=bv.copy(); bv[:,2]-=sink
    off=0
    for p in keep:
        n=len(p["verts"]); v=bv[off:off+n]; c=v[:,:2].mean(axis=0)
        r=np.linalg.norm(v[:,:2]-c,axis=1).max()
        if r>shrink: v[:,:2]=c+(v[:,:2]-c)*((r-shrink)/r)
        off+=n
    allv=np.vstack([sv,bv]); allf=np.vstack([sf,bf+len(sv)])
    mm=trimesh.Trimesh(allv,allf,process=False); mm.merge_vertices()
    e=np.sort(mm.edges_sorted,axis=1); cc=collections.Counter(map(tuple,e))
    comps=mm.split(only_watertight=False); cl=sum(1 for x in comps if x.is_watertight)
    nm=sum(1 for x in cc.values() if x>2); op=sum(1 for x in cc.values() if x==1)
    print(f"  buildings={len(keep)} (dropped {dropped} unsealable)  faces={len(mm.faces):,}  "
          f"comps={len(comps)} CLOSED={cl} open={op} nonManif={nm}"
          f"  {'** ALL CLOSED **' if cl==len(comps) and nm==0 and op==0 else ''}")
    if out:
        trimesh.Trimesh(allv,allf,process=False).export(out)
        print(f"    -> {out}")

for name,B,f in [("KentRidge",(21950,30250,22850,31150),"WTRAW_kr.stl"),
                 ("Duxton",(28941,28758,29341,29158),"WTRAW_duxton.stl")]:
    print(f"\n===== {name} =====")
    build(B, out=SP+f)
