"""Stage-by-stage defect provenance for the slice+CDT pipeline.
Spec (non-negotiable): closed, edge-manifold, vertex-manifold, intersection-free.
For each stage, count which of those four are violated and by how much."""
import sys, os; sys.path.insert(0,"/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np, trimesh, time
import meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
from collections import defaultdict
from shapely.geometry import box
from pyproj import Transformer
from sbg.onemap_native.tiles import domain_leaf_tiles
from sbg.onemap_native.extract import extract_domain_buildings, seal_piece

VS=0.5; DEC_ERR=0.25; SHRINK=0.05; DZ=1.0
N=int(os.environ.get("N","60"))

def audit(v, f, cut_z=None):
    """-> dict of the four spec violations. cut_z: boundary at that z is BY DESIGN."""
    v=np.asarray(v,float); f=np.asarray(f)
    if len(f)==0: return None
    uq,inv=np.unique(np.round(v,4),axis=0,return_inverse=True)
    wf=inv.reshape(-1)[f]
    wf=wf[(wf[:,0]!=wf[:,1])&(wf[:,1]!=wf[:,2])&(wf[:,0]!=wf[:,2])]
    if len(wf)==0: return None
    ed=np.sort(np.vstack([wf[:,[0,1]],wf[:,[1,2]],wf[:,[0,2]]]),axis=1)
    ued,cnt=np.unique(ed,axis=0,return_counts=True)
    bnd=ued[cnt==1]
    if cut_z is not None and len(bnd):
        zb=uq[bnd][:,:,2]
        at_cut=int((np.abs(zb-cut_z)<0.05).all(axis=1).sum())
    else: at_cut=0
    # winding
    he=set(); wind=0
    for a_,b_,c_ in wf:
        for x,y in ((a_,b_),(b_,c_),(c_,a_)):
            if (x,y) in he: wind+=1
            he.add((x,y))
    # bowties
    link=defaultdict(list)
    for a_,b_,c_ in wf:
        link[a_].append((b_,c_)); link[b_].append((c_,a_)); link[c_].append((a_,b_))
    bow=0
    for vt,es in link.items():
        adj=defaultdict(list)
        for x,y in es: adj[x].append(y); adj[y].append(x)
        seen=set(); comps=0
        for s in adj:
            if s in seen: continue
            comps+=1; st=[s]; seen.add(s)
            while st:
                n_=st.pop()
                for k in adj[n_]:
                    if k not in seen: seen.add(k); st.append(k)
        if comps>1: bow+=1
    try:
        ml=mn.meshFromFacesVerts(np.asarray(wf,np.int32),np.asarray(uq,float))
        sx=mr.findSelfCollidingTriangles(mr.MeshPart(ml)).size()
    except Exception: sx=-1
    return dict(faces=len(wf), bnd=int((cnt==1).sum())-at_cut, bnd_at_cut=at_cut,
                nm=int((cnt>2).sum()), bow=bow, wind=wind, sx=int(sx))

def show(tag, rows):
    rows=[r for r in rows if r]
    if not rows: print(f"{tag:34s} (no data)"); return
    agg={k:sum(r[k] for r in rows) for k in rows[0]}
    nbad=lambda k: sum(1 for r in rows if r[k]>0)
    print(f"{tag:34s} f={agg['faces']:>9,}  bnd*={agg['bnd']:>6,}({nbad('bnd'):>3})  "
          f"NM={agg['nm']:>5,}({nbad('nm'):>3})  bow={agg['bow']:>4,}({nbad('bow'):>3})  "
          f"wind={agg['wind']:>5,}({nbad('wind'):>3})  selfX={agg['sx']:>6,}({nbad('sx'):>3})")

B=(28941,28758,29341,29158)
t=Transformer.from_crs("EPSG:3414","EPSG:4326",always_xy=True)
lo,la=t.transform([B[0],B[2]],[B[1],B[3]])
pieces=extract_domain_buildings(domain_leaf_tiles(min(lo),min(la),max(lo),max(la)),
                                box(*B), store_dir="data/onemap_store")[:N]
print(f"{len(pieces)} pieces.  bnd* = boundary edges EXCLUDING the by-design cut plane")
print("(N) = how many buildings show that defect at all\n")

A=defaultdict(list)
for p in pieces:
    A["1 raw from OneMap"].append(audit(p["verts"],p["faces"]))
    v,f=seal_piece(p["verts"],p["faces"])
    A["2 after seal_piece"].append(audit(v,f))
    src=mn.meshFromFacesVerts(np.asarray(f,np.int32),np.asarray(v,float))
    g=mr.meshToLevelSet(mr.MeshPart(src),mr.AffineXf3f(),mr.Vector3f(VS,VS,VS),3.0)
    st=mr.GridToMeshSettings(); st.voxelSize=mr.Vector3f(VS,VS,VS); st.isoValue=0.0; st.adaptivity=0.0
    r=mr.gridToMesh(g,st)
    rv,rf=mn.getNumpyVerts(r),np.asarray(mn.getNumpyFaces(r.topology))
    A["3 after DMC remesh"].append(audit(rv,rf))
    ds=mr.DecimateSettings(); ds.maxError=DEC_ERR; mr.decimateMesh(r,ds)
    rv,rf=mn.getNumpyVerts(r),np.asarray(mn.getNumpyFaces(r.topology))
    A["4 after decimate"].append(audit(rv,rf))
    m=trimesh.Trimesh(rv,rf,process=False)
    try:
        c=m.bounding_box.centroid
        m.apply_translation(-c); m.apply_scale([1-SHRINK/max(m.extents[0],1e-6),
                                                1-SHRINK/max(m.extents[1],1e-6),1.0])
        m.apply_translation(c)
    except Exception: pass
    A["5 after XY shrink"].append(audit(m.vertices,m.faces))
    zc=float(m.vertices[:,2].min())+DZ
    sh=m.slice_plane([0,0,zc],[0,0,1],cap=False)
    if sh is None or len(sh.faces)==0: continue
    sh=trimesh.Trimesh(sh.vertices.copy(),sh.faces.copy(),process=False); sh.merge_vertices()
    A["6 after slice (open bottom)"].append(audit(sh.vertices,sh.faces,cut_z=zc))
for k in sorted(A): show(k,A[k])
