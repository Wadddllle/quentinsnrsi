"""Test the hypothesis that the 798 X-junction pinches come from seal_piece's
fillHole BASE CAP (which fills each boundary loop as an independent disc, so
nested courtyard loops get overlapping caps).

Two direct checks, no re-run needed:
  A. Face NORMALS at each bad edge. A cap is near-horizontal (|nz| ~ 1); a facade
     is near-vertical (|nz| ~ 0). If the pinches are cap-vs-cap, |nz| is high.
  B. Attribute each bad edge to the piece that owns it, then compare the
     boundary-LOOP-COUNT distribution of pinch-owning pieces against all pieces.
     The hypothesis predicts pinch owners are disproportionately multi-loop.
"""
import sys, os; sys.path.insert(0, "/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np, trimesh
from collections import defaultdict
from scipy.spatial import cKDTree
from shapely.geometry import box
from pyproj import Transformer
from sbg.onemap_native.tiles import domain_leaf_tiles
from sbg.onemap_native.extract import extract_domain_buildings, seal_piece

B = (28941, 28758, 29341, 29158)
STL = "data/wt_raw_test/duxton_ftetwild_domain.stl"


def loop_count(v, f):
    """boundary loops of the RAW piece after dedup+weld (i.e. what fillHole caps)"""
    v = np.asarray(v, float); f = np.asarray(f)
    uq, inv = np.unique(np.round(v, 3), axis=0, return_inverse=True)
    wf = inv.reshape(-1)[f]
    wf = wf[(wf[:, 0] != wf[:, 1]) & (wf[:, 1] != wf[:, 2]) & (wf[:, 0] != wf[:, 2])]
    # drop the duplicate of every double-sided face
    srt = np.sort(wf, axis=1)
    _, keep = np.unique(srt, axis=0, return_index=True)
    wf = wf[np.sort(keep)]
    if len(wf) == 0:
        return 0, 0
    ed = np.sort(np.vstack([wf[:, [0, 1]], wf[:, [1, 2]], wf[:, [2, 0]]]), axis=1)
    u, c = np.unique(ed, axis=0, return_counts=True)
    bnd = u[c == 1]
    if len(bnd) == 0:
        return 0, 0
    adj = defaultdict(list)
    for a, b in bnd:
        adj[a].append(b); adj[b].append(a)
    seen, loops = set(), 0
    for s in adj:
        if s in seen:
            continue
        loops += 1
        st = [s]; seen.add(s)
        while st:
            n = st.pop()
            for k in adj[n]:
                if k not in seen:
                    seen.add(k); st.append(k)
    return loops, len(bnd)


tr = Transformer.from_crs("EPSG:3414", "EPSG:4326", always_xy=True)
lo, la = tr.transform([B[0], B[2]], [B[1], B[3]])
pieces = extract_domain_buildings(domain_leaf_tiles(min(lo), min(la), max(lo), max(la)),
                                  box(*B), store_dir="data/onemap_store")

# rebuild EXACTLY what ftw_domain.py fed in, to recover the recentering offset
SV, lab, loops = [], [], []
for i, p in enumerate(pieces):
    try:
        sv, sf = seal_piece(p["verts"], p["faces"])
    except Exception:
        sv, sf = p["verts"], p["faces"]
    sv = np.asarray(sv, float); sf = np.asarray(sf)
    if len(sf) == 0:
        continue
    nl, nb = loop_count(p["verts"], p["faces"])
    SV.append(sv); lab.append(np.full(len(sv), len(loops))); loops.append(nl)
allv = np.vstack(SV)
lab = np.concatenate(lab)
ctr = allv.mean(axis=0)
allv = allv - ctr
loops = np.array(loops)
print(f"{len(loops)} pieces.  boundary loops per piece (pre-fillHole): "
      f"1 loop={int((loops==1).sum())}  2-3={int(((loops>=2)&(loops<=3)).sum())}  "
      f">3={int((loops>3).sum())}  0={int((loops==0).sum())}")

# ---- the 798 bad edges from the shipped output
m = trimesh.load(STL, process=False)
v = np.asarray(m.vertices, np.float32); f = np.asarray(m.faces).astype(np.int64)
uq, inv = np.unique(v.view([('', np.float32)] * 3).ravel(), return_inverse=True)
va = uq.view(np.float32).reshape(-1, 3)
nv = np.int64(len(uq)); idx = inv.astype(np.int64)[f]
de = np.vstack([idx[:, [0, 1]], idx[:, [1, 2]], idx[:, [2, 0]]])
fid = np.tile(np.arange(len(f)), 3)
a, b = de[:, 0], de[:, 1]
key = np.minimum(a, b) * nv + np.maximum(a, b)
u, c = np.unique(key, return_counts=True)
bad = set(u[c > 2].tolist())
sel = np.array([k in bad for k in key])
print(f"{len(bad)} bad edges, {int(sel.sum())} half-edge records on them")

# ---- A. normals of the faces meeting at a bad edge
fn = m.face_normals[fid[sel]]
nz = np.abs(fn[:, 2])
print(f"\nA. |nz| of faces at bad edges  (1.0 = horizontal cap, 0.0 = vertical facade)")
print(f"   median={np.median(nz):.3f}  p25={np.percentile(nz,25):.3f}  "
      f"p75={np.percentile(nz,75):.3f}")
print(f"   near-horizontal (|nz|>0.9): {float((nz>0.9).mean()):.1%}   "
      f"near-vertical (|nz|<0.1): {float((nz<0.1).mean()):.1%}")
allnz = np.abs(m.face_normals[:, 2])
print(f"   (whole mesh for reference: |nz|>0.9 is {float((allnz>0.9).mean()):.1%})")

# ---- B. attribute each bad edge to its owning piece
tree = cKDTree(allv)
mid = 0.5 * (va[(np.array(sorted(bad)) // nv).astype(np.int64)]
             + va[(np.array(sorted(bad)) % nv).astype(np.int64)])
_, near = tree.query(mid, k=1)
owner = lab[near]
cnt = np.bincount(owner, minlength=len(loops))
own = cnt > 0
print(f"\nB. pinch attribution")
print(f"   {int(own.sum())} of {len(loops)} pieces own at least one pinch")
for tag, msk in [("ALL pieces", np.ones(len(loops), bool)), ("pinch OWNERS", own)]:
    L = loops[msk]
    print(f"   {tag:14s} n={len(L):4d}  loops: mean={L.mean():5.2f} "
          f"median={np.median(L):4.1f}  %multi-loop={float((L>1).mean()):6.1%}")
# pinch count vs loop count
print(f"\n   pinches per piece, grouped by that piece's loop count:")
for lo_, hi_ in [(0, 1), (2, 3), (4, 8), (9, 10**6)]:
    msk = (loops >= lo_) & (loops <= hi_)
    if msk.sum():
        print(f"     loops {lo_}-{hi_ if hi_<10**6 else '+':<3}: {int(msk.sum()):4d} pieces, "
              f"{int(cnt[msk].sum()):5d} pinches, "
              f"{cnt[msk].sum()/max(msk.sum(),1):6.2f} per piece")
