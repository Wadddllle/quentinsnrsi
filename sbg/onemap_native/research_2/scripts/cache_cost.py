"""Per-building fTetWild cost + storage, for an island-wide precompute cache.

Each building ALONE, single-threaded, eps ladder 0.15 -> 0.05 -> 0.015 -> 0.005,
stop as soon as strict audit reads 0/0/0/0. Measures time, retries, output bytes.
"""
import sys, os; sys.path.insert(0,"/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np, io, contextlib, time, tempfile
from multiprocessing import Pool
from shapely.geometry import box
from pyproj import Transformer
from sbg.onemap_native.tiles import domain_leaf_tiles
from sbg.onemap_native.extract import extract_domain_buildings, seal_piece

B=(28941,28758,29341,29158)
LADDER=[0.15,0.05,0.015,0.005]
NPROC=int(os.environ.get("NPROC","10"))
NB=int(os.environ.get("NB","60"))

def strict(v,f):
    v=np.ascontiguousarray(np.asarray(v,np.float32))
    uq,inv=np.unique(v.view([('',np.float32)]*3).ravel(),return_inverse=True)
    i=inv.astype(np.int64)[np.ascontiguousarray(f).astype(np.int64)]
    i=i[(i[:,0]!=i[:,1])&(i[:,1]!=i[:,2])&(i[:,0]!=i[:,2])]
    n=np.int64(len(uq))
    d=np.vstack([i[:,[0,1]],i[:,[1,2]],i[:,[2,0]]])
    a,b=d[:,0],d[:,1]
    _,c=np.unique(np.minimum(a,b)*n+np.maximum(a,b),return_counts=True)
    return int((c==1).sum()),int((c>2).sum())

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

def one(arg):
    i,v,f=arg
    import wildmeshing as wm, meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
    v=np.asarray(v,float); f=np.asarray(f,np.int32)
    diag=float(np.linalg.norm(v.max(axis=0)-v.min(axis=0)))
    rec={"i":i,"in_faces":len(f),"diag":diag,"t":0.0,"tries":0,
         "eps":None,"ok":False,"faces":0,"bytes":0,"nm":-1,"sx":-1}
    for eps in LADDER:
        rec["tries"]+=1; t0=time.time()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                t=wm.Tetrahedralizer(epsilon=eps/diag,edge_length_r=0.05,coarsen=True,
                                     max_its=0,stop_quality=10,max_threads=1)
                t.set_mesh(v,f); t.tetrahedralize()
                o=t.get_tet_mesh(floodfill=True,manifold_surface=False,
                                 correct_surface_orientation=True)
            rec["t"]+=time.time()-t0
            bv,bf=boundary(np.asarray(o[0],float),np.asarray(o[1]))
            op,nm=strict(bv,bf)
            ml=mn.meshFromFacesVerts(np.asarray(bf,np.int32),np.asarray(bv,float))
            sx=mr.findSelfCollidingTriangles(mr.MeshPart(ml)).size()
            rec.update(eps=eps,faces=len(bf),nm=nm,sx=sx,
                       bytes=len(bv)*12+len(bf)*12)   # float32 xyz + uint32 idx
            if op==0 and nm==0 and sx==0:
                rec["ok"]=True; return rec
        except Exception:
            rec["t"]+=time.time()-t0
    return rec

if __name__=="__main__":
    tr=Transformer.from_crs("EPSG:3414","EPSG:4326",always_xy=True)
    lo,la=tr.transform([B[0],B[2]],[B[1],B[3]])
    P=extract_domain_buildings(domain_leaf_tiles(min(lo),min(la),max(lo),max(la)),
                               box(*B),store_dir="data/onemap_store")
    jobs=[]
    for p in P:
        try: sv,sf=seal_piece(p["verts"],p["faces"])
        except Exception: continue
        sv=np.asarray(sv,float); sf=np.asarray(sf)
        if len(sf)>=4: jobs.append((len(jobs),sv-sv.mean(axis=0),sf))
    rng=np.random.default_rng(0); pick=rng.choice(len(jobs),min(NB,len(jobs)),replace=False)
    jobs=[jobs[k] for k in sorted(pick)]
    print(f"{len(jobs)} buildings, ladder={LADDER}, single-threaded, {NPROC} procs",flush=True)
    t0=time.time(); 
    with Pool(NPROC) as pool: R=pool.map(one,jobs)
    wall=time.time()-t0
    ok=[r for r in R if r["ok"]]
    cpu=sum(r["t"] for r in R)
    print(f"\nwall {wall:.0f}s   total CPU-seconds {cpu:.0f}s   ({cpu/len(R):.2f} s/building)")
    print(f"0/0/0/0: {len(ok)}/{len(R)}  ({len(ok)/len(R):.1%})")
    for k,e in enumerate(LADDER):
        n=sum(1 for r in ok if r["eps"]==e)
        print(f"   solved at eps={e:<6}: {n:4}  ({n/len(R):6.1%})")
    print(f"   never solved         : {len(R)-len(ok):4}")
    ts=np.array([r["t"] for r in R]); by=np.array([r["bytes"] for r in ok])
    fa=np.array([r["faces"] for r in ok]); inf=np.array([r["in_faces"] for r in R])
    print(f"\nCPU s/building: mean {ts.mean():.2f} median {np.median(ts):.2f} "
          f"p95 {np.percentile(ts,95):.2f} max {ts.max():.2f}")
    print(f"in faces/bldg : mean {inf.mean():.0f}   out faces/bldg: mean {fa.mean():.0f} "
          f"(x{fa.mean()/inf.mean():.1f})")
    print(f"bytes/building: mean {by.mean():,.0f}  median {np.median(by):,.0f}")
    for N in (118782,146645):
        print(f"\n=== extrapolate to {N:,} buildings ===")
        print(f"  CPU-hours        : {ts.mean()*N/3600:,.0f}")
        for c in (10,12,16,32,64):
            print(f"    on {c:3} cores  : {ts.mean()*N/3600/c:8.1f} h  = {ts.mean()*N/3600/c/24:5.1f} days")
        print(f"  cache raw        : {by.mean()*N/1e9:,.1f} GB")
