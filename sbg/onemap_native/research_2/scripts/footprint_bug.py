import sys, os; sys.path.insert(0,"/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np, trimesh, meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
from shapely.geometry import box, Polygon
from shapely.ops import unary_union
from pyproj import Transformer
from sbg.onemap_native.tiles import domain_leaf_tiles
from sbg.onemap_native.extract import extract_domain_buildings, seal_piece

B=(28941,28758,29341,29158)   # Duxton 400m
t=Transformer.from_crs("EPSG:3414","EPSG:4326",always_xy=True)
lo,la=t.transform([B[0],B[2]],[B[1],B[3]])
pieces=extract_domain_buildings(domain_leaf_tiles(min(lo),min(la),max(lo),max(la)),
                                box(*B), store_dir="data/onemap_store")
print(f"{len(pieces)} pieces\n")

rows=[]
for p in pieces:
    fp=p.get("footprint")
    if not fp: continue
    # what PRODUCTION uses: the near-base convex hull ring(s)
    try:
        hull=unary_union([Polygon(np.asarray(r)) for r in fp if len(r)>=3]).buffer(0)
    except Exception: continue
    if hull.is_empty or hull.area<=0: continue
    # TRUE cross-section: slice the sealed solid 1m above its base
    try:
        v,f=seal_piece(p["verts"],p["faces"])
        m=trimesh.Trimesh(v,f,process=False)
        zc=float(v[:,2].min())+1.0
        # WORLD-frame section: slice open-bottomed and read the cut loop from the
        # shell's own outline (to_planar() returns a LOCAL centred frame -- using it
        # made every building look displaced, which was a measurement bug, not a finding)
        sh=m.slice_plane([0,0,zc],[0,0,1],cap=False)
        if sh is None or len(sh.faces)==0: continue
        sh=trimesh.Trimesh(sh.vertices.copy(),sh.faces.copy(),process=False); sh.merge_vertices()
        ol=sh.outline(); ps=[]
        for e in ol.entities:
            pts=ol.vertices[e.points]
            if len(pts)<4: continue
            if abs(float(np.median(pts[:,2]))-zc)>0.25: continue
            q=Polygon(pts[:,:2])
            if not q.is_valid: q=q.buffer(0)
            if not q.is_empty and q.area>0.05: ps.append(q)
        if not ps: continue
        true=unary_union(ps).buffer(0)
    except Exception: continue
    if true.is_empty or true.area<=0: continue
    rows.append((hull.area, true.area, true.area/hull.area,
                 true.difference(hull).area, hull.difference(true).area))

a=np.array(rows)
print(f"buildings compared: {len(a)}")
print(f"\n  hull (production) area  : median {np.median(a[:,0]):8.1f} m^2   total {a[:,0].sum():10.1f}")
print(f"  true section area       : median {np.median(a[:,1]):8.1f} m^2   total {a[:,1].sum():10.1f}")
r=a[:,2]
print(f"\n  ratio true/hull         : p10 {np.percentile(r,10):.2f}  median {np.median(r):.2f}  "
      f"mean {r.mean():.2f}  p90 {np.percentile(r,90):.2f}  max {r.max():.1f}")
print(f"  buildings where the production footprint is TOO SMALL (ratio>1.2): "
      f"{int((r>1.2).sum())}/{len(r)} ({100*(r>1.2).mean():.0f}%)")
print(f"  buildings where it is TOO BIG (ratio<0.8): {int((r<0.8).sum())}/{len(r)}")
print(f"\n  area the real building covers but the pad does NOT (overhang): "
      f"total {a[:,3].sum():.0f} m^2, median {np.median(a[:,3]):.1f} m^2/bldg")
print(f"  area the pad covers but the building does not: total {a[:,4].sum():.0f} m^2")
