"""Find buildings with genuinely THIN walls -- the ones that alias into
'pillar gap pillar' when voxelized. Local thickness = cast a ray from each face
centroid along -normal (into the solid) and take the distance to the next hit.
Faces with thickness < VS are below Nyquist and cannot survive voxelization.
"""
import sys, os; sys.path.insert(0, "/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np, trimesh, time
from shapely.geometry import box
from pyproj import Transformer
from sbg.onemap_native.tiles import domain_leaf_tiles
from sbg.onemap_native.extract import extract_domain_buildings, seal_piece

DOMS = {"duxton": (28941, 28758, 29341, 29158),
        "kentridge": (21950, 30250, 22850, 31150)}
DOM = os.environ.get("DOM", "duxton")
B = DOMS[DOM]
NS = int(os.environ.get("NSAMPLE", "400"))      # rays per building
VS = 0.5

tr = Transformer.from_crs("EPSG:3414", "EPSG:4326", always_xy=True)
lo, la = tr.transform([B[0], B[2]], [B[1], B[3]])
pieces = extract_domain_buildings(domain_leaf_tiles(min(lo), min(la), max(lo), max(la)),
                                  box(*B), store_dir="data/onemap_store")
print(f"DOMAIN={DOM}  {len(pieces)} pieces\n")

rows = []
t0 = time.time()
for i, p in enumerate(pieces):
    try:
        v, f = seal_piece(p["verts"], p["faces"])
    except Exception:
        continue
    v = np.asarray(v, float); f = np.asarray(f)
    if len(f) < 50:
        continue
    m = trimesh.Trimesh(v, f, process=False)
    n = m.face_normals
    c = m.triangles_center
    k = min(NS, len(f))
    sel = np.random.default_rng(0).choice(len(f), k, replace=False)
    # start just inside the surface, shoot inward
    org = c[sel] - n[sel] * 1e-3
    loc, idx_r, _ = m.ray.intersects_location(org, -n[sel], multiple_hits=False)
    if len(idx_r) == 0:
        continue
    th = np.linalg.norm(loc - org[idx_r], axis=1)
    rows.append((i, len(f), float(np.median(th)),
                 float((th < VS).mean()), float((th < 1.0).mean()),
                 m.bounds[1] - m.bounds[0]))
    if (i + 1) % 50 == 0:
        print(f"  ...{i+1}/{len(pieces)} ({time.time()-t0:.0f}s)", flush=True)

rows.sort(key=lambda r: -r[3])
print(f"\n{'piece':>6} {'faces':>7} {'med_thk':>9} {'frac<0.5m':>10} {'frac<1m':>9}   bbox(m)")
for r in rows[:15]:
    print(f"{r[0]:6} {r[1]:7,} {r[2]:9.2f} {r[3]:10.1%} {r[4]:9.1%}   "
          f"{r[5][0]:.0f}x{r[5][1]:.0f}x{r[5][2]:.0f}")
th_all = np.array([r[3] for r in rows])
print(f"\n{len(rows)} buildings measured. Fraction of faces on sub-0.5m-thick walls:")
print(f"  median={np.median(th_all):.1%}  p90={np.percentile(th_all,90):.1%}  "
      f"max={th_all.max():.1%}")
print(f"  buildings with >20% thin faces: {(th_all>0.2).sum()}/{len(rows)}")
print(f"[done] {time.time()-t0:.0f}s")
