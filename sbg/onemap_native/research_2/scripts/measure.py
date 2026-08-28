import sys, numpy as np, meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
for p in sys.argv[1:]:
    m = mr.loadMesh(p)
    t = m.topology
    holes = len(t.findHoleRepresentiveEdges())
    comps = len(mr.MeshComponents.getAllComponents(mr.MeshPart(m)))
    try: sx = mr.findSelfCollidingTriangles(mr.MeshPart(m)).size()
    except Exception:
        try: sx = len(mr.findSelfCollidingTriangles(mr.MeshPart(m)))
        except Exception: sx = -1
    # strict re-weld edge histogram
    v = mn.getNumpyVerts(m); f = mn.getNumpyFaces(t)
    key = np.round(v, 4)
    _, inv = np.unique(key, axis=0, return_inverse=True)
    wf = inv.reshape(-1)[f]
    wf = wf[(wf[:,0]!=wf[:,1])&(wf[:,1]!=wf[:,2])&(wf[:,0]!=wf[:,2])]
    ed = np.sort(np.vstack([wf[:,[0,1]], wf[:,[1,2]], wf[:,[0,2]]]), axis=1)
    _, cnt = np.unique(ed, axis=0, return_counts=True)
    print(f"{p.split('/')[-1]:38s} faces={t.numValidFaces():>8,} holes={holes:>4} "
          f"comps={comps:>4} selfX={sx:>6} open={int((cnt==1).sum()):>6} nonmanif={int((cnt>2).sum()):>5}")
