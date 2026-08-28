import sys, os; sys.path.insert(0,"/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
"""Per-building meshlib Dual-MC remesh: how clean is the RAW isosurface output,
before any slicing/CDT? Directly tests the defect class the CGAL/GSoC report targets."""
import numpy as np, meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
from shapely.geometry import box
from sbg.onemap_native.tiles import domain_leaf_tiles
from sbg.onemap_native.extract import extract_domain_buildings, seal_piece
from pyproj import Transformer
VS=0.5
B=(21950,30250,22850,31150)
t=Transformer.from_crs("EPSG:3414","EPSG:4326",always_xy=True)
lo,la=t.transform([B[0],B[2]],[B[1],B[3]])
poly=box(*B)
pieces=extract_domain_buildings(domain_leaf_tiles(min(lo),min(la),max(lo),max(la)),
                                poly, store_dir="data/onemap_store")
print(f"{len(pieces)} pieces")

def strict(m):
    v=mn.getNumpyVerts(m); f=np.asarray(mn.getNumpyFaces(m.topology))
    _,inv=np.unique(np.round(v,4),axis=0,return_inverse=True)
    wf=inv.reshape(-1)[f]
    wf=wf[(wf[:,0]!=wf[:,1])&(wf[:,1]!=wf[:,2])&(wf[:,0]!=wf[:,2])]
    ed=np.sort(np.vstack([wf[:,[0,1]],wf[:,[1,2]],wf[:,[0,2]]]),axis=1)
    _,c=np.unique(ed,axis=0,return_counts=True)
    return int((c==1).sum()), int((c>2).sum())

tot_nm=tot_open=tot_sx=tot_holes=n=0
for p in pieces[:60]:
    v,f=seal_piece(p["verts"],p["faces"])
    src=mn.meshFromFacesVerts(np.asarray(f,np.int32),np.asarray(v,float))
    g=mr.meshToLevelSet(mr.MeshPart(src),mr.AffineXf3f(),mr.Vector3f(VS,VS,VS),3.0)
    st=mr.GridToMeshSettings(); st.voxelSize=mr.Vector3f(VS,VS,VS)
    st.isoValue=0.0; st.adaptivity=0.0
    r=mr.gridToMesh(g,st)
    o,nm=strict(r)
    try: sx=mr.findSelfCollidingTriangles(mr.MeshPart(r)).size()
    except Exception: sx=-1
    h=len(r.topology.findHoleRepresentiveEdges())
    c=len(mr.MeshComponents.getAllComponents(mr.MeshPart(r)))
    tot_nm+=nm; tot_open+=o; tot_sx+=max(sx,0); tot_holes+=h; n+=1
    if nm or o or sx or h:
        print(f"  bldg {n:3d}: faces={r.topology.numValidFaces():>7,} holes={h} comps={c} "
              f"open={o} NONMANIFOLD={nm} selfX={sx}")
print(f"\nTOTAL over {n} buildings: nonManifoldEdges={tot_nm}  openEdges={tot_open}  "
      f"holes={tot_holes}  selfIntersectingTris={tot_sx}")
