"""Same question, but grouped by REAL mesh proximity (what actually decides whether a
level set fuses two buildings), not bbox overlap. Threshold ~1 voxel."""
import sys, os; sys.path.insert(0,"/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np, time
from shapely.geometry import box
from pyproj import Transformer
from scipy.spatial import cKDTree
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from sbg.onemap_native.tiles import domain_leaf_tiles
from sbg.onemap_native.extract import extract_domain_buildings, seal_piece
VS=0.5
DOMAINS={"Duxton 400m":(28941,28758,29341,29158),
         "Kent Ridge 900m":(21950,30250,22850,31150),
         "CBD 2km":(28500,28500,30500,30500)}
t=Transformer.from_crs("EPSG:3414","EPSG:4326",always_xy=True)
for name,B in DOMAINS.items():
    lo,la=t.transform([B[0],B[2]],[B[1],B[3]])
    pieces=extract_domain_buildings(domain_leaf_tiles(min(lo),min(la),max(lo),max(la)),
                                    box(*B), store_dir="data/onemap_store")
    n=len(pieces)
    bb=np.array([[p["verts"][:,0].min(),p["verts"][:,1].min(),p["verts"][:,2].min(),
                  p["verts"][:,0].max(),p["verts"][:,1].max(),p["verts"][:,2].max()] for p in pieces])
    trees=[cKDTree(p["verts"]) for p in pieces]
    print(f"\n=== {name}: {n} pieces ===")
    for THR in (0.5, 1.0):
        ii=[];jj=[]
        for a in range(n):
            for b in range(a+1,n):
                # cheap bbox reject with the threshold as margin
                if (bb[a,0]-bb[b,3]>THR or bb[b,0]-bb[a,3]>THR or
                    bb[a,1]-bb[b,4]>THR or bb[b,1]-bb[a,4]>THR or
                    bb[a,2]-bb[b,5]>THR or bb[b,2]-bb[a,5]>THR): continue
                d,_=trees[b].query(pieces[a]["verts"], distance_upper_bound=THR)
                if np.isfinite(d).any(): ii.append(a); jj.append(b)
        lbl=np.arange(n)
        if ii:
            g=coo_matrix((np.ones(len(ii)),(ii,jj)),shape=(n,n))
            _,lbl=connected_components(g,directed=False)
        ng=len(np.unique(lbl)); sizes=np.bincount(lbl)
        vox=[]
        for g_ in np.unique(lbl):
            m=lbl==g_
            lo3=bb[m][:,:3].min(axis=0); hi3=bb[m][:,3:].max(axis=0)
            vox.append(np.prod(((hi3-lo3)+3*VS)/VS))
        vox=np.array(vox)
        print(f"  surface-gap<{THR}m: {ng:>4} groups  max members={sizes.max():>4}  "
              f"groups>1={int((sizes>1).sum()):>3}  max bbox voxels={vox.max():>12,.0f} "
              f"({vox.max()*4/1e9:.2f} GB dense)")
