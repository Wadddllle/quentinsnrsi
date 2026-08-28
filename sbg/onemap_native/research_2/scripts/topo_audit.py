import sys, os; sys.path.insert(0,"/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np, trimesh, meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
from collections import defaultdict

P = os.environ.get("MESH")
m = trimesh.load(P, process=False)
v = np.asarray(m.vertices); f = np.asarray(m.faces)
# STRICT weld at 0.1mm -- STL is triangle soup, so this is mandatory
_, inv = np.unique(np.round(v,4), axis=0, return_inverse=True)
inv = inv.reshape(-1); wf = inv[f]
wf = wf[(wf[:,0]!=wf[:,1])&(wf[:,1]!=wf[:,2])&(wf[:,0]!=wf[:,2])]
nv = int(wf.max())+1
print(f"=== {P.split('/')[-1]} ===")
print(f"faces {len(wf):,}   welded verts {nv:,}\n")

# --- 1. EDGE-manifoldness + closure ---
ed = np.sort(np.vstack([wf[:,[0,1]],wf[:,[1,2]],wf[:,[0,2]]]), axis=1)
ued, cnt = np.unique(ed, axis=0, return_counts=True)
b, nm = int((cnt==1).sum()), int((cnt>2).sum())
print(f"[CLOSED?]        boundary edges (1 face) : {b:,}      -> {'CLOSED' if b==0 else 'NOT closed'}")
print(f"[EDGE-MANIFOLD?] edges with >=3 faces    : {nm:,}      -> {'edge-manifold' if nm==0 else 'NOT edge-manifold'}")
print(f"                 edges with exactly 2    : {int((cnt==2).sum()):,}")

# --- 2. VERTEX-manifoldness (bowties) -- faces around a vertex must form ONE fan ---
# build per-vertex the set of opposite edges; a manifold vertex's link is a single cycle/path
link = defaultdict(list)
for a_,b_,c_ in wf:
    link[a_].append((b_,c_)); link[b_].append((c_,a_)); link[c_].append((a_,b_))
bowties = 0
for vtx, es in link.items():
    adj = defaultdict(list)
    for x,y in es:
        adj[x].append(y); adj[y].append(x)
    seen=set(); comps=0
    for s in adj:
        if s in seen: continue
        comps+=1; st=[s]; seen.add(s)
        while st:
            n=st.pop()
            for k in adj[n]:
                if k not in seen: seen.add(k); st.append(k)
    if comps>1: bowties+=1
print(f"[VERTEX-MANIFOLD?] bowtie vertices (link in >1 piece): {bowties:,}"
      f"      -> {'vertex-manifold' if bowties==0 else 'NOT vertex-manifold'}")

# --- 3. Orientation consistency ---
he = set()
bad_orient = 0
for a_,b_,c_ in wf:
    for x,y in ((a_,b_),(b_,c_),(c_,a_)):
        if (x,y) in he: bad_orient += 1
        he.add((x,y))
print(f"[ORIENTED?]      duplicate half-edges (same dir): {bad_orient:,}"
      f"      -> {'consistently oriented' if bad_orient==0 else 'inconsistent winding'}")

# --- 4. SELF-INTERSECTION magnitude ---
ml = mn.meshFromFacesVerts(np.asarray(wf,np.int32), np.asarray(np.unique(np.round(v,4),axis=0),float))
pairs = mr.findSelfCollidingTriangles(mr.MeshPart(ml))
try: n_pairs = pairs.size()
except Exception: n_pairs = len(pairs)
print(f"\n[INTERSECTION-FREE?] self-colliding triangle PAIRS: {n_pairs:,}"
      f"      -> {'intersection-free' if n_pairs==0 else 'NOT intersection-free'}")
if n_pairs:
    uq = np.unique(np.round(v,4), axis=0)
    tri = uq[wf]
    fa, fb = [], []
    for i in range(n_pairs):
        p = pairs[i]; fa.append(int(p.aFace)); fb.append(int(p.bFace))
    fa=np.array(fa); fb=np.array(fb)
    A, B = tri[fa], tri[fb]
    def plane(T):
        n = np.cross(T[:,1]-T[:,0], T[:,2]-T[:,0])
        L = np.linalg.norm(n,axis=1,keepdims=True); L[L==0]=1
        return n/L
    nA, nB = plane(A), plane(B)
    # penetration = how far B's verts cross A's plane, and vice versa
    dB = np.einsum('ijk,ik->ij', B - A[:,0][:,None,:], nA)
    dA = np.einsum('ijk,ik->ij', A - B[:,0][:,None,:], nB)
    pen = np.maximum(np.abs(dB).max(axis=1), np.abs(dA).max(axis=1))
    ctr = (A.mean(axis=1)+B.mean(axis=1))/2
    print(f"    unique faces involved : {len(np.unique(np.concatenate([fa,fb]))):,} "
          f"({100*len(np.unique(np.concatenate([fa,fb])))/len(wf):.2f}% of all faces)")
    print(f"    penetration depth (m) : median {np.median(pen):.3f}  p90 {np.percentile(pen,90):.3f}  "
          f"max {pen.max():.3f}")
    print(f"    pairs with pen < 1cm  : {int((pen<0.01).sum()):,} ({100*(pen<0.01).mean():.0f}%)")
    print(f"    pairs with pen < 5cm  : {int((pen<0.05).sum()):,} ({100*(pen<0.05).mean():.0f}%)")
    print(f"    pairs with pen > 50cm : {int((pen>0.5).sum()):,} ({100*(pen>0.5).mean():.0f}%)")
    print(f"    z of collisions       : min {ctr[:,2].min():.1f}  median {np.median(ctr[:,2]):.1f}  "
          f"max {ctr[:,2].max():.1f}")
    # spatial clustering: how many distinct 10m cells
    cell = np.floor(ctr[:,:2]/10.0).astype(int)
    print(f"    spread                : {len(np.unique(cell,axis=0))} distinct 10m cells")
