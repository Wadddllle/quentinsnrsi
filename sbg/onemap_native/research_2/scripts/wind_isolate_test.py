import sys, os; sys.path.insert(0,"/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np
from collections import defaultdict
from shapely.geometry import box
from sbg.onemap_native.terrain import (conforming_terrain, terrain_flat_base_solid,
                                       build_domain_dtm, DtmSampler)
B=(28941,28758,29341,29158)
grid,aff=build_domain_dtm(B,step=5.0); dtm=DtmSampler(grid,aff)
dom=box(*B)
tv,tt=conforming_terrain(dom,dtm,[],[],terrain_step=10.0)

def wind_and_bnd(v,f):
    v=np.asarray(v,float); f=np.asarray(f)
    _,inv=np.unique(np.round(v,4),axis=0,return_inverse=True)
    wf=inv.reshape(-1)[f]
    wf=wf[(wf[:,0]!=wf[:,1])&(wf[:,1]!=wf[:,2])&(wf[:,0]!=wf[:,2])]
    he=set(); wind=0
    for a,b,c in wf:
        for x,y in ((a,b),(b,c),(c,a)):
            if (x,y) in he: wind+=1
            he.add((x,y))
    ed=np.sort(np.vstack([wf[:,[0,1]],wf[:,[1,2]],wf[:,[0,2]]]),axis=1)
    _,cnt=np.unique(ed,axis=0,return_counts=True)
    return wind,int((cnt==1).sum()),int((cnt>2).sum()),len(wf)

w,b,nm,nf=wind_and_bnd(tv,tt)
print(f"CDT terrain surface alone      : faces={nf:>7,} wind={w:>4} bnd={b:>6} NM={nm}")
sv,sf=terrain_flat_base_solid(tv,tt,dom,base_z=-10.0,terrain_step=10.0)
w,b,nm,nf=wind_and_bnd(sv,sf)
print(f"+ flat base solid (as shipped) : faces={nf:>7,} wind={w:>4} bnd={b:>6} NM={nm}")

# how many faces did the solidify add? -> that tells us wall+cap size
print(f"  (solidify added {len(sf)-len(tt):,} faces)")

# Variant A: flip ONLY the added faces (wall+cap)
n_top=len(tt)
sfA=sf.copy(); sfA[n_top:]=sfA[n_top:][:,::-1]
w,b,nm,nf=wind_and_bnd(sv,sfA)
print(f"variant A: flip wall+cap       : faces={nf:>7,} wind={w:>4} bnd={b:>6} NM={nm}")

# Variant B: flip the TOP surface only
sfB=sf.copy(); sfB[:n_top]=sfB[:n_top][:,::-1]
w,b,nm,nf=wind_and_bnd(sv,sfB)
print(f"variant B: flip top surface    : faces={nf:>7,} wind={w:>4} bnd={b:>6} NM={nm}")

n_wall=320
print()
for tag,sl in (("wall only",slice(n_top,n_top+n_wall)),("cap only",slice(n_top+n_wall,None))):
    sfX=sf.copy(); sfX[sl]=sfX[sl][:,::-1]
    w,b,nm,nf=wind_and_bnd(sv,sfX)
    print(f"variant: flip {tag:12s}      : faces={nf:>7,} wind={w:>4} bnd={b:>6} NM={nm}")

# Global BFS consistent orientation (works on any closed manifold surface)
import trimesh
tm=trimesh.Trimesh(sv,sf,process=False)
trimesh.repair.fix_winding(tm)
w,b,nm,nf=wind_and_bnd(tm.vertices,tm.faces)
print(f"trimesh.repair.fix_winding     : faces={nf:>7,} wind={w:>4} bnd={b:>6} NM={nm}")
vol=tm.volume
print(f"  volume after fix_winding = {vol:,.0f} m^3 (sign tells us if it's inside-out)")
