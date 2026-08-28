import sys, os; sys.path.insert(0,"/home/quentin/snrsi")
import numpy as np, trimesh
def fast(v,f,tag):
    v=np.asarray(v,float); f=np.asarray(f)
    _,inv=np.unique(np.round(v,4),axis=0,return_inverse=True)
    wf=inv.reshape(-1)[f]
    wf=wf[(wf[:,0]!=wf[:,1])&(wf[:,1]!=wf[:,2])&(wf[:,0]!=wf[:,2])]
    ed=np.sort(np.vstack([wf[:,[0,1]],wf[:,[1,2]],wf[:,[0,2]]]),axis=1)
    _,c=np.unique(ed,axis=0,return_counts=True)
    print(f"{tag:46s} faces={len(wf):>8,}  boundary={int((c==1).sum()):>6,}  "
          f"NON-MANIFOLD={int((c>2).sum()):>4}")
SP=os.path.dirname(os.path.abspath(__file__))+"/"
d=np.load(SP+"PROV_assembly.npz")
v,f=d["v"],d["f"]
print(f"vertex X range {v[:,0].min():.0f}..{v[:,0].max():.0f}  "
      f"float32 spacing at that magnitude = {np.spacing(np.float32(v[:,0].max())):.4f} m\n")
fast(v,f,"A. in-memory float64 (ground truth)")
p=trimesh.load(SP+"PROV_assembly.ply",process=False); fast(p.vertices,p.faces,"B. round-tripped through binary PLY")
s=trimesh.load(SP+"SLICECDT2_duxton.stl",process=False); fast(s.vertices,s.faces,"C. round-tripped through STL (float32)")
# D: STL but recentred on origin first
v2=v-v.mean(axis=0)
trimesh.Trimesh(v2,f,process=False).export(SP+"tmp_centred.stl")
s2=trimesh.load(SP+"tmp_centred.stl",process=False); fast(s2.vertices,s2.faces,"D. STL after recentring to origin")
