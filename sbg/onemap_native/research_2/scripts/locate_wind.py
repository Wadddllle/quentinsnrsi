import sys, os, numpy as np, trimesh
from collections import defaultdict
p=os.environ["MESH"]
m=trimesh.load(p,process=False)
v=np.asarray(m.vertices); f=np.asarray(m.faces)
uq,inv=np.unique(np.round(v,4),axis=0,return_inverse=True)
wf=inv.reshape(-1)[f]
wf=wf[(wf[:,0]!=wf[:,1])&(wf[:,1]!=wf[:,2])&(wf[:,0]!=wf[:,2])]
he={}; bad=[]
for fi,(a,b,c) in enumerate(wf):
    for x,y in ((a,b),(b,c),(c,a)):
        if (x,y) in he: bad.append((x,y))
        else: he[(x,y)]=fi
bad=np.array(bad)
print(f"winding conflicts: {len(bad)}")
if len(bad):
    pts=(uq[bad[:,0]]+uq[bad[:,1]])/2
    print(f"  z    : min {pts[:,2].min():.2f} med {np.median(pts[:,2]):.2f} max {pts[:,2].max():.2f}")
    xmin,xmax=uq[:,0].min(),uq[:,0].max(); ymin,ymax=uq[:,1].min(),uq[:,1].max()
    d=np.minimum(np.minimum(pts[:,0]-xmin,xmax-pts[:,0]),
                 np.minimum(pts[:,1]-ymin,ymax-pts[:,1]))
    print(f"  dist to domain boundary: <0.1m {(d<0.1).sum()}  <1m {(d<1).sum()}  >5m {(d>5).sum()}")
    zb=uq[:,2].min()
    print(f"  at the flat base plane z={zb:.2f}: {(np.abs(pts[:,2]-zb)<0.05).sum()}")
    cells=np.unique(np.floor(pts[:,:2]/10).astype(int),axis=0)
    print(f"  spread over {len(cells)} distinct 10m cells")

# which loops? cluster conflicting edges into connected groups
if len(bad):
    from collections import defaultdict as dd
    adj=dd(list)
    for x,y in bad: adj[x].append(y); adj[y].append(x)
    seen=set(); groups=[]
    for s0 in adj:
        if s0 in seen: continue
        g=[]; st=[s0]; seen.add(s0)
        while st:
            n_=st.pop(); g.append(n_)
            for k in adj[n_]:
                if k not in seen: seen.add(k); st.append(k)
        groups.append(g)
    print(f"\n  conflicts form {len(groups)} connected groups")
    groups.sort(key=len,reverse=True)
    for g in groups[:10]:
        pg=uq[g]
        print(f"    group verts={len(g):>4}  z {pg[:,2].min():7.2f}..{pg[:,2].max():7.2f}  "
              f"xy span {np.ptp(pg[:,0]):6.2f} x {np.ptp(pg[:,1]):6.2f}")
