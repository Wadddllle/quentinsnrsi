"""Do slice rings overlap each other? Overlapping PSLG segments break `triangle`,
so this decides whether individual rings can be constraints (exact seam) or must
be unioned into compounds."""
import sys, numpy as np, trimesh
sys.path.insert(0,"/home/quentin/snrsi")
from shapely.geometry import box, Polygon
from shapely.strtree import STRtree
from pyproj import Transformer
from sbg.onemap_native import extract as EX
from sbg.onemap_native.tiles import domain_leaf_tiles
import meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn

def rings_of(p, VS=0.25, DZ=1.0):
    sv,sf=EX.seal_piece(p["verts"],p["faces"])
    m0=trimesh.Trimesh(np.asarray(sv),np.asarray(sf),process=True); m0.merge_vertices()
    src=mn.meshFromFacesVerts(np.asarray(m0.faces,np.int32),np.asarray(m0.vertices,float))
    g=mr.meshToLevelSet(mr.MeshPart(src),mr.AffineXf3f(),mr.Vector3f(VS,VS,VS),3.0)
    st=mr.GridToMeshSettings(); st.voxelSize=mr.Vector3f(VS,VS,VS); st.isoValue=0.0; st.adaptivity=0.0
    res=mr.gridToMesh(g,st)
    v=mn.getNumpyVerts(res); f=np.asarray(mn.getNumpyFaces(res.topology))
    m=trimesh.Trimesh(v,f,process=False); m.merge_vertices()
    zc=float(m.vertices[:,2].min())+DZ
    sec=m.section(plane_origin=[0,0,zc],plane_normal=[0,0,1])
    if sec is None: return None,None,None
    polys=[]
    for e in sec.entities:
        pts=sec.vertices[e.points][:,:2]
        if len(pts)<4: continue
        q=Polygon(pts)
        if not q.is_valid: q=q.buffer(0)
        if q.is_valid and q.area>0.5: polys.append(q)
    return polys, m, zc

B=(28941,28758,29341,29158)
tf=Transformer.from_crs("EPSG:3414","EPSG:4326",always_xy=True)
dom=box(*B); lo,la=tf.transform([B[0],B[2]],[B[1],B[3]])
pieces=EX.extract_domain_buildings(domain_leaf_tiles(min(lo),min(la),max(lo),max(la)),dom,
                                   store_dir="/home/quentin/snrsi/data/onemap_store")
allp=[]; owner=[]
for i,p in enumerate(pieces[:120]):
    r,_,_=rings_of(p)
    if not r: continue
    for q in r: allp.append(q); owner.append(i)
tree=STRtree(allp)
ov=0; pairs=set(); areas=[]
for i,q in enumerate(allp):
    for j in tree.query(q):
        j=int(j)
        if j<=i or owner[j]==owner[i]: continue
        inter=q.intersection(allp[j])
        if inter.area>1e-6:
            pairs.add((i,j)); areas.append(inter.area)
print(f"rings: {len(allp)} from {len(set(owner))} buildings")
print(f"overlapping ring PAIRS (different buildings): {len(pairs)}")
if areas:
    a=np.array(areas)
    print(f"  overlap area m2: median={np.median(a):.3f} p90={np.percentile(a,90):.2f} max={a.max():.2f}")
    print(f"  rings involved in an overlap: {len(set([x for pr in pairs for x in pr]))}/{len(allp)}")
