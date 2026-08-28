import sys, numpy as np, trimesh
p=sys.argv[1]
m=trimesh.load(p, process=False)
v=np.asarray(m.vertices); f=np.asarray(m.faces)
_,inv=np.unique(np.round(v,4),axis=0,return_inverse=True)
wf=inv.reshape(-1)[f]
srt=np.sort(wf,axis=1)
uq,cnt=np.unique(srt,axis=0,return_counts=True)
print(f"{p.split('/')[-1]}: {len(f):,} faces -> {len(uq):,} unique triangles "
      f"({100*len(uq)/len(f):.1f}%);  appearing 2x: {int((cnt==2).sum()):,}  >2x: {int((cnt>2).sum()):,}")
