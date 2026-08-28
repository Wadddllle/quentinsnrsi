"""Does nudging the ISO-VALUE off zero remove the DMC pinch points?

A pinch is where the SDF is exactly tangent to 0 -- two solid lobes meeting at a
point. Extracting at iso>0 dilates slightly so the lobes MERGE into one thick
blob; the degenerate tangency (and the non-manifold edge a positional weld makes
from it) disappears. Cost is a few cm of inflation.

Judged under a STRICT EXACT-POSITION WELD, because that is what a downstream
mesher does when it reads the STL.
"""
import sys, os; sys.path.insert(0, "/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np, time, gc
import meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
from shapely.geometry import box
from pyproj import Transformer
from sbg.onemap_native.tiles import domain_leaf_tiles
from sbg.onemap_native.extract import extract_domain_buildings, seal_piece

VS = float(os.environ.get("VS", "0.5"))
DOMS = {"duxton": (28941, 28758, 29341, 29158),
        "kentridge": (21950, 30250, 22850, 31150)}
DOM = os.environ.get("DOM", "duxton")
B = DOMS[DOM]
ISOS = [float(x) for x in os.environ.get("ISOS", "0,0.02,0.05,0.1,0.2").split(",")]


def mlmesh(v, f):
    return mn.meshFromFacesVerts(np.asarray(f, np.int32), np.asarray(v, float))


def strict(v, f):
    """weld by EXACT float32 position -- what a consumer's STL import does"""
    v = np.asarray(v, np.float32)
    uq, inv = np.unique(v.view([('', np.float32)] * 3).ravel(), return_inverse=True)
    i = inv.astype(np.int64)[np.asarray(f).astype(np.int64)]
    i = i[(i[:, 0] != i[:, 1]) & (i[:, 1] != i[:, 2]) & (i[:, 0] != i[:, 2])]
    n = np.int64(len(uq))
    d = np.vstack([i[:, [0, 1]], i[:, [1, 2]], i[:, [2, 0]]])
    a, b = d[:, 0], d[:, 1]
    _, c = np.unique(np.minimum(a, b) * n + np.maximum(a, b), return_counts=True)
    _, dd = np.unique(a * n + b, return_counts=True)
    return int((c == 1).sum()), int((c > 2).sum()), int((dd - 1)[dd > 1].sum())


tr = Transformer.from_crs("EPSG:3414", "EPSG:4326", always_xy=True)
lo, la = tr.transform([B[0], B[2]], [B[1], B[3]])
pieces = extract_domain_buildings(domain_leaf_tiles(min(lo), min(la), max(lo), max(la)),
                                  box(*B), store_dir="data/onemap_store")
V, F, n = [], [], 0
for p in pieces:
    try:
        sv, sf = seal_piece(p["verts"], p["faces"])
    except Exception:
        sv, sf = p["verts"], p["faces"]
    sv = np.asarray(sv, float); sf = np.asarray(sf)
    if len(sf) == 0:
        continue
    V.append(sv); F.append(sf + n); n += len(sv)
V = np.vstack(V); F = np.vstack(F)
print(f"DOMAIN={DOM}  VS={VS}  {len(pieces)} pieces  {len(F):,} sealed faces\n")

grid = mr.meshToLevelSet(mr.MeshPart(mlmesh(V, F)), mr.AffineXf3f(),
                         mr.Vector3f(VS, VS, VS), 3.0)

print(f"{'iso(m)':>8} {'faces':>12} {'comps':>6} {'open':>6} {'NM':>5} {'wind':>6} "
      f"{'selfX':>7} {'vol_m3':>16} {'t':>6}")
base_vol = None
for iso in ISOS:
    gc.collect()
    t0 = time.time()
    gs = mr.GridToMeshSettings()
    gs.voxelSize = mr.Vector3f(VS, VS, VS)
    gs.isoValue = iso
    gs.adaptivity = 0.0
    m = mr.gridToMesh(grid, gs)
    v = mn.getNumpyVerts(m); f = mn.getNumpyFaces(m.topology)
    op, nm, wd = strict(v, f)
    sx = mr.findSelfCollidingTriangles(mr.MeshPart(m)).size()
    ncomp = mr.MeshComponents.getAllComponents(m).size()
    vol = m.volume()
    if base_vol is None:
        base_vol = vol
    print(f"{iso:8.3f} {len(f):12,} {ncomp:6} {op:6} {nm:5} {wd:6} {sx:7,} "
          f"{vol:16,.0f} {time.time()-t0:5.1f}s"
          + (f"  ({(vol/base_vol-1)*100:+.2f}%)" if iso != ISOS[0] else ""))
    del m
