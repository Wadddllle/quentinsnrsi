"""Push the 13 still-failing merge groups to eps 0.005 / 0.002. Measure TIME too."""
import sys, os, io, contextlib, time, pickle, signal
sys.path.insert(0,"/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np
from multiprocessing import Pool
from shapely.geometry import box
from pyproj import Transformer
import scipy.sparse as sps, scipy.sparse.csgraph as csg
from sbg.onemap_native.tiles import domain_leaf_tiles
from sbg.onemap_native.extract import extract_domain_buildings, seal_piece
SP="/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583/scratchpad"
B=(28941,28758,29341,29158)
STILL=[9,23,54,95,56,87,5,49,64,70,88,46,85]
EPSL=[0.005,0.002]; TMO=420

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

class TO(Exception): pass
def _al(s,f): raise TO()

def probe(arg):
    gi,nmem,V,F=arg
    import wildmeshing as wm, meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
    ctr=V.mean(axis=0); vl=V-ctr
    diag=float(np.linalg.norm(vl.max(axis=0)-vl.min(axis=0)))
    out=dict(gi=gi,nmem=nmem,faces=len(F),trials=[],solved=None,v=None,f=None)
    for eps in EPSL:
        for k in range(2):
            signal.signal(signal.SIGALRM,_al); signal.alarm(TMO); t0=time.time()
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    t=wm.Tetrahedralizer(epsilon=eps/diag,edge_length_r=0.05,coarsen=True,
                                         max_its=0,stop_quality=10,max_threads=1)
                    t.set_mesh(vl,F); t.tetrahedralize()
                    o=t.get_tet_mesh(floodfill=True,manifold_surface=False,
                                     correct_surface_orientation=True)
                dt=time.time()-t0
                tv,tt=np.asarray(o[0],float),np.asarray(o[1])
                if len(tt)==0: out["trials"].append((eps,k,"EMPTY",0,0,dt)); continue
                bv,bf=boundary(tv,tt); op,nm=audit(bv,bf)
                ml=mn.meshFromFacesVerts(np.asarray(bf,np.int32),np.asarray(bv,float))
                sx=mr.findSelfCollidingTriangles(mr.MeshPart(ml)).size()
                out["trials"].append((eps,k,"OK" if (op==0 and nm==0 and sx==0) else "D",
                                      nm,int(sx),dt))
                if op==0 and nm==0 and sx==0:
                    out["solved"]=eps; out["v"]=bv+ctr; out["f"]=bf
                    signal.alarm(0); return out
            except TO: out["trials"].append((eps,k,"TIMEOUT",0,0,float(TMO)))
            except Exception as e: out["trials"].append((eps,k,f"EXC",0,0,time.time()-t0))
            finally: signal.alarm(0)
    return out

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
    C=[c for c in pickle.load(open(f"{SP}/duxton_cache.pkl","rb"))
       if c["v"] is not None and len(c["v"])>0]
    idx=[c["i"] for c in C]
    M=[mn.meshFromFacesVerts(np.asarray(c["f"],np.int32),np.asarray(c["v"],float)) for c in C]
    bb=np.array([[c["v"].min(axis=0),c["v"].max(axis=0)] for c in C])
    E=[(a,b) for a in range(len(C)) for b in range(a+1,len(C))
       if not((bb[a,0]>bb[b,1]).any() or (bb[b,0]>bb[a,1]).any())
       and mr.findCollidingTriangles(mr.MeshPart(M[a]),mr.MeshPart(M[b])).size()>0]
    n=len(C)
    g=sps.coo_matrix((np.ones(len(E)),([e[0] for e in E],[e[1] for e in E])),shape=(n,n))
    nc,lab=csg.connected_components(g+g.T,directed=False)
    jobs=[]
    for gi in STILL:
        mem=np.where(lab==gi)[0]; vs,fs,off=[],[],0
        for m in mem:
            v,f=RAW[idx[m]]; vs.append(v); fs.append(np.asarray(f,np.int32)+off); off+=len(v)
        jobs.append((gi,len(mem),np.vstack(vs),np.vstack(fs).astype(np.int32)))
    print(f"{len(jobs)} groups, eps {EPSL}, 2 trials each, timeout {TMO}s",flush=True)
    t0=time.time()
    with Pool(7) as pool: R=pool.map(probe,jobs)
    print(f"[{time.time()-t0:.0f}s wall]\n")
    print(f"{'grp':>4} {'bld':>4} {'faces':>6} {'solved':>7}  trials (eps,NM,selfX,sec)")
    nsolved=0; times=[]
    for r in sorted(R,key=lambda r:-r["nmem"]):
        s=" ".join(f"[{e}:{tag}{'' if tag in ('OK','TIMEOUT','EXC','EMPTY') else f'/n{nm}x{sx}'}/{dt:.0f}s]"
                   for e,k,tag,nm,sx,dt in r["trials"])
        print(f"{r['gi']:>4} {r['nmem']:>4} {r['faces']:>6} {str(r['solved']):>7}  {s}")
        if r["solved"]: nsolved+=1
        times += [dt for *_,dt in r["trials"]]
    print(f"\nSOLVED {nsolved}/{len(R)} at eps<=0.005")
    print(f"time per attempt: median {np.median(times):.0f}s  max {max(times):.0f}s")
    good=[dict(i=r["gi"],v=r["v"],f=r["f"]) for r in R if r["solved"]]
    pickle.dump(good,open(f"{SP}/duxton_lowfix.pkl","wb"))
    print(f"saved {len(good)} newly-solved groups")
