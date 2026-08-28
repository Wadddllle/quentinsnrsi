"""meshlib PURE voxel remesh (meshToLevelSet -> gridToMesh @ iso 0), per building.
No offset => no corner rounding. 'adaptivity' coarsens flat walls."""
import sys, time, resource, numpy as np, trimesh
sys.path.insert(0,"/home/quentin/snrsi")
from shapely.geometry import box
from pyproj import Transformer
from sbg.onemap_native import extract as EX
from sbg.onemap_native.tiles import domain_leaf_tiles
import meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn

def sx(v,f):
    m=mn.meshFromFacesVerts(np.asarray(f,np.int32),np.asarray(v,float))
    try: return mr.findSelfCollidingTriangles(mr.MeshPart(m)).size()
    except Exception: return -1

B=(28941,28758,29341,29158)
tf=Transformer.from_crs("EPSG:3414","EPSG:4326",always_xy=True)
dom=box(*B); lo,la=tf.transform([B[0],B[2]],[B[1],B[3]])
pieces=EX.extract_domain_buildings(domain_leaf_tiles(min(lo),min(la),max(lo),max(la)),dom,
                                   store_dir="/home/quentin/snrsi/data/onemap_store")
sel=sorted(pieces,key=lambda p:-float(np.ptp(p["verts"][:,2])))[:25]
print(f"{'voxel':>6} {'adapt':>6} {'ok':>4} {'wtight':>7} {'selfX':>6} {'t/bldg':>8} "
      f"{'peakRAM':>8} {'faces/bldg':>11} {'vol err':>9} {'bbox err':>9}")
for vs,ad in ((0.5,0.0),(0.5,0.5),(0.25,0.0),(0.25,0.5),(0.25,1.0),(0.1,0.5)):
    t0=time.time(); ok=wt=0; sxt=0; nf=[]; ve=[]; be=[]
    for p in sel:
        sv,sf=EX.seal_piece(p["verts"],p["faces"])
        m0=trimesh.Trimesh(np.asarray(sv),np.asarray(sf),process=True); m0.merge_vertices()
        src=mn.meshFromFacesVerts(np.asarray(m0.faces,np.int32),np.asarray(m0.vertices,float))
        try:
            grid=mr.meshToLevelSet(mr.MeshPart(src), mr.AffineXf3f(),
                                   mr.Vector3f(vs,vs,vs), 3.0)
            st=mr.GridToMeshSettings(); st.voxelSize=mr.Vector3f(vs,vs,vs)
            st.isoValue=0.0; st.adaptivity=ad
            res=mr.gridToMesh(grid, st)
            v=mn.getNumpyVerts(res); f=np.asarray(mn.getNumpyFaces(res.topology))
        except Exception as e:
            continue
        mm=trimesh.Trimesh(v,f,process=False); mm.merge_vertices()
        ok+=1; wt+=bool(mm.is_watertight); nf.append(len(mm.faces)); sxt+=max(sx(v,f),0)
        if m0.volume>0: ve.append(abs(mm.volume-m0.volume)/m0.volume*100)
        be.append(float(np.abs(np.ptp(mm.vertices,axis=0)-np.ptp(m0.vertices,axis=0)).max()))
    el=time.time()-t0
    ram=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024
    if ok:
        print(f"{vs:>6.2f} {ad:>6.1f} {ok:>4} {wt:>7} {sxt:>6} {el/ok:>7.2f}s {ram:>7.0f}M "
              f"{int(np.mean(nf)):>11,} {np.mean(ve):>8.2f}% {np.mean(be):>8.2f}m")
