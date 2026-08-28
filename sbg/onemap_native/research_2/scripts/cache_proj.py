"""BOUNDED cost projection for an island-wide per-building fTetWild cache.

eps=0.15, ONE attempt, no ladder. A weekend job must have a bounded worst case;
the retry ladder does not (eps=0.005 on a 12k-face piece is effectively unbounded).
Failures are counted, not retried -- they become a separate, much smaller queue.
"""
import sys, os; sys.path.insert(0,"/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np, io, contextlib, time, pickle
from multiprocessing import Pool
from shapely.geometry import box
from pyproj import Transformer
from sbg.onemap_native.tiles import domain_leaf_tiles
from sbg.onemap_native.extract import extract_domain_buildings, seal_piece

SP="/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583/scratchpad"
DOMAINS={"duxton":(28941,28758,29341,29158),"cbd":(29300,29300,29900,29900),
         "kentridge":(21950,30250,22850,31150),"queenstown":(24000,30000,24800,30800),
         "jurong":(13000,35000,14000,36000),"bishan":(28000,36000,29000,37000)}
EPS=0.15

def strict(v,f):
    v=np.ascontiguousarray(np.asarray(v,np.float32))
    uq,inv=np.unique(v.view([('',np.float32)]*3).ravel(),return_inverse=True)
    i=inv.astype(np.int64)[np.ascontiguousarray(f).astype(np.int64)]
    i=i[(i[:,0]!=i[:,1])&(i[:,1]!=i[:,2])&(i[:,0]!=i[:,2])]
    n=np.int64(len(uq)); d=np.vstack([i[:,[0,1]],i[:,[1,2]],i[:,[2,0]]])
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
    t0=time.time()
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            t=wm.Tetrahedralizer(epsilon=EPS/diag,edge_length_r=0.05,coarsen=True,
                                 max_its=0,stop_quality=10,max_threads=1)
            t.set_mesh(v,f); t.tetrahedralize()
            o=t.get_tet_mesh(floodfill=True,manifold_surface=False,
                             correct_surface_orientation=True)
        dt=time.time()-t0
        bv,bf=boundary(np.asarray(o[0],float),np.asarray(o[1]))
        op,nm=strict(bv,bf)
        ml=mn.meshFromFacesVerts(np.asarray(bf,np.int32),np.asarray(bv,float))
        sx=mr.findSelfCollidingTriangles(mr.MeshPart(ml)).size()
        return dict(i=i,inf=len(f),t=dt,ok=(op==0 and nm==0 and sx==0),
                    op=op,nm=nm,sx=sx,of=len(bf),by=len(bv)*12+len(bf)*12)
    except Exception as e:
        return dict(i=i,inf=len(f),t=time.time()-t0,ok=False,op=-1,nm=-1,sx=-1,
                    of=0,by=0)

if __name__=="__main__":
    tr=Transformer.from_crs("EPSG:3414","EPSG:4326",always_xy=True)
    allp=[]
    for name,B in DOMAINS.items():
        lo,la=tr.transform([B[0],B[2]],[B[1],B[3]])
        P=extract_domain_buildings(domain_leaf_tiles(min(lo),min(la),max(lo),max(la)),
                                   box(*B),store_dir="data/onemap_store")
        for p in P:
            try: sv,sf=seal_piece(p["verts"],p["faces"])
            except Exception: continue
            sv=np.asarray(sv,float); sf=np.asarray(sf)
            if len(sf)>=4: allp.append((sv-sv.mean(axis=0),sf))
    fc=np.array([len(f) for _,f in allp])
    print(f"{len(allp)} sealed pieces / 6 domains | faces p50={np.percentile(fc,50):.0f} "
          f"mean={fc.mean():.0f} p99={np.percentile(fc,99):.0f} max={fc.max()}",flush=True)

    edges=np.unique(np.percentile(fc,[0,40,60,75,85,92,96,99,100])).astype(int)
    rng=np.random.default_rng(1); pick=[]
    for a,b in zip(edges[:-1],edges[1:]):
        idx=np.where((fc>=a)&(fc<b))[0]
        if len(idx): pick+=list(rng.choice(idx,min(14,len(idx)),replace=False))
    pick=sorted(set(list(pick)+list(np.argsort(fc)[-5:])))
    jobs=[(k,allp[k][0],allp[k][1]) for k in pick]
    print(f"timing {len(jobs)} pieces @ eps={EPS}, 1 try, faces "
          f"{fc[pick].min()}..{fc[pick].max()}",flush=True)
    t0=time.time()
    with Pool(10) as pool: R=pool.map(one,jobs)
    print(f"[{time.time()-t0:.0f}s wall]\n")

    x=np.array([r["inf"] for r in R],float); y=np.array([r["t"] for r in R])
    ok=np.array([r["ok"] for r in R])
    by=np.array([r["by"] for r in R],float)
    print(f"0/0/0/0 at eps=0.15 first try: {ok.sum()}/{len(R)}  ({ok.mean():.1%})")
    bad=[r for r in R if not r["ok"]]
    if bad:
        print("  failures (faces, open, NM, selfX):")
        for r in bad[:12]: print(f"    {r['inf']:7,}  {r['op']:4} {r['nm']:4} {r['sx']:5}")
    m=(x>0)&(y>0); b_,la_=np.polyfit(np.log(x[m]),np.log(y[m]),1); a_=np.exp(la_)
    mb=(by>0); bb_,lab=np.polyfit(np.log(x[mb]),np.log(by[mb]),1); ab=np.exp(lab)
    print(f"\nfit  t = {a_:.4g}*faces^{b_:.3f}   (measured max {y.max():.1f}s "
          f"on {int(x[np.argmax(y)]):,} faces)")
    print(f"fit  bytes = {ab:.4g}*faces^{bb_:.3f}")
    tpred=a_*fc**b_; bpred=ab*fc**bb_
    print(f"\nover the REAL face distribution: {tpred.mean():.2f} s/bldg, "
          f"{bpred.mean():,.0f} bytes/bldg")
    print(f"  worst single building in sample: {y.max():.1f}s")
    for N in (118782,146645):
        H=tpred.mean()*N/3600
        print(f"\n=== {N:,} buildings, eps=0.15 pass ===")
        print(f"  {H:,.0f} CPU-hours   cache {bpred.mean()*N/1e9:.1f} GB")
        for c in (8,10,12,16):
            print(f"    {c:2} cores: {H/c:6.1f} h = {H/c/24:4.2f} days")
        print(f"  expected failures needing a 2nd pass: ~{int((1-ok.mean())*N):,}")
    with open(f"{SP}/proj.pkl","wb") as fh: pickle.dump(dict(R=R,fc=fc,a=a_,b=b_),fh)
    print("\nsaved -> proj.pkl")
