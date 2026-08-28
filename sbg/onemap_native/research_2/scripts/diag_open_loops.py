import sys, os; sys.path.insert(0,"/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np, trimesh
from collections import defaultdict

P=os.environ.get("MESH","data/wt_raw_test/duxton_400m_slice_cdt.stl")
m=trimesh.load(P, process=False)
v=np.asarray(m.vertices); f=np.asarray(m.faces)
# strict weld
key=np.round(v,4)
uq,inv=np.unique(key,axis=0,return_inverse=True)
inv=inv.reshape(-1); wf=inv[f]
wf=wf[(wf[:,0]!=wf[:,1])&(wf[:,1]!=wf[:,2])&(wf[:,0]!=wf[:,2])]
ed=np.sort(np.vstack([wf[:,[0,1]],wf[:,[1,2]],wf[:,[0,2]]]),axis=1)
ued,cnt=np.unique(ed,axis=0,return_counts=True)
open_ed=ued[cnt==1]
print(f"open edges: {len(open_ed)}")

# chain open edges into loops
adj=defaultdict(list)
for a,b in open_ed: adj[a].append(b); adj[b].append(a)
seen=set(); loops=[]
for s in adj:
    if s in seen: continue
    comp=[]; stack=[s]; seen.add(s)
    while stack:
        n=stack.pop(); comp.append(n)
        for k in adj[n]:
            if k not in seen: seen.add(k); stack.append(k)
    loops.append(comp)
print(f"open loops (connected components of the open-edge graph): {len(loops)}\n")

zs=uq[:,2]
print(f"{'loop':>5} {'verts':>6} {'z_min':>8} {'z_max':>8} {'dz':>7} {'xy_span':>8} {'approx_area':>11}")
rows=[]
for i,c in enumerate(sorted(loops,key=len,reverse=True)):
    pts=uq[c]
    dz=pts[:,2].max()-pts[:,2].min()
    span=max(np.ptp(pts[:,0]),np.ptp(pts[:,1]))
    # planar-ish area estimate via convex hull in XY
    try:
        from scipy.spatial import ConvexHull
        a=ConvexHull(pts[:,:2]).volume if len(c)>=3 else 0.0
    except Exception: a=0.0
    rows.append((len(c),pts[:,2].min(),pts[:,2].max(),dz,span,a))
for r in rows[:18]:
    print(f"{'':>5} {r[0]:>6} {r[1]:>8.2f} {r[2]:>8.2f} {r[3]:>7.2f} {r[4]:>8.2f} {r[5]:>11.1f}")
print(f"... {max(0,len(rows)-18)} more")
tot=sum(r[0] for r in rows)
flat=[r for r in rows if r[3]<0.05]
print(f"\nloops that are FLAT (dz<5cm, i.e. a planar cap would close them): {len(flat)}/{len(rows)}"
      f"  covering {sum(r[0] for r in flat)}/{tot} open verts")
big=[r for r in rows if r[5]>1.0]
print(f"loops with hull area >1 m^2: {len(big)}  (total area {sum(r[5] for r in big):.1f} m^2)")
print(f"loops with hull area <=0.05 m^2 (below the ring threshold): {len([r for r in rows if r[5]<=0.05])}")
