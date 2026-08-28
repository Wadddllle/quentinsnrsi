"""Is 'group by proximity, then voxel-remesh each GROUP as one' feasible?
Key risk: a proximity chain through a dense shophouse row links the whole block,
and level-set memory scales with the group's BBOX VOLUME / voxel^3."""
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
DOMAINS={"Duxton 400m (dense shophouse)":(28941,28758,29341,29158),
         "Kent Ridge 900m (campus)":(21950,30250,22850,31150),
         "CBD 2km (towers)":(28500,28500,30500,30500)}
t=Transformer.from_crs("EPSG:3414","EPSG:4326",always_xy=True)

def groups_at(pieces, gap):
    """connected components of 'bbox within `gap` metres'"""
    bb=np.array([[p["verts"][:,0].min(),p["verts"][:,1].min(),p["verts"][:,2].min(),
                  p["verts"][:,0].max(),p["verts"][:,1].max(),p["verts"][:,2].max()]
                 for p in pieces])
    n=len(bb); ii=[];jj=[]
    ctr=(bb[:,:2]+bb[:,3:5])/2
    rad=np.linalg.norm(bb[:,3:5]-bb[:,:2],axis=1)/2
    tree=cKDTree(ctr)
    for a in range(n):
        for b in tree.query_ball_point(ctr[a], rad[a]+rad.max()+gap):
            if b<=a: continue
            #真 bbox gap test
            dx=max(bb[a,0]-bb[b,3], bb[b,0]-bb[a,3], 0)
            dy=max(bb[a,1]-bb[b,4], bb[b,1]-bb[a,4], 0)
            if dx<=gap and dy<=gap: ii.append(a); jj.append(b)
    if not ii: return np.arange(n), bb
    g=coo_matrix((np.ones(len(ii)),(ii,jj)),shape=(n,n))
    _,lbl=connected_components(g,directed=False)
    return lbl, bb

for name,B in DOMAINS.items():
    lo,la=t.transform([B[0],B[2]],[B[1],B[3]])
    t0=time.time()
    pieces=extract_domain_buildings(domain_leaf_tiles(min(lo),min(la),max(lo),max(la)),
                                    box(*B), store_dir="data/onemap_store")
    print(f"\n=== {name} : {len(pieces)} pieces (extract {time.time()-t0:.1f}s) ===")
    for gap in (0.0, 0.5, 2.0):
        lbl,bb=groups_at(pieces,gap)
        ng=len(np.unique(lbl))
        sizes=np.bincount(lbl)
        vox=[]
        for g_ in np.unique(lbl):
            m=lbl==g_
            lo3=bb[m][:,:3].min(axis=0); hi3=bb[m][:,3:].max(axis=0)
            pad=3*VS
            dims=(hi3-lo3)+2*pad
            vox.append(np.prod(dims/VS))
        vox=np.array(vox)
        # narrow-band level set: memory ~ surface voxels, but gridToMesh allocates
        # on the dense bbox grid in the worst case -> report both
        print(f"  gap={gap:>4}m: {ng:>4} groups  max members={sizes.max():>4}  "
              f"max bbox voxels={vox.max():>12,.0f}  "
              f"(dense f32 = {vox.max()*4/1e9:>6.2f} GB)  total voxels={vox.sum():>13,.0f}")
