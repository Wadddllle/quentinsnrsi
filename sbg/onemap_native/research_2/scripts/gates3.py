import sys, os; sys.path.insert(0,"/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np, pickle, trimesh
import meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
import scipy.sparse as sps, scipy.sparse.csgraph as csg
from shapely.geometry import MultiPoint
SP="/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583/scratchpad"
C=[c for c in pickle.load(open(f"{SP}/duxton_cache.pkl","rb"))
   if c["v"] is not None and len(c["v"])>0 and len(c["f"])>0]

print("GATE 2 (welded)  slice at base+1m")
okc=0; nring=[]; bad=[]
for c in C:
    z0=float(c["v"][:,2].min())+1.0
    try:
        tm=trimesh.Trimesh(c["v"],c["f"],process=False)
        sh=tm.slice_plane([0,0,z0],[0,0,1],cap=False)
        if sh is None or len(sh.faces)==0: bad.append("empty"); continue
        sh.merge_vertices()                      # <-- the fix
        e=np.sort(sh.edges_sorted,axis=1)
        uq,cnt=np.unique(e,axis=0,return_counts=True); be=uq[cnt==1]
        if len(be)==0: bad.append("no boundary"); continue
        vs=np.unique(be); zs=sh.vertices[vs][:,2]
        if np.ptp(zs)>1e-5: bad.append(f"nonplanar"); continue
        deg=np.bincount(be.ravel(),minlength=len(sh.vertices))
        if (deg[vs]!=2).any(): bad.append("open loop"); continue
        g=sps.coo_matrix((np.ones(len(be)),(be[:,0],be[:,1])),shape=(len(sh.vertices),)*2)
        lab=csg.connected_components(g+g.T,directed=False)[1]
        nring.append(len(np.unique(lab[vs]))); okc+=1
    except Exception as ex: bad.append(type(ex).__name__)
print(f"  clean planar closed ring : {okc}/{len(C)}  ({okc/len(C):.1%})")
if nring:
    nr=np.array(nring); print(f"  rings/bldg mean {nr.mean():.2f}, multi-ring {int((nr>1).sum())}")
if bad:
    from collections import Counter; print(f"  failures: {Counter(bad).most_common()}")

print("\nGATE 1b  are the hits real volume overlap, or coincident party walls?")
mls=[mn.meshFromFacesVerts(np.asarray(c["f"],np.int32),np.asarray(c["v"],float)) for c in C]
vols=[]
bbs=np.array([[c["v"].min(axis=0),c["v"].max(axis=0)] for c in C])
pairs=[]
for a in range(len(C)):
    for b in range(a+1,len(C)):
        if (bbs[a,0]>bbs[b,1]).any() or (bbs[b,0]>bbs[a,1]).any(): continue
        if mr.findCollidingTriangles(mr.MeshPart(mls[a]),mr.MeshPart(mls[b])).size()>0:
            pairs.append((a,b))
print(f"  {len(pairs)} intersecting pairs; measuring intersection volume ...")
for a,b in pairs[:60]:
    try:
        r=mr.boolean(mls[a],mls[b],mr.BooleanOperation.Intersection)
        if r.valid() and r.mesh.topology.numValidFaces()>0:
            vols.append(abs(mr.getMeshVolume(r.mesh)))
        else: vols.append(0.0)
    except Exception: vols.append(-1.0)
v=np.array([x for x in vols if x>=0])
if len(v):
    print(f"  intersection volume (m^3) over {len(v)} pairs: "
          f"median {np.median(v):.3f}  p90 {np.percentile(v,90):.2f}  max {v.max():.2f}")
    print(f"  pairs with ~zero volume (<0.01 m^3, i.e. TOUCHING not overlapping): "
          f"{int((v<0.01).sum())}/{len(v)}")
    print(f"  pairs with real overlap  (>1 m^3): {int((v>1).sum())}/{len(v)}")
