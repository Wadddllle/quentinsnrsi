"""Are the 116 NM / 228 winding real, or an artifact of welding a 9.2M-face mesh
at 0.1mm? Sweep the weld tolerance."""
import sys, os, numpy as np, trimesh
m=trimesh.load(os.environ["MESH"], process=False)
v=np.asarray(m.vertices); f=np.asarray(m.faces)
print(f"faces={len(f):,}  coord range x {v[:,0].min():.0f}..{v[:,0].max():.0f}")
for dec in (6,5,4,3):
    _,inv=np.unique(np.round(v,dec),axis=0,return_inverse=True)
    wf=inv.reshape(-1)[f]
    wf=wf[(wf[:,0]!=wf[:,1])&(wf[:,1]!=wf[:,2])&(wf[:,0]!=wf[:,2])]
    und=np.sort(np.vstack([wf[:,[0,1]],wf[:,[1,2]],wf[:,[0,2]]]),axis=1)
    _,c=np.unique(und,axis=0,return_counts=True)
    d=np.vstack([wf[:,[0,1]],wf[:,[1,2]],wf[:,[2,0]]])
    _,dc=np.unique(d,axis=0,return_counts=True)
    print(f"  weld 1e-{dec} m: boundary={int((c==1).sum()):>6}  NM={int((c>2).sum()):>5}  "
          f"winding={int((dc-1)[dc>1].sum()):>5}")
