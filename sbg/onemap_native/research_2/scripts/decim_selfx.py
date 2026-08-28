"""Does decimateMesh introduce self-intersections in a GROUP level set?
Per-piece it did not (measured 0/90 buildings). Kent Ridge group #2 came back with
selfX=8 on its own solid, which a level set alone cannot produce."""
import sys, os; sys.path.insert(0,"/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np
import meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
from shapely.geometry import box
from pyproj import Transformer
from scipy.spatial import cKDTree
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from sbg.onemap_native.tiles import domain_leaf_tiles
from sbg.onemap_native.extract import extract_domain_buildings, seal_piece
VS=0.5; GAP=0.5
B=(21950,30250,22850,31150)   # Kent Ridge
t=Transformer.from_crs("EPSG:3414","EPSG:4326",always_xy=True)
lo,la=t.transform([B[0],B[2]],[B[1],B[3]])
pieces=extract_domain_buildings(domain_leaf_tiles(min(lo),min(la),max(lo),max(la)),
                                box(*B), store_dir="data/onemap_store")
sealed=[]
for p in pieces:
    try: sealed.append(seal_piece(p["verts"],p["faces"]))
    except Exception: sealed.append(None)
keep=[i for i,x in enumerate(sealed) if x is not None and len(x[1])>=4]
bb=np.array([[sealed[i][0][:,0].min(),sealed[i][0][:,1].min(),sealed[i][0][:,2].min(),
              sealed[i][0][:,0].max(),sealed[i][0][:,1].max(),sealed[i][0][:,2].max()] for i in keep])
trees=[cKDTree(sealed[i][0]) for i in keep]
n=len(keep); ii=[];jj=[]
for a in range(n):
    for b in range(a+1,n):
        if (bb[a,0]-bb[b,3]>GAP or bb[b,0]-bb[a,3]>GAP or
            bb[a,1]-bb[b,4]>GAP or bb[b,1]-bb[a,4]>GAP or
            bb[a,2]-bb[b,5]>GAP or bb[b,2]-bb[a,5]>GAP): continue
        d,_=trees[b].query(sealed[keep[a]][0], distance_upper_bound=GAP)
        if np.isfinite(d).any(): ii.append(a); jj.append(b)
lbl=np.arange(n)
if ii:
    g=coo_matrix((np.ones(len(ii)),(ii,jj)),shape=(n,n)); _,lbl=connected_components(g,directed=False)
sizes=np.bincount(lbl)
order=np.argsort(-sizes)[:8]     # 8 biggest groups
print(f"{n} pieces -> {len(np.unique(lbl))} groups; testing the 8 largest\n")
print(f"{'grp':>4} {'mem':>4} {'faces_raw':>10} {'sxRaw':>10}  | maxError sweep")
tot_r=tot_d=0
for g_ in order:
    mem=[keep[k] for k in np.where(lbl==g_)[0]]
    V=[];F=[];off=0
    for i in mem:
        v_,f_=sealed[i]; V.append(v_); F.append(np.asarray(f_)+off); off+=len(v_)
    gv=np.vstack(V); gf=np.vstack(F)
    src=mn.meshFromFacesVerts(np.asarray(gf,np.int32),np.asarray(gv,float))
    grid=mr.meshToLevelSet(mr.MeshPart(src),mr.AffineXf3f(),mr.Vector3f(VS,VS,VS),3.0)
    st=mr.GridToMeshSettings(); st.voxelSize=mr.Vector3f(VS,VS,VS); st.isoValue=0.0; st.adaptivity=0.0
    r=mr.gridToMesh(grid,st)
    n_raw=r.topology.numValidFaces(); sx_raw=mr.findSelfCollidingTriangles(mr.MeshPart(r)).size()
    row=f"{g_:>4} {len(mem):>4} {n_raw:>10,} {sx_raw:>10}"
    for err in (0.25, 0.10, 0.05):
        rr=mr.gridToMesh(grid,st)
        ds=mr.DecimateSettings(); ds.maxError=err; mr.decimateMesh(rr,ds)
        row+=f" | {err}: {rr.topology.numValidFaces():>9,}f sx={mr.findSelfCollidingTriangles(mr.MeshPart(rr)).size():>4}"
        if err==0.25: tot_d+=mr.findSelfCollidingTriangles(mr.MeshPart(rr)).size()
    tot_r+=sx_raw
    print(row, flush=True)
print(f"\nTOTAL selfX  before decimate={tot_r}   after decimate={tot_d}")
