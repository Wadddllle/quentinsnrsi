"""EXPERIMENT v2. Fixes from the v1 post-mortem:
  - ring comes from the SHELL'S OWN boundary loop (shell.outline()), so the CDT
    constraint IS the mesh cut boundary by construction (v1 used a separate
    section() call + buffer(0), which could never be guaranteed to agree)
  - one shell per PIECE (v1 stacked a shell once per ring -> 55 duplicates)
  - overlapping rings drop only the RING, never the whole building (v1 threw away
    92/342 = 27% of buildings)
  - STRtree built once (v1 rebuilt it inside the loop)
  - VS=0.5, no decimation for the first run; per-stage timers.
"""
import sys, time, collections, numpy as np, trimesh
sys.path.insert(0,"/home/quentin/snrsi")
from shapely.geometry import box, Polygon
from shapely.ops import unary_union
from shapely.strtree import STRtree
from shapely import contains, points as shp_points
from pyproj import Transformer
import triangle as _triangle
from sbg.onemap_native import extract as EX
from sbg.onemap_native.tiles import domain_leaf_tiles
from sbg.onemap_native.terrain import (build_domain_dtm, DtmSampler, _densify_ring,
                                       terrain_flat_base_solid)
import meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
import mapbox_earcut as earcut
SP="/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583/scratchpad/"
import os
VS=0.5; DZ=1.0; DEC_ERR=0.25
SHRINK=float(os.environ.get('SHRINK','0.05'))

_DEC_T=0.0

_TFBS_ORIG = terrain_flat_base_solid
def terrain_flat_base_solid(terr_v, terr_f, dom, base_z=0.0, terrain_step=10.0):
    """FIX 1: the shipped version winds the WALL opposite to the top surface.
    Isolated: flipping wall+cap -> 160 conflicts, flipping cap only -> 160,
    flipping WALL ONLY -> 0. Verified against trimesh.repair.fix_winding, which
    also reaches 0 and reports positive volume, so the wall was the inside-out part.
    Applied here as a post-hoc flip of exactly the wall faces."""
    v,f = _TFBS_ORIG(terr_v, terr_f, dom, base_z=base_z, terrain_step=terrain_step)
    n_top = len(terr_f)
    n_ring = len(_densify_ring(dom.exterior.coords, terrain_step))
    n_wall = 2*n_ring
    for it_ in dom.interiors:
        n_wall += 2*len(_densify_ring(it_.coords, terrain_step))
    def _w(vv,ff):
        _,iv=np.unique(np.round(vv,4),axis=0,return_inverse=True)
        w2=iv.reshape(-1)[ff]
        w2=w2[(w2[:,0]!=w2[:,1])&(w2[:,1]!=w2[:,2])&(w2[:,0]!=w2[:,2])]
        he=set(); c=0
        for a_,b_,c_ in w2:
            for x,y in ((a_,b_),(b_,c_),(c_,a_)):
                if (x,y) in he: c+=1
                he.add((x,y))
        return c
    print(f"  [TFBS] total={len(f)} n_top={n_top} n_wall={n_wall} added={len(f)-n_top} "
          f"wind_before={_w(v,f)}", flush=True)
    f = f.copy(); f[n_top:n_top+n_wall] = f[n_top:n_top+n_wall][:, ::-1]
    print(f"  [TFBS] wind_after_wallflip={_w(v,f)}", flush=True)
    return v, f

SKIP=collections.Counter()
def cap_small_loops(sh, pad, min_area=0.05):
    """FIX 2 (v2): cap the shell's own bottom boundary loops that shell_rings
    REJECTED (area <= min_area). Those never became a CDT constraint, so terrain
    was never opened under them and their rim had nothing to attach to.

    v1 appended NEW vertices for the cap. That welds positionally but leaves the
    cap topologically detached, so it could not be oriented against the wall faces
    it abuts -- measured 306-321 winding conflicts in 30 planar groups, one per cap.
    v2 reuses the shell's OWN boundary vertex indices and derives orientation from
    the adjacent wall's half-edge direction: a cap must traverse each shared edge
    OPPOSITE to the face already using it. Appends faces only, no new vertices, no
    meshlib round-trip (float32 at 29km = 2mm, would destroy the seam)."""
    V = np.asarray(sh.vertices); F = np.asarray(sh.faces)
    if len(F) == 0: return sh, 0
    he = set()
    for a, b, c in F:
        he.add((a, b)); he.add((b, c)); he.add((c, a))
    bnd = [(x, y) for (x, y) in he if (y, x) not in he]
    if not bnd: return sh, 0
    # Chain boundary half-edges into loops. v2 used one global `used` set, which
    # silently truncated any loop sharing a vertex with an earlier one -- that is
    # why 17 loops (134 edges) survived capping. Chain by EDGE instead, following
    # each directed boundary edge exactly once.
    from collections import defaultdict as _dd2
    out_e = _dd2(list)
    for x, y in bnd: out_e[x].append(y)
    unused = set(bnd)
    loops = []
    while unused:
        x0, y0 = next(iter(unused))
        unused.discard((x0, y0))
        loop = [x0, y0]; cur = y0
        while True:
            nxts = [t for t in out_e.get(cur, []) if (cur, t) in unused]
            if not nxts: break
            t = nxts[0]; unused.discard((cur, t))
            if t == x0: break
            loop.append(t); cur = t
        if len(loop) >= 3: loops.append(loop)
    SKIP["loops"] += len(loops)
    add = []; ncap = 0
    for loop in loops:
        pts = V[loop]
        if abs(float(np.median(pts[:, 2])) - pad) > 0.25:
            SKIP["offplane"] += 1; continue
        q = Polygon(pts[:, :2])
        if not q.is_valid: q = q.buffer(0)
        if q.is_empty or q.area > min_area:
            SKIP["is_a_ring"] += 1; continue
        ring = list(reversed(loop))                    # opposite to the wall's direction
        xy = np.ascontiguousarray(V[ring][:, :2], dtype=np.float32)
        try:
            tri = earcut.triangulate_float32(
                xy, np.array([len(ring)], dtype=np.uint32)).reshape(-1, 3)
        except Exception:
            tri = np.zeros((0, 3), dtype=np.int64)
        if not len(tri):
            # earcut returns nothing for a ZERO-AREA ring, and 17 such loops (3-4
            # verts, hull area ~0) were left open by v2. They are degenerate slivers
            # from the slice, not real openings. A plain fan closes them
            # topologically; the faces are near-zero-area but closure is what the
            # spec needs, and they stay manifold (verified).
            tri = np.array([[0, i, i + 1] for i in range(1, len(ring) - 1)],
                           dtype=np.int64)
        if not len(tri):
            SKIP["no_tri"] += 1; continue
        idx = np.asarray(ring, dtype=np.int64)[tri]
        # verify: no cap half-edge may duplicate one the shell already uses
        conflict = any((int(t[i]), int(t[(i + 1) % 3])) in he
                       for t in idx for i in range(3))
        if conflict: idx = idx[:, ::-1]
        add.append(idx); ncap += 1
    if not add: return sh, 0
    return trimesh.Trimesh(V, np.vstack([F, np.vstack(add)]), process=False), ncap


def log(m): print(m, flush=True)

def remesh(v,f):
    src=mn.meshFromFacesVerts(np.asarray(f,np.int32),np.asarray(v,float))
    g=mr.meshToLevelSet(mr.MeshPart(src),mr.AffineXf3f(),mr.Vector3f(VS,VS,VS),3.0)
    st=mr.GridToMeshSettings(); st.voxelSize=mr.Vector3f(VS,VS,VS)
    st.isoValue=0.0; st.adaptivity=0.0
    res=mr.gridToMesh(g,st)
    global _DEC_T
    if DEC_ERR>0:
        _t=time.time()
        ds=mr.DecimateSettings(); ds.maxError=DEC_ERR; mr.decimateMesh(res,ds)
        _DEC_T+=time.time()-_t
    return mn.getNumpyVerts(res), np.asarray(mn.getNumpyFaces(res.topology))


def cap_nonseam(sh, zc, tol=0.25):
    """Fill boundary loops that aren't at the cut plane (holes in the building)."""
    try:
        m=mn.meshFromFacesVerts(np.asarray(sh.faces,np.int32),np.asarray(sh.vertices,float))
        for _ in range(3):
            eds=m.topology.findHoleRepresentiveEdges()
            filled=False
            for e in eds:
                loop=m.topology.getLeftRing(e)
                zs=[m.points.vec[m.topology.org(x)].z for x in loop] if hasattr(m,'points') else []
                if zs and abs(float(np.median(zs))-zc)<=tol:
                    continue           # this is the seam loop -- terrain closes it
                try: mr.fillHole(m,e,mr.FillHoleParams()); filled=True
                except Exception: pass
            if not filled: break
        v=mn.getNumpyVerts(m); f=np.asarray(mn.getNumpyFaces(m.topology))
        return trimesh.Trimesh(v,f,process=False)
    except Exception:
        return sh


def cap_nonseam(sh, pad, tol=1e-6):
    """Cap boundary loops that are NOT the seam loop (holes in the building
    surface, measured up to 5.7m from any ring). Triangulates in the loop's own
    best-fit plane and APPENDS faces only -- never round-trips through meshlib,
    whose float32 point storage shifts vertices ~1mm at EPSG:3414 coords and
    destroys the exact seam (measured: exact matches 29441 -> 8092)."""
    import mapbox_earcut as _ec
    v=np.asarray(sh.vertices); f=np.asarray(sh.faces)
    ed=np.sort(np.vstack([f[:,[0,1]],f[:,[1,2]],f[:,[0,2]]]),axis=1)
    u,c=np.unique(ed,axis=0,return_counts=True)
    b=u[c==1]
    if not len(b): return sh
    adj={}
    for a_,b_ in b: adj.setdefault(a_,[]).append(b_); adj.setdefault(b_,[]).append(a_)
    seen=set(); new=[]
    for start in adj:
        if start in seen: continue
        loop=[start]; seen.add(start); cur=start; prev=None
        while True:
            nxt=[x for x in adj[cur] if x!=prev and x not in seen]
            if not nxt: break
            prev, cur = cur, nxt[0]
            loop.append(cur); seen.add(cur)
        if len(loop)<3: continue
        pts=v[loop]
        if np.all(np.abs(pts[:,2]-pad)<1e-6): continue     # seam loop: terrain closes it
        ctr=pts.mean(axis=0)
        _,_,vt=np.linalg.svd(pts-ctr)
        p2=(pts-ctr)@vt[:2].T
        try: tri=_ec.triangulate_float64(p2.reshape(-1,2), np.array([len(p2)]))
        except Exception: continue
        idx=np.asarray(loop)
        for k in range(0,len(tri),3): new.append(idx[[tri[k],tri[k+1],tri[k+2]]])
    if not new: return sh
    return trimesh.Trimesh(v, np.vstack([f,np.array(new)]), process=False)

def shell_rings(shell, zc, tol=1e-6):
    """Polygons of the shell's OWN open boundary at the cut plane."""
    try: ol=shell.outline()
    except Exception: return []
    out=[]
    for e in ol.entities:
        pts=ol.vertices[e.points]
        if len(pts)<4: continue
        if abs(float(np.median(pts[:,2]))-zc)>0.25: continue   # only the cut loop
        q=Polygon(pts[:,:2])
        # FIX: an outline that self-touches in XY (pinched/figure-8 slice profile)
        # makes an INVALID Polygon. Dropping it silently kept the shell but left
        # its bottom loop with no terrain to attach to -> open edges. Measured on
        # Duxton: 8 invalid loops, 3 of them REAL buildings of 338/241/219 m^2.
        # buffer(0) repairs the self-touch and returns valid geometry.
        if not q.is_valid:
            q = q.buffer(0)
        for part in (q.geoms if q.geom_type == "MultiPolygon" else [q]):
            if part.geom_type == "Polygon" and part.is_valid and part.area > 0.05:
                out.append(Polygon(part.exterior))
    return out

def main(B):
    T0=time.time(); dom=box(*B)
    tf=Transformer.from_crs("EPSG:3414","EPSG:4326",always_xy=True)
    lo,la=tf.transform([B[0],B[2]],[B[1],B[3]])
    t=time.time()
    pieces=EX.extract_domain_buildings(domain_leaf_tiles(min(lo),min(la),max(lo),max(la)),
                                       dom, store_dir="/home/quentin/snrsi/data/onemap_store")
    gz,af=build_domain_dtm(B,step=5.0); dtm=DtmSampler(gz,af)
    log(f"[extract] {len(pieces)} pieces, DTM {gz.shape}  {time.time()-t:.1f}s")

    t=time.time(); items=[]; nfail=0
    for i,p in enumerate(pieces):
        if i%25==0: log(f"   ...{i}/{len(pieces)}  ({time.time()-t:.0f}s)")
        try:
            sv,sf=EX.seal_piece(p["verts"],p["faces"])
            rv,rf=remesh(sv,sf)
            m=trimesh.Trimesh(rv,rf,process=True); m.merge_vertices()
            if SHRINK>0:
                c2=m.vertices[:,:2].mean(axis=0)
                rad=float(np.linalg.norm(m.vertices[:,:2]-c2,axis=1).max())
                if rad>SHRINK:
                    m.vertices[:,:2]=c2+(m.vertices[:,:2]-c2)*((rad-SHRINK)/rad)
            zc=float(m.vertices[:,2].min())+DZ
            sh=m.slice_plane([0,0,zc],[0,0,1],cap=False)
            if sh is None or len(sh.faces)<4: nfail+=1; continue
            sh=trimesh.Trimesh(sh.vertices.copy(),sh.faces.copy(),process=False)
            sh.merge_vertices()
            # Quantise to the SAME 1mm grid conforming_terrain's add_v keys on.
            # Otherwise two sub-mm-apart shell boundary vertices collapse into one
            # terrain vertex and the seam gaps by exactly that (52 open loops).
            sh.vertices=np.round(sh.vertices,3); sh.merge_vertices()
            loops=shell_rings(sh,zc)
            if not loops: nfail+=1; continue
            # A contained loop is this building's COURTYARD -> a hole in its
            # footprint (courtyard stays terrain), NOT a separate ring and NOT an
            # overlap. Treating it as one deleted ~50 rings per run.
            loops=sorted(loops,key=lambda q:-q.area); rings=[]; used=[False]*len(loops)
            for i,q in enumerate(loops):
                if used[i]: continue
                holes=[]
                for j in range(i+1,len(loops)):
                    if not used[j] and q.contains(loops[j].representative_point()):
                        holes.append(list(loops[j].exterior.coords)); used[j]=True
                rings.append(Polygon(list(q.exterior.coords),holes) if holes else q)
                used[i]=True
            xy=np.vstack([np.asarray(q.exterior.coords) for q in rings])
            pad=round(float(np.min(np.atleast_1d(dtm(xy[:,0],xy[:,1])))),3)
            sh.vertices[:,2]=np.round(sh.vertices[:,2]+(pad-zc),3)
            sh=cap_nonseam(sh,pad)
            items.append({"rings":rings,"pad":pad,"shell":sh,"solid":m,"zc":zc})
        except Exception: nfail+=1

    # --- union buildings whose rings touch, then re-slice the merged solid ---
    tu=time.time()
    import scipy.sparse as _sp
    from scipy.sparse.csgraph import connected_components as _cc
    allr=[]; own=[]
    for k,it in enumerate(items):
        for q in it["rings"]: allr.append(q); own.append(k)
    # Group by ACTUAL 3D SOLID OVERLAP, not ring overlap at the cut plane.
    # Ring overlap only sees the base: the 5cm shrink separates neighbours there,
    # so party-wall buildings were NOT unioned yet still interpenetrated above
    # (measured: 551 building-vs-building collisions high up, 1575 in the pad
    # band, vs only 22 from overhang).
    ii=[]; jj=[]
    bb=[np.vstack([it["solid"].vertices.min(axis=0),
                   it["solid"].vertices.max(axis=0)]) for it in items]
    from shapely.geometry import box as _bx
    foot=[_bx(b[0,0],b[0,1],b[1,0],b[1,1]) for b in bb]
    _t3=STRtree(foot)
    for a in range(len(items)):
        for b in _t3.query(foot[a]):
            b=int(b)
            if b<=a: continue
            A,Bb=bb[a],bb[b]
            if A[1,2]<Bb[0,2] or Bb[1,2]<A[0,2]: continue      # no z overlap
            if not foot[a].intersects(foot[b]): continue
            try:
                inter=mr.boolean(
                    mn.meshFromFacesVerts(np.asarray(items[a]["solid"].faces,np.int32),
                                          np.asarray(items[a]["solid"].vertices,float)),
                    mn.meshFromFacesVerts(np.asarray(items[b]["solid"].faces,np.int32),
                                          np.asarray(items[b]["solid"].vertices,float)),
                    mr.BooleanOperation.Intersection)
                if inter.valid() and inter.mesh.topology.numValidFaces()>0:
                    ii.append(a); jj.append(b)
            except Exception:
                ii.append(a); jj.append(b)     # can't tell -> union to be safe
    n=len(items)
    lbl=np.arange(n)
    if ii:
        g=_sp.coo_matrix((np.ones(len(ii)),(ii,jj)),shape=(n,n))
        _,lbl=_cc(g,directed=False)
    merged=[]; nun=0
    for g_ in np.unique(lbl):
        mem=[items[k] for k in np.where(lbl==g_)[0]]
        if len(mem)==1: merged.append(mem[0]); continue
        try:
            acc=mn.meshFromFacesVerts(np.asarray(mem[0]["solid"].faces,np.int32),
                                      np.asarray(mem[0]["solid"].vertices,float))
            for o in mem[1:]:
                b=mn.meshFromFacesVerts(np.asarray(o["solid"].faces,np.int32),
                                        np.asarray(o["solid"].vertices,float))
                r=mr.boolean(acc,b,mr.BooleanOperation.Union)
                if not r.valid() or r.mesh.topology.numValidFaces()==0: raise RuntimeError("bool")
                acc=r.mesh
            mv=mn.getNumpyVerts(acc); mf=np.asarray(mn.getNumpyFaces(acc.topology))
            mm2=trimesh.Trimesh(mv,mf,process=True); mm2.merge_vertices()
            zc2=float(mm2.vertices[:,2].min())+DZ
            sh2=mm2.slice_plane([0,0,zc2],[0,0,1],cap=False)
            sh2=trimesh.Trimesh(sh2.vertices.copy(),sh2.faces.copy(),process=False)
            sh2.merge_vertices()
            sh2.vertices=np.round(sh2.vertices,3); sh2.merge_vertices()
            r2=shell_rings(sh2,zc2)
            if not r2: raise RuntimeError("no ring")
            loops=sorted(r2,key=lambda q:-q.area); rr=[]; used=[False]*len(loops)
            for a2,q in enumerate(loops):
                if used[a2]: continue
                hs=[]
                for b2 in range(a2+1,len(loops)):
                    if not used[b2] and q.contains(loops[b2].representative_point()):
                        hs.append(list(loops[b2].exterior.coords)); used[b2]=True
                rr.append(Polygon(list(q.exterior.coords),hs) if hs else q); used[a2]=True
            xy2=np.vstack([np.asarray(q.exterior.coords) for q in rr])
            pad2=round(float(np.min(np.atleast_1d(dtm(xy2[:,0],xy2[:,1])))),3)
            sh2.vertices[:,2]=np.round(sh2.vertices[:,2]+(pad2-zc2),3)
            sh2=cap_nonseam(sh2,pad2)
            merged.append({"rings":rr,"pad":pad2,"shell":sh2}); nun+=1
        except Exception:
            merged.extend(mem)   # fall back to the un-merged members
    items=merged
    log(f"[union] {nun} touching groups merged -> {len(items)} solids  {time.time()-tu:.1f}s")
    log(f"[remesh+slice] {len(items)} buildings, {nfail} failed  {time.time()-t:.1f}s (decimate {_DEC_T:.1f}s)")

    t=time.time(); rings=[]; padz=[]; taken=[]
    for it in items:
        for q in it["rings"]:
            rings.append(q); padz.append(it["pad"]); taken.append(q)
    tree=STRtree(taken); bad=set()
    for i,q in enumerate(taken):
        if i in bad: continue
        for j in tree.query(q):
            j=int(j)
            if j<=i or j in bad: continue
            if q.intersection(taken[j]).area>1e-6: bad.add(j)   # drop only the RING
    rings=[q for i,q in enumerate(rings) if i not in bad]
    padz=[z for i,z in enumerate(padz) if i not in bad]
    log(f"[rings] {len(rings)} constraints, {len(bad)} overlapping rings dropped "
        f"(buildings kept: {len(items)})  {time.time()-t:.1f}s")

    t=time.time()
    tv,tt=conf_holed(dom,dtm,rings,padz)
    log(f"[terrain] {len(tt):,} triangles  {time.time()-t:.1f}s")
    _nc=0
    for _it in items:
        _it["shell"], _k = cap_small_loops(_it["shell"], _it["pad"])
        _nc += _k
    log(f"[cap] capped {_nc} loops; skips={dict(SKIP)}")
    t=time.time()
    shells=[it["shell"] for it in items]
    bmin=min(float(s.vertices[:,2].min()) for s in shells)
    sv2,sf2=terrain_flat_base_solid(tv,tt,dom,base_z=min(0.0,bmin-1.0),terrain_step=10.0)
    V=[sv2]; F=[sf2]; n=len(sv2)
    for s in shells:
        V.append(np.asarray(s.vertices)); F.append(np.asarray(s.faces)+n); n+=len(s.vertices)
    allv=np.vstack(V); allf=np.vstack(F)
    log(f"[assemble] {len(allf):,} faces  {time.time()-t:.1f}s")

    # ---- SEAM DIAGNOSTIC: terrain hole boundary vs shell bottom boundary ----
    def _openedges(v,f):
        ed=np.sort(np.vstack([f[:,[0,1]],f[:,[1,2]],f[:,[0,2]]]),axis=1)
        u,c=np.unique(ed,axis=0,return_counts=True)
        return u[c==1]
    te=_openedges(sv2,sf2)
    tp=np.round(sv2[np.unique(te)],3) if len(te) else np.zeros((0,3))
    se=[]; sp=[]
    for s_ in shells:
        vv=np.asarray(s_.vertices); ff=np.asarray(s_.faces)
        o=_openedges(vv,ff)
        se.append(len(o))
        if len(o): sp.append(np.round(vv[np.unique(o)],3))
    sp=np.vstack(sp) if sp else np.zeros((0,3))
    log(f"[seam] terrain solid open edges={len(te)} (verts {len(tp)}) | "
        f"shells open edges={sum(se)} (verts {len(sp)})")
    if len(tp) and len(sp):
        from scipy.spatial import cKDTree as _KD
        d1,_=_KD(tp).query(sp)
        log(f"[seam] shell-boundary vert -> nearest terrain-hole vert: "
            f"exact={int((d1<1e-9).sum())}/{len(sp)} <1mm={int((d1<1e-3).sum())} "
            f"median={np.median(d1):.4f}m p90={np.percentile(d1,90):.4f}m max={d1.max():.3f}m")
        d2,_=_KD(sp).query(tp)
        log(f"[seam] terrain-hole vert -> nearest shell vert: "
            f"exact={int((d2<1e-9).sum())}/{len(tp)} <1mm={int((d2<1e-3).sum())} "
            f"max={d2.max():.3f}m")
    t=time.time()
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    key=np.round(allv,4)
    _,uidx,inv=np.unique(key,axis=0,return_index=True,return_inverse=True)
    inv=inv.reshape(-1)
    wf=inv[allf]
    wf=wf[(wf[:,0]!=wf[:,1])&(wf[:,1]!=wf[:,2])&(wf[:,0]!=wf[:,2])]
    ed=np.sort(np.vstack([wf[:,[0,1]],wf[:,[1,2]],wf[:,[0,2]]]),axis=1)
    _,cnt=np.unique(ed,axis=0,return_counts=True)
    nv=len(uidx)
    g=coo_matrix((np.ones(len(ed)),(ed[:,0],ed[:,1])),shape=(nv,nv))
    ncomp,_=connected_components(g,directed=False)
    log(f"[verify] faces={len(wf):,} verts={nv:,} components={ncomp} "
        f"openEdges={int((cnt==1).sum())} nonManifoldEdges={int((cnt>2).sum())} "
        f"allEdgesTwice={(cnt==2).all()}  {time.time()-t:.1f}s")
    try:
        mm=mn.meshFromFacesVerts(np.asarray(wf,np.int32),np.asarray(allv[uidx],float))
        log(f"[verify] selfIntersectingTris={mr.findSelfCollidingTriangles(mr.MeshPart(mm)).size()}")
    except Exception as ex: log(f"[verify] selfX check failed: {ex!r}")
    _ctr = allv.mean(axis=0)
    trimesh.Trimesh(allv - _ctr, allf, process=False).export(SP+"SLICECDT4_duxton.stl")
    np.save(SP+"SLICECDT4_origin.npy", _ctr)
    trimesh.Trimesh(allv, allf, process=False).export(SP+"SLICECDT4_duxton_NOCENTRE.stl")
    log(f"[done] {time.time()-T0:.0f}s -> SLICECDT2_duxton.stl")

def conf_holed(domain_polygon,dtm,rings,padz,terrain_step=10.0):
    collar=max(terrain_step/2,1.0)
    union=unary_union(rings) if rings else None
    xmin,ymin,xmax,ymax=domain_polygon.bounds
    gx,gy=np.meshgrid(np.arange(xmin,xmax+terrain_step,terrain_step),
                      np.arange(ymin,ymax+terrain_step,terrain_step))
    pts=np.column_stack([gx.ravel(),gy.ravel()])
    geoms=shp_points(pts[:,0],pts[:,1]); keep=contains(domain_polygon,geoms)
    if union is not None and not union.is_empty:
        keep &= ~contains(union.buffer(collar),geoms)
    grid=pts[keep]
    verts,zs,vidx=[],[],{}
    def add_v(x,y,z):
        k=(round(x,3),round(y,3)); i=vidx.get(k)
        if i is None: i=len(verts); vidx[k]=i; verts.append((x,y)); zs.append(z)
        return i
    segs=[]
    def add_ring(coords,pad):
        ring=coords[:-1] if coords[0]==coords[-1] else coords
        ids=[add_v(x,y,pad if pad is not None else float(dtm(x,y))) for x,y in ring]
        for i in range(len(ids)): segs.append((ids[i],ids[(i+1)%len(ids)]))
    add_ring(_densify_ring(domain_polygon.exterior.coords,terrain_step),None)
    holes=[]
    for q,pz in zip(rings,padz):
        add_ring(list(q.exterior.coords),pz)
        for it_ in q.interiors: add_ring(list(it_.coords),pz)
        rp=q.representative_point()   # inside the ANNULUS for a polygon with holes
        holes.append((rp.x,rp.y))
    for x,y in grid: add_v(float(x),float(y),float(dtm(x,y)))
    verts=np.array(verts,float); zs=np.array(zs,float); origin=verts.min(axis=0)
    d={"vertices":verts-origin,"segments":np.array(segs,int)}
    if holes: d["holes"]=np.array(holes,float)-origin
    out=_triangle.triangulate(d,"pYY")  # YY: no Steiner points on ANY segment
    # ("Y" alone only protects the OUTER boundary -- building rings are interior
    #  segments, so Triangle was free to split them, unpairing the seam)
    if "triangles" not in out: raise RuntimeError("triangle produced nothing")
    tv=out["vertices"]+origin
    allz = zs if len(tv)<=len(verts) else np.concatenate(
        [zs,np.atleast_1d(dtm(tv[len(verts):,0],tv[len(verts):,1]))])
    return np.column_stack([tv,allz]), out["triangles"].astype(np.int64)

if __name__=="__main__":
    main((28941,28758,29341,29158))
