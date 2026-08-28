import numpy as np, trimesh, meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
f='/home/quentin/snrsi/data/wt_raw_test/duxton_400m_slice_cdt.stl'
m=trimesh.load(f,process=False); m.merge_vertices()
mm=mn.meshFromFacesVerts(np.asarray(m.faces,np.int32),np.asarray(m.vertices,float))
r=mr.findSelfCollidingTriangles(mr.MeshPart(mm))
a=np.array([int(r[k].aFace) for k in range(r.size())])
b=np.array([int(r[k].bFace) for k in range(r.size())])
idx=np.unique(np.concatenate([a,b])); idx=idx[idx<len(m.faces)]
print(f"pairs={r.size()}  distinct faces={len(idx)}")
ctr=m.vertices[m.faces[idx]].mean(axis=1)
print(f"z of involved faces: min={ctr[:,2].min():.2f} median={np.median(ctr[:,2]):.2f} max={ctr[:,2].max():.2f}")
h,ed=np.histogram(ctr[:,2],bins=10)
for c,lo,hi in zip(h,ed[:-1],ed[1:]):
    if c: print(f"   z {lo:6.2f}-{hi:6.2f}: {c:5d} {'#'*int(40*c/max(h.max(),1))}")
# per PAIR: are both faces near the pad band (terrain zone) or one high (overhang)?
za=m.vertices[m.faces[a]].mean(axis=1)[:,2]; zb=m.vertices[m.faces[b]].mean(axis=1)[:,2]
PAD=16.0
both_low=int(((za<PAD)&(zb<PAD)).sum()); both_high=int(((za>=PAD)&(zb>=PAD)).sum())
mixed=int(r.size())-both_low-both_high
print(f"\npairs with BOTH faces below z={PAD} (terrain/pad band): {both_low}")
print(f"pairs with BOTH faces above  (building-vs-building up high): {both_high}")
print(f"mixed (one low one high -- overhang crossing terrain):      {mixed}")
# horizontal spread of each pair: same building or two different ones?
ca=m.vertices[m.faces[a]].mean(axis=1)[:,:2]; cb=m.vertices[m.faces[b]].mean(axis=1)[:,:2]
d=np.linalg.norm(ca-cb,axis=1)
print(f"\nXY distance between colliding faces: median={np.median(d):.2f}m p90={np.percentile(d,90):.2f}m max={d.max():.1f}m")
