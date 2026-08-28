"""Two gates that decide whether an fTetWild per-building cache is usable.

GATE 1  Do cleaned neighbours intersect EACH OTHER? If yes, concatenating them
        breaks selfX=0 and the disjoint-solids route is dead.
GATE 2  Does a cleaned solid SLICE at pad height into a clean planar closed ring?
        That ring is the CDT constraint; no ring, no exact terrain seam.

Also caches the cleaned solids (this is the real cache format) and re-measures
ring area vs the convex-hull footprint the pipeline uses today.
"""
import sys, os; sys.path.insert(0,"/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np, io, contextlib, time, pickle
from multiprocessing import Pool
from shapely.geometry import box, Polygon, MultiPoint
from pyproj import Transformer
from sbg.onemap_native.tiles import domain_leaf_tiles
from sbg.onemap_native.extract import extract_domain_buildings, seal_piece

SP="/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583/scratchpad"
B=(28941,28758,29341,29158); EPS=0.15

def boundary(tv,tt):
    q=np.vstack([tt[:,[0,1,2]],tt[:,[0,1,3]],tt[:,[0,2,3]],tt[:,[1,2,3]]])
    opp=np.concatenate([tt[:,3],tt[:,2],tt[:,1],tt[:,0]])
    _,idx,cnt=np.unique(np.sort(q,axis=1),axis=0,return_index=True,return_counts=True)
    k=idx[cnt==1]; bf=q[k]
    a_,b_,c_=tv[bf[:,0]],tv[bf[:,1]],tv[bf[:,2]]
    fl=np.einsum('ij,ij->i',np.cross(b_-a_,c_-a_),tv[opp[k]]-a_)>0
    bf[fl]=bf[fl][:,[0,2,1]]
    used=np.unique(bf); rm=np.full(len(tv),-1,np.int64); rm[used]=np.arange(len(used))
    return tv[used],rm[bf]

def clean(arg):
    """fTetWild a single piece IN ITS OWN LOCAL FRAME; return solid + offset."""
    i,v,f=arg
    import wildmeshing as wm, meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
    v=np.asarray(v,float); f=np.asarray(f,np.int32)
    ctr=v.mean(axis=0); vl=v-ctr
    diag=float(np.linalg.norm(vl.max(axis=0)-vl.min(axis=0)))
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            t=wm.Tetrahedralizer(epsilon=EPS/diag,edge_length_r=0.05,coarsen=True,
                                 max_its=0,stop_quality=10,max_threads=1)
            t.set_mesh(vl,f); t.tetrahedralize()
            o=t.get_tet_mesh(floodfill=True,manifold_surface=False,
                             correct_surface_orientation=True)
        bv,bf=boundary(np.asarray(o[0],float),np.asarray(o[1]))
        ml=mn.meshFromFacesVerts(np.asarray(bf,np.int32),np.asarray(bv,float))
        sx=mr.findSelfCollidingTriangles(mr.MeshPart(ml)).size()
        return dict(i=i,v=bv+ctr,f=bf,sx=int(sx),ok=True)
    except Exception as e:
        return dict(i=i,v=None,f=None,sx=-1,ok=False)

if __name__=="__main__":
    import meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn, trimesh
    tr=Transformer.from_crs("EPSG:3414","EPSG:4326",always_xy=True)
    lo,la=tr.transform([B[0],B[2]],[B[1],B[3]])
    P=extract_domain_buildings(domain_leaf_tiles(min(lo),min(la),max(lo),max(la)),
                               box(*B),store_dir="data/onemap_store")
    jobs=[];hulls=[]
    for p in P:
        try: sv,sf=seal_piece(p["verts"],p["faces"])
        except Exception: continue
        sv=np.asarray(sv,float); sf=np.asarray(sf)
        if len(sf)<4: continue
        jobs.append((len(jobs),sv,sf))
        try:
            hulls.append(MultiPoint(sv[sv[:,2]<sv[:,2].min()+1.0][:,:2]).convex_hull.area)
        except Exception: hulls.append(0.0)
    print(f"{len(jobs)} sealed pieces, cleaning @ eps={EPS} ...",flush=True)
    t0=time.time()
    with Pool(10) as pool: C=pool.map(clean,jobs)
    C=[c for c in C if c["ok"]]
    print(f"[{time.time()-t0:.0f}s] cleaned {len(C)}/{len(jobs)}   "
          f"self-intersecting: {sum(1 for c in C if c['sx']>0)}",flush=True)
    with open(f"{SP}/duxton_cache.pkl","wb") as fh:
        pickle.dump([{k:c[k] for k in ("i","v","f")} for c in C],fh)
    nb=sum(c["v"].nbytes+c["f"].astype(np.int32).nbytes for c in C)
    print(f"cache: {nb/1e6:.1f} MB for {len(C)} buildings "
          f"({nb/len(C)/1024:.1f} KB/bldg)\n")

    # ---------- GATE 1: pairwise intersection between cleaned neighbours
    print("GATE 1  pairwise intersection between cleaned buildings")
    mls=[mn.meshFromFacesVerts(np.asarray(c["f"],np.int32),np.asarray(c["v"],float))
         for c in C]
    bbs=np.array([[c["v"].min(axis=0),c["v"].max(axis=0)] for c in C])
    npair=0; nhit=0; tot=0
    for a in range(len(C)):
        for b in range(a+1,len(C)):
            if (bbs[a,0]>bbs[b,1]).any() or (bbs[b,0]>bbs[a,1]).any(): continue
            npair+=1
            n=mr.findCollidingTriangles(mr.MeshPart(mls[a]),mr.MeshPart(mls[b])).size()
            if n>0: nhit+=1; tot+=n
    print(f"  bbox-overlapping pairs : {npair}")
    print(f"  ACTUALLY intersecting  : {nhit}   ({tot} colliding triangle pairs)")
    print(f"  -> concatenation selfX would be {tot}"
          f"{'   CLEAN' if tot==0 else '   NEEDS boolean union on those pairs'}\n")

    # ---------- GATE 2: slice at pad height -> planar closed ring
    print("GATE 2  slice cleaned solid at base+1m")
    okslice=0; nring=[]; areas=[]; bad=[]
    for k,c in enumerate(C):
        z0=c["v"][:,2].min()+1.0
        try:
            tm=trimesh.Trimesh(c["v"],c["f"],process=False)
            sh=tm.slice_plane([0,0,z0],[0,0,1],cap=False)
            if sh is None or len(sh.faces)==0: bad.append((k,"empty")); continue
            e=np.sort(sh.edges_sorted,axis=1)
            uq,cnt=np.unique(e,axis=0,return_counts=True)
            be=uq[cnt==1]
            if len(be)==0: bad.append((k,"no boundary")); continue
            zs=sh.vertices[np.unique(be)][:,2]
            if zs.ptp()>1e-6: bad.append((k,f"nonplanar {zs.ptp():.3g}")); continue
            deg=np.bincount(be.ravel())
            if (deg[deg>0]!=2).any(): bad.append((k,"open loop")); continue
            import scipy.sparse as sps, scipy.sparse.csgraph as csg
            nv=sh.vertices.shape[0]
            g=sps.coo_matrix((np.ones(len(be)),(be[:,0],be[:,1])),shape=(nv,nv))
            nl=csg.connected_components(g+g.T,directed=False)[0]-(nv-len(np.unique(be)))
            nring.append(max(nl,1)); okslice+=1
            ring=MultiPoint(sh.vertices[np.unique(be)][:,:2]).convex_hull
            areas.append((ring.area,hulls[c["i"]] if c["i"]<len(hulls) else 0.0))
        except Exception as ex: bad.append((k,type(ex).__name__))
    print(f"  clean planar closed ring : {okslice}/{len(C)}")
    if nring:
        nr=np.array(nring)
        print(f"  rings per building       : mean {nr.mean():.2f}, multi-ring "
              f"{int((nr>1).sum())} (courtyards)")
    if bad:
        from collections import Counter
        print(f"  failures ({len(bad)}): {Counter(r for _,r in bad).most_common(5)}")
    if areas:
        A=np.array([a for a,_ in areas]); H=np.array([h for _,h in areas])
        m=H>0
        print(f"  slice-ring area vs today's convex-hull footprint: "
              f"median ratio {np.median(A[m]/H[m]):.2f}x  (hull is {'larger' if np.median(A[m]/H[m])<1 else 'smaller'})")
