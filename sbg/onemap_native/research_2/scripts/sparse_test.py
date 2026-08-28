"""Decisive test: is meshlib's meshToLevelSet SPARSE (narrow band) or DENSE?

Method: level-set one building, then level-set the SAME building with a far-away
degenerate speck added so the bounding box inflates ~10x per axis (~1000x volume)
while the SURFACE AREA is unchanged.
  cost unchanged  -> sparse (cost ~ surface area) -> ONE GLOBAL GRID IS VIABLE
  cost scales     -> dense  (cost ~ bbox volume)  -> grouping is mandatory

Also prints DTM relief per domain, to check whether Duxton's success is just
"it's flat so the terrain path is trivial".
"""
import sys, os; sys.path.insert(0, "/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np, time, resource, gc
import meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
from shapely.geometry import box
from pyproj import Transformer
from sbg.onemap_native.tiles import domain_leaf_tiles
from sbg.onemap_native.extract import extract_domain_buildings, seal_piece
from sbg.onemap_native.terrain import build_domain_dtm

VS = 0.5
DOMS = {"duxton": (28941, 28758, 29341, 29158),
        "kentridge": (21950, 30250, 22850, 31150)}


def rss_mb():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


def mlmesh(v, f):
    return mn.meshFromFacesVerts(np.asarray(f, np.int32), np.asarray(v, float))


def levelset_cost(v, f, tag):
    gc.collect()
    r0 = rss_mb(); t0 = time.time()
    g = mr.meshToLevelSet(mr.MeshPart(mlmesh(v, f)), mr.AffineXf3f(),
                          mr.Vector3f(VS, VS, VS), 3.0)
    dt = time.time() - t0
    r1 = rss_mb()
    d = (np.asarray(v).max(axis=0) - np.asarray(v).min(axis=0)) + 3 * VS
    nvox_dense = float(np.prod(d / VS))
    # try to read the real active-voxel count if the binding exposes it
    active = None
    for attr in ("activeVoxelCount", "getActiveVoxelCount", "activeVoxels"):
        try:
            a = getattr(g, attr)
            active = a() if callable(a) else a
            break
        except Exception:
            pass
    print(f"  {tag:38s} bbox={d[0]:7.0f}x{d[1]:7.0f}x{d[2]:6.0f}m  "
          f"dense_vox={nvox_dense:>14,.0f}  t={dt:6.2f}s  peakRSS={r1:7.0f}MB "
          f"(+{r1-r0:6.0f})  active={active}")
    del g
    return dt, nvox_dense


# ---------------------------------------------------------------- relief check
print("=== DTM relief per domain (is Duxton just flat?) ===")
for name, B in DOMS.items():
    dtm = build_domain_dtm(B, step=5.0, margin=0.0)
    z = np.asarray(dtm[0] if isinstance(dtm, tuple) else dtm, float)
    z = z[np.isfinite(z)]
    print(f"  {name:10s} span={B[2]-B[0]:.0f}m  z: min={z.min():6.1f} max={z.max():6.1f} "
          f"RELIEF={z.max()-z.min():6.1f}m   p10={np.percentile(z,10):5.1f} "
          f"p90={np.percentile(z,90):5.1f}")

# ------------------------------------------------------- sparse vs dense probe
print("\n=== level set: sparse (surface-area bound) or dense (volume bound)? ===")
B = DOMS["duxton"]
t = Transformer.from_crs("EPSG:3414", "EPSG:4326", always_xy=True)
lo, la = t.transform([B[0], B[2]], [B[1], B[3]])
pieces = extract_domain_buildings(domain_leaf_tiles(min(lo), min(la), max(lo), max(la)),
                                  box(*B), store_dir="data/onemap_store")
# pick the biggest piece so timings are above noise
pieces.sort(key=lambda p: -len(p["faces"]))
v, f = seal_piece(pieces[0]["verts"], pieces[0]["faces"])
v = np.asarray(v, float); f = np.asarray(f)
print(f"  test building: {len(f):,} faces, {len(v):,} verts")

base_t, base_vox = levelset_cost(v, f, "1. building alone")

# inflate the bbox WITHOUT adding surface area: one tiny far-away triangle
for mult in (5, 10):
    ext = (v.max(axis=0) - v.min(axis=0)) * mult
    far = v.max(axis=0) + ext
    tiny = np.array([far, far + [0.01, 0, 0], far + [0, 0.01, 0]])
    v2 = np.vstack([v, tiny])
    f2 = np.vstack([f, [[len(v), len(v) + 1, len(v) + 2]]])
    dt, nv = levelset_cost(v2, f2, f"2. + speck at {mult}x bbox")
    print(f"     -> dense_vox x{nv/base_vox:8.1f}   time x{dt/max(base_t,1e-6):6.2f}")

print("\n  VERDICT: time ratio ~1  => SPARSE, one global grid viable.")
print("           time ratio ~ voxel ratio => DENSE, grouping mandatory.")
