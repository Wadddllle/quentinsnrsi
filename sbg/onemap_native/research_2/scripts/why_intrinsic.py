"""Why does a SINGLE building have non-manifold edges after fTetWild?

Discriminator: if eps is collapsing a genuine sub-eps gap INSIDE the building,
shrinking eps removes the pinch. If it is real self-touching geometry, eps does
nothing. Also report component count -- a _BATCHID piece is not always one
building, and two structures touching inside one piece is the same tangency
mechanism as between pieces.
"""
import sys, os; sys.path.insert(0,'/home/quentin/snrsi'); os.chdir('/home/quentin/snrsi')
import numpy as np, io, contextlib
from multiprocessing import Pool
from shapely.geometry import box
from pyproj import Transformer
from sbg.onemap_native.tiles import domain_leaf_tiles
from sbg.onemap_native.extract import extract_domain_buildings, seal_piece
B=(28941,28758,29341,29158)
BAD=[178,135,138,154,185,32,59,144]
def one(arg):
    i,v,f,eps=arg
    import wildmeshing as wm
    import meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
    v=np.asarray(v,float); f=np.asarray(f,np.int32)
    diag=float(np.linalg.norm(v.max(axis=0)-v.min(axis=0)))
    try:
        buf=io.StringIO()
        with contextlib.redirect_stdout(buf):
            t=wm.Tetrahedralizer(epsilon=eps/diag,edge_length_r=0.05,coarsen=True,
                                 max_its=0,stop_quality=10,max_threads=1)
            t.set_mesh(v,f); t.tetrahedralize()
            o=t.get_tet_mesh(floodfill=True,manifold_surface=False,correct_surface_orientation=True)
        tv,tt=np.asarray(o[0],float),np.asarray(o[1])
        q=np.vstack([tt[:,[0,1,2]],tt[:,[0,1,3]],tt[:,[0,2,3]],tt[:,[1,2,3]]])
        _,idx,cnt=np.unique(np.sort(q,axis=1),axis=0,return_index=True,return_counts=True)
        bf=q[idx[cnt==1]]
        u=np.unique(bf); rm=np.full(len(tv),-1,np.int64); rm[u]=np.arange(len(u))
        bv,bff=tv[u],rm[bf]
        v32=np.ascontiguousarray(np.asarray(bv,np.float32))
        uq,inv=np.unique(v32.view([('',np.float32)]*3).ravel(),return_inverse=True)
        ii=inv.astype(np.int64)[bff]
        ii=ii[(ii[:,0]!=ii[:,1])&(ii[:,1]!=ii[:,2])&(ii[:,0]!=ii[:,2])]
        n=np.int64(len(uq)); d=np.vstack([ii[:,[0,1]],ii[:,[1,2]],ii[:,[2,0]]])
        a,b=d[:,0],d[:,1]
        _,c=np.unique(np.minimum(a,b)*n+np.maximum(a,b),return_counts=True)
        ml=mn.meshFromFacesVerts(np.asarray(bff,np.int32),np.asarray(bv,float))
        nc=mr.MeshComponents.getAllComponents(ml).size()
        return i,eps,int((c>2).sum()),nc
    except Exception as e:
        return i,eps,-1,-1
if __name__=='__main__':
    tr=Transformer.from_crs('EPSG:3414','EPSG:4326',always_xy=True)
    lo,la=tr.transform([B[0],B[2]],[B[1],B[3]])
    P=extract_domain_buildings(domain_leaf_tiles(min(lo),min(la),max(lo),max(la)),box(*B),store_dir='data/onemap_store')
    S=[]
    for p in P:
        try: sv,sf=seal_piece(p['verts'],p['faces'])
        except Exception: continue
        sv=np.asarray(sv,float); sf=np.asarray(sf)
        if len(sf)>=4: S.append((sv-sv.mean(axis=0),sf))
    jobs=[]
    for i in BAD:
        for eps in (0.10,0.02,0.005):
            jobs.append((i,S[i][0],S[i][1],eps))
    with Pool(8) as pool: res=pool.map(one,jobs)
    d={}
    for i,eps,nm,nc in res: d[(i,eps)]=(nm,nc)
    print(f"{'piece':>6} {'faces':>7} | {'eps=0.10':>16} {'eps=0.02':>16} {'eps=0.005':>16}")
    print(f"{'':6} {'':7} | {'NM  comp':>16} {'NM  comp':>16} {'NM  comp':>16}")
    for i in BAD:
        row=f"{i:6} {len(S[i][1]):7,} |"
        for eps in (0.10,0.02,0.005):
            nm,nc=d[(i,eps)]
            row+=f"{nm:10}{nc:6} "
        print(row)
