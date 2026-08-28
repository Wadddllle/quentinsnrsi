"""Resolve building-building overlap by DIFFERENCE, then let DAGMC judge it.

B := B - A for every overlapping pair, processed in index order so each volume
is only ever cut by lower-indexed neighbours. That is imprint-and-merge done
with CSG: the pair ends up sharing the contact surface (which DAGMC WANTS)
with no shared volume (which DAGMC forbids -> lost particles).
"""
import sys, os, pickle, time
sys.path.insert(0,"/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np
import meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
SP="/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583/scratchpad"
C=[c for c in pickle.load(open(f"{SP}/duxton_cache.pkl","rb"))
   if c["v"] is not None and len(c["v"])>0]
M=[mn.meshFromFacesVerts(np.asarray(c["f"],np.int32),np.asarray(c["v"],float)) for c in C]
def bbox(m):
    v=mn.getNumpyVerts(m); return v.min(axis=0),v.max(axis=0)
BB=[bbox(m) for m in M]
pairs=[(a,b) for a in range(len(C)) for b in range(a+1,len(C))
       if not((BB[a][0]>BB[b][1]).any() or (BB[b][0]>BB[a][1]).any())
       and mr.findCollidingTriangles(mr.MeshPart(M[a]),mr.MeshPart(M[b])).size()>0]
print(f"{len(C)} volumes, {len(pairs)} overlapping pairs",flush=True)
t0=time.time(); nfix=nfail=0
for a,b in pairs:
    try:
        r=mr.boolean(M[a],M[b],mr.BooleanOperation.DifferenceBA)
        if r.valid() and r.mesh.topology.numValidFaces()>0:
            M[b]=r.mesh; BB[b]=bbox(M[b]); nfix+=1
        else: nfail+=1
    except Exception: nfail+=1
print(f"[{time.time()-t0:.0f}s] differences applied {nfix}, failed {nfail}",flush=True)
out=[]
for i,m in enumerate(M):
    v=mn.getNumpyVerts(m); f=mn.getNumpyFaces(m.topology)
    if len(v)>0 and len(f)>=4: out.append(dict(i=i,v=v,f=f))
print(f"{len(out)} volumes survive")
pickle.dump(out,open(f"{SP}/duxton_resolved.pkl","wb"))
