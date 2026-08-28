"""WHY is the remeshed output not watertight?  Open edges, non-manifold, or
components -- and is the INPUT even closed?"""
import sys, collections, numpy as np, trimesh
sys.path.insert(0,"/home/quentin/snrsi")
from shapely.geometry import box
from pyproj import Transformer
from sbg.onemap_native import extract as EX
from sbg.onemap_native.tiles import domain_leaf_tiles
import meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn

def diag(m):
    e=np.sort(m.edges_sorted,axis=1); c=collections.Counter(collections.Counter(map(tuple,e)).values())
    return c.get(1,0), sum(v for k,v in c.items() if k>2), len(m.split(only_watertight=False))

B=(28941,28758,29341,29158)
tf=Transformer.from_crs("EPSG:3414","EPSG:4326",always_xy=True)
dom=box(*B); lo,la=tf.transform([B[0],B[2]],[B[1],B[3]])
pieces=EX.extract_domain_buildings(domain_leaf_tiles(min(lo),min(la),max(lo),max(la)),dom,
                                   store_dir="/home/quentin/snrsi/data/onemap_store")
sel=sorted(pieces,key=lambda p:-float(np.ptp(p["verts"][:,2])))[:25]
in_closed=0; rows=[]
for i,p in enumerate(sel):
    sv,sf=EX.seal_piece(p["verts"],p["faces"])
    m0=trimesh.Trimesh(np.asarray(sv),np.asarray(sf),process=True); m0.merge_vertices()
    o0,n0,c0=diag(m0); in_closed += (o0==0 and n0==0)
    src=mn.meshFromFacesVerts(np.asarray(m0.faces,np.int32),np.asarray(m0.vertices,float))
    grid=mr.meshToLevelSet(mr.MeshPart(src),mr.AffineXf3f(),mr.Vector3f(.25,.25,.25),3.0)
    st=mr.GridToMeshSettings(); st.voxelSize=mr.Vector3f(.25,.25,.25); st.isoValue=0.0; st.adaptivity=0.0
    res=mr.gridToMesh(grid,st)
    v=mn.getNumpyVerts(res); f=np.asarray(mn.getNumpyFaces(res.topology))
    # meshlib's OWN view of the result
    holes=len(res.topology.findHoleRepresentiveEdges())
    mm=trimesh.Trimesh(v,f,process=False); mm.merge_vertices()
    o,n,c=diag(mm)
    rows.append((o0,n0,c0,holes,o,n,c,len(mm.faces),mm.is_watertight))
print(f"INPUT sealed pieces fully closed: {in_closed}/25")
print(f"{'#':>2} | INPUT open nm comp | meshlib holes | OUT open nm comp faces wt")
for i,r in enumerate(rows[:14],1):
    print(f"{i:>2} | {r[0]:>9} {r[1]:>2} {r[2]:>4} | {r[3]:>13} | {r[4]:>8} {r[5]:>2} {r[6]:>4} {r[7]:>6,} {str(r[8])}")
import numpy as _n
print(f"\nmeshlib holes==0 on output: {sum(1 for r in rows if r[3]==0)}/25")
print(f"trimesh watertight        : {sum(1 for r in rows if r[8])}/25")
print(f"outputs with >1 component : {sum(1 for r in rows if r[6]>1)}/25")
