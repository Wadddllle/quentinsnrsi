"""Standard Marching Cubes (manifold by construction) vs Dual MC, per building."""
import sys, time, collections, numpy as np, trimesh
sys.path.insert(0,"/home/quentin/snrsi")
from shapely.geometry import box
from pyproj import Transformer
from sbg.onemap_native import extract as EX
from sbg.onemap_native.tiles import domain_leaf_tiles
import meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
print("MeshToVolumeParams.Type:", [a for a in dir(mr.MeshToVolumeParams.Type) if not a.startswith('_')])

def diag(v,f):
    m=trimesh.Trimesh(v,f,process=False); m.merge_vertices()
    e=np.sort(m.edges_sorted,axis=1); c=collections.Counter(collections.Counter(map(tuple,e)).values())
    return c.get(1,0), sum(x for k,x in c.items() if k>2), len(m.split(only_watertight=False)), m
def sx(v,f):
    mm=mn.meshFromFacesVerts(np.asarray(f,np.int32),np.asarray(v,float))
    try: return mr.findSelfCollidingTriangles(mr.MeshPart(mm)).size()
    except Exception: return -1

B=(28941,28758,29341,29158)
tf=Transformer.from_crs("EPSG:3414","EPSG:4326",always_xy=True)
dom=box(*B); lo,la=tf.transform([B[0],B[2]],[B[1],B[3]])
pieces=EX.extract_domain_buildings(domain_leaf_tiles(min(lo),min(la),max(lo),max(la)),dom,
                                   store_dir="/home/quentin/snrsi/data/onemap_store")
sel=sorted(pieces,key=lambda p:-float(np.ptp(p["verts"][:,2])))[:25]
VS=0.25
for label in ("DualMC","StdMC"):
    t0=time.time(); wt=0; nm=0; op=0; comp=0; sxt=0; nf=[]; ve=[]; ok=0
    for p in sel:
        sv,sf=EX.seal_piece(p["verts"],p["faces"])
        m0=trimesh.Trimesh(np.asarray(sv),np.asarray(sf),process=True); m0.merge_vertices()
        src=mn.meshFromFacesVerts(np.asarray(m0.faces,np.int32),np.asarray(m0.vertices,float))
        try:
            if label=="DualMC":
                g=mr.meshToLevelSet(mr.MeshPart(src),mr.AffineXf3f(),mr.Vector3f(VS,VS,VS),3.0)
                st=mr.GridToMeshSettings(); st.voxelSize=mr.Vector3f(VS,VS,VS)
                st.isoValue=0.0; st.adaptivity=0.0
                res=mr.gridToMesh(g,st)
            else:
                pr=mr.MeshToVolumeParams(); pr.type=mr.MeshToVolumeParams.Type.Signed
                pr.voxelSize=mr.Vector3f(VS,VS,VS); pr.surfaceOffset=3.0
                pr.outXf=mr.AffineXf3f()
                vol=mr.meshToVolume(mr.MeshPart(src),pr)
                mc=mr.MarchingCubesParams(); mc.iso=0.0; mc.lessInside=True
                res=mr.marchingCubes(vol,mc)
                res.transform(pr.outXf.inverse())
            v=mn.getNumpyVerts(res); f=np.asarray(mn.getNumpyFaces(res.topology))
        except Exception as e:
            print("  fail:",repr(e)[:90]); continue
        o,n,c,mm=diag(v,f); ok+=1
        wt+=bool(mm.is_watertight); nm+=n; op+=o; comp+=(c>1); sxt+=max(sx(v,f),0)
        nf.append(len(mm.faces))
        if m0.volume>0: ve.append(abs(mm.volume-m0.volume)/m0.volume*100)
    print(f"{label:<7} ok={ok:>3} watertight={wt:>3}/25  nonManifEdges={nm:>5}  openEdges={op:>4}  "
          f"multiComp={comp:>3}  selfX={sxt:>4}  faces/bldg={int(np.mean(nf)) if nf else 0:>8,}  "
          f"volErr={np.mean(ve) if ve else 0:.2f}%  {time.time()-t0:.0f}s")
