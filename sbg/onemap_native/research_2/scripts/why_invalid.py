import sys, os; sys.path.insert(0,"/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np, trimesh
from shapely.geometry import box, Polygon
from pyproj import Transformer
import meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
from sbg.onemap_native.tiles import domain_leaf_tiles
from sbg.onemap_native.extract import extract_domain_buildings, seal_piece
VS=0.5; DEC=0.25; SHRINK=0.05; DZ=1.0
B=(28941,28758,29341,29158)
t=Transformer.from_crs("EPSG:3414","EPSG:4326",always_xy=True)
lo,la=t.transform([B[0],B[2]],[B[1],B[3]])
pieces=extract_domain_buildings(domain_leaf_tiles(min(lo),min(la),max(lo),max(la)),
                                box(*B), store_dir="data/onemap_store")
ninv=0; rep=0; cross=0
for p in pieces:
    v,f=seal_piece(p["verts"],p["faces"])
    src=mn.meshFromFacesVerts(np.asarray(f,np.int32),np.asarray(v,float))
    g=mr.meshToLevelSet(mr.MeshPart(src),mr.AffineXf3f(),mr.Vector3f(VS,VS,VS),3.0)
    st=mr.GridToMeshSettings(); st.voxelSize=mr.Vector3f(VS,VS,VS); st.isoValue=0.0; st.adaptivity=0.0
    r=mr.gridToMesh(g,st)
    ds=mr.DecimateSettings(); ds.maxError=DEC; mr.decimateMesh(r,ds)
    m=trimesh.Trimesh(mn.getNumpyVerts(r),np.asarray(mn.getNumpyFaces(r.topology)),process=False)
    try:
        c=m.bounding_box.centroid
        m.apply_translation(-c); m.apply_scale([1-SHRINK/max(m.extents[0],1e-6),
                                                1-SHRINK/max(m.extents[1],1e-6),1.0]); m.apply_translation(c)
    except Exception: pass
    zc=float(m.vertices[:,2].min())+DZ
    sh=m.slice_plane([0,0,zc],[0,0,1],cap=False)
    if sh is None or len(sh.faces)==0: continue
    sh=trimesh.Trimesh(sh.vertices.copy(),sh.faces.copy(),process=False); sh.merge_vertices()
    try: ol=sh.outline()
    except Exception: continue
    for e in ol.entities:
        pts=ol.vertices[e.points]
        if len(pts)<4: continue
        if abs(float(np.median(pts[:,2]))-zc)>0.25: continue
        q=Polygon(pts[:,:2])
        if q.is_valid: continue
        ninv+=1
        keys=[(round(float(a),6),round(float(b),6)) for a,b,_ in pts]
        # drop the closing duplicate
        if keys[0]==keys[-1]: keys=keys[:-1]
        has_rep = len(keys)!=len(set(keys))
        if has_rep: rep+=1
        else: cross+=1
print(f"\ninvalid loops: {ninv}")
print(f"  with a REPEATED vertex (self-TOUCHING -> splittable exactly): {rep}")
print(f"  with NO repeated vertex (self-CROSSING -> needs a new intersection vertex): {cross}")
