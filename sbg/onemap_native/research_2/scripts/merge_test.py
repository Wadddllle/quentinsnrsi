"""Group-fTetWild merge: union touching buildings by MESHING THEM TOGETHER.

Rationale: all buildings get one material, so touching buildings have no reason
to be separate DAGMC volumes. Merge VOLUMES (no shared-surface bookkeeping).

Why this is not a repeat of s10's failure: domain-scale fTetWild died because
eps closed REAL AIR GAPS (2.2cm between distinct buildings). Inside an
overlapping group there IS no air -- they interpenetrate, they are one
structure. So group ONLY what genuinely overlaps; leave real gaps alone.
"""
import sys, os, io, contextlib, time, pickle
sys.path.insert(0,"/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np
from multiprocessing import Pool
from shapely.geometry import box
from pyproj import Transformer
import scipy.sparse as sps, scipy.sparse.csgraph as csg
from sbg.onemap_native.tiles import domain_leaf_tiles
from sbg.onemap_native.extract import extract_domain_buildings, seal_piece

SP="/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583/scratchpad"
B=(28941,28758,29341,29158); EPS=0.15

def boundary(tv,tt):
    q=np.vstack([tt[:,[0,1,2]],tt[:,[0,1,3]],tt[:,[0,2,3]],tt[:,[1,2,3]]])
    opp=np.concatenate([tt[:,3],tt[:,2],tt[:,1],tt[:,0]])
    _,idx,cnt=np.unique(np.sort(q,axis=1),axis=0,return_index=True,return_counts=True)
    k=idx[cnt==1]; bf=q[k]
    a,b,c=tv[bf[:,0]],tv[bf[:,1]],tv[bf[:,2]]
    fl=np.einsum('ij,ij->i',np.cross(b-a,c-a),tv[opp[k]]-a)>0
    bf[fl]=bf[fl][:,[0,2,1]]
    u=np.unique(bf); rm=np.full(len(tv),-1,np.int64); rm[u]=np.arange(len(u))
    return tv[u],rm[bf]

def audit(v,f):
    v=np.ascontiguousarray(np.asarray(v,np.float32))
    uq,inv=np.unique(v.view([('',np.float32)]*3).ravel(),return_inverse=True)
    i=inv.astype(np.int64)[np.ascontiguousarray(f).astype(np.int64)]
    i=i[(i[:,0]!=i[:,1])&(i[:,1]!=i[:,2])&(i[:,0]!=i[:,2])]
    n=np.int64(len(uq)); d=np.vstack([i[:,[0,1]],i[:,[1,2]],i[:,[2,0]]])
    a,b=d[:,0],d[:,1]
    _,c=np.unique(np.minimum(a,b)*n+np.maximum(a,b),return_counts=True)
    return int((c==1).sum()),int((c>2).sum())

def mesh_group(arg):
    gi,V,F=arg
    import wildmeshing as wm, meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
    ctr=V.mean(axis=0); vl=V-ctr
    diag=float(np.linalg.norm(vl.max(axis=0)-vl.min(axis=0)))
    for eps in (EPS,0.05,0.015):
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                t=wm.Tetrahedralizer(epsilon=eps/diag,edge_length_r=0.05,coarsen=True,
                                     max_its=0,stop_quality=10,max_threads=1)
                t.set_mesh(vl,F); t.tetrahedralize()
                o=t.get_tet_mesh(floodfill=True,manifold_surface=False,
                                 correct_surface_orientation=True)
            tv,tt=np.asarray(o[0],float),np.asarray(o[1])
            if len(tv)==0 or len(tt)==0: continue
            bv,bf=boundary(tv,tt)
            if len(bv)==0 or len(bf)<4: continue
            op,nm=audit(bv,bf)
            if op or nm: continue
            ml=mn.meshFromFacesVerts(np.asarray(bf,np.int32),np.asarray(bv,float))
            if mr.findSelfCollidingTriangles(mr.MeshPart(ml)).size(): continue
            return dict(gi=gi,v=bv+ctr,f=bf,eps=eps,ok=True)
        except Exception: continue
    return dict(gi=gi,v=None,f=None,eps=None,ok=False)

if __name__=="__main__":
    import meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
    tr=Transformer.from_crs("EPSG:3414","EPSG:4326",always_xy=True)
    lo,la=tr.transform([B[0],B[2]],[B[1],B[3]])
    P=extract_domain_buildings(domain_leaf_tiles(min(lo),min(la),max(lo),max(la)),
                               box(*B),store_dir="data/onemap_store")
    RAW=[]
    for p in P:
        try: sv,sf=seal_piece(p["verts"],p["faces"])
        except Exception: continue
        sv=np.asarray(sv,float); sf=np.asarray(sf)
        if len(sf)>=4: RAW.append((sv,sf))
    C=pickle.load(open(f"{SP}/duxton_cache.pkl","rb"))
    idx=[c["i"] for c in C if c["v"] is not None and len(c["v"])>0]
    good=[c for c in C if c["v"] is not None and len(c["v"])>0]
    M=[mn.meshFromFacesVerts(np.asarray(c["f"],np.int32),np.asarray(c["v"],float)) for c in good]
    bb=np.array([[c["v"].min(axis=0),c["v"].max(axis=0)] for c in good])
    E=[]
    for a in range(len(good)):
        for b in range(a+1,len(good)):
            if (bb[a,0]>bb[b,1]).any() or (bb[b,0]>bb[a,1]).any(): continue
            if mr.findCollidingTriangles(mr.MeshPart(M[a]),mr.MeshPart(M[b])).size()>0:
                E.append((a,b))
    n=len(good)
    g=sps.coo_matrix((np.ones(len(E)),([e[0] for e in E],[e[1] for e in E])),shape=(n,n))
    ncomp,lab=csg.connected_components(g+g.T,directed=False)
    sizes=np.bincount(lab)
    print(f"{n} volumes, {len(E)} overlapping pairs -> {ncomp} components")
    print(f"  singletons {int((sizes==1).sum())}, multi {int((sizes>1).sum())}, "
          f"largest {sizes.max()}",flush=True)

    jobs=[]
    for gi in range(ncomp):
        mem=np.where(lab==gi)[0]
        vs,fs,off=[],[],0
        for m in mem:
            v,f=RAW[idx[m]]
            vs.append(v); fs.append(np.asarray(f,np.int32)+off); off+=len(v)
        jobs.append((gi,np.vstack(vs),np.vstack(fs).astype(np.int32)))
    t0=time.time()
    with Pool(10) as pool: R=pool.map(mesh_group,jobs)
    ok=[r for r in R if r["ok"]]
    print(f"[{time.time()-t0:.0f}s] groups meshed 0/0/0/0: {len(ok)}/{len(R)}")
    for e in (EPS,0.05,0.015):
        print(f"   at eps={e}: {sum(1 for r in ok if r['eps']==e)}")
    pickle.dump([dict(i=r["gi"],v=r["v"],f=r["f"]) for r in ok],
                open(f"{SP}/duxton_merged.pkl","wb"))
    print(f"saved {len(ok)} merged volumes")
