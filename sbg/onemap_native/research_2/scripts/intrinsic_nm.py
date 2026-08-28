"""Do buildings have non-manifold edges ON THEIR OWN?

Claimed ~0.5/piece from a 6-piece sample. That is not evidence. Run ALL pieces
individually and get the real distribution. If the sum is near the domain-level
floor (~140), the claim holds and the floor is intrinsic to the source geometry.
If it is ~0, the claim was made up and the plateau has another cause.

Each piece alone, max_threads=1, parallel across processes.
"""
import sys, os; sys.path.insert(0, "/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np, io, contextlib, time
from multiprocessing import Pool
from shapely.geometry import box
from pyproj import Transformer
from sbg.onemap_native.tiles import domain_leaf_tiles
from sbg.onemap_native.extract import extract_domain_buildings, seal_piece

B = (28941, 28758, 29341, 29158)
EPS = float(os.environ.get("EPS", "0.10"))
NPROC = int(os.environ.get("NPROC", "10"))


def one(arg):
    i, v, f = arg
    import wildmeshing as wm
    import meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
    v = np.asarray(v, float); f = np.asarray(f, np.int32)
    diag = float(np.linalg.norm(v.max(axis=0) - v.min(axis=0)))
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            t = wm.Tetrahedralizer(epsilon=EPS / diag, edge_length_r=0.05,
                                   coarsen=True, max_its=0, stop_quality=10,
                                   max_threads=1)
            t.set_mesh(v, f)
            t.tetrahedralize()
            o = t.get_tet_mesh(floodfill=True, manifold_surface=False,
                               correct_surface_orientation=True)
        tv, tt = np.asarray(o[0], float), np.asarray(o[1])
        q = np.vstack([tt[:, [0, 1, 2]], tt[:, [0, 1, 3]],
                       tt[:, [0, 2, 3]], tt[:, [1, 2, 3]]])
        _, idx, cnt = np.unique(np.sort(q, axis=1), axis=0,
                                return_index=True, return_counts=True)
        bf = q[idx[cnt == 1]]
        used = np.unique(bf)
        rm = np.full(len(tv), -1, np.int64); rm[used] = np.arange(len(used))
        bv, bff = tv[used], rm[bf]
        v32 = np.ascontiguousarray(np.asarray(bv, np.float32))
        uq, inv = np.unique(v32.view([('', np.float32)] * 3).ravel(), return_inverse=True)
        ii = inv.astype(np.int64)[bff]
        ii = ii[(ii[:, 0] != ii[:, 1]) & (ii[:, 1] != ii[:, 2]) & (ii[:, 0] != ii[:, 2])]
        n = np.int64(len(uq))
        d = np.vstack([ii[:, [0, 1]], ii[:, [1, 2]], ii[:, [2, 0]]])
        a, b = d[:, 0], d[:, 1]
        _, c = np.unique(np.minimum(a, b) * n + np.maximum(a, b), return_counts=True)
        ml = mn.meshFromFacesVerts(np.asarray(bff, np.int32), np.asarray(bv, float))
        sx = mr.findSelfCollidingTriangles(mr.MeshPart(ml)).size()
        return i, int((c == 1).sum()), int((c > 2).sum()), int(sx), len(f)
    except Exception:
        return i, -1, -1, -1, len(f)


if __name__ == "__main__":
    tr = Transformer.from_crs("EPSG:3414", "EPSG:4326", always_xy=True)
    lo, la = tr.transform([B[0], B[2]], [B[1], B[3]])
    P = extract_domain_buildings(domain_leaf_tiles(min(lo), min(la), max(lo), max(la)),
                                 box(*B), store_dir="data/onemap_store")
    jobs = []
    for i, p in enumerate(P):
        try:
            sv, sf = seal_piece(p["verts"], p["faces"])
        except Exception:
            continue
        sv = np.asarray(sv, float); sf = np.asarray(sf)
        if len(sf) >= 4:
            jobs.append((len(jobs), sv - sv.mean(axis=0), sf))
    print(f"{len(jobs)} pieces, each ALONE, eps={EPS}, {NPROC} procs", flush=True)
    t0 = time.time()
    with Pool(NPROC) as pool:
        res = pool.map(one, jobs)
    ok = [r for r in res if r[2] >= 0]
    nm = np.array([r[2] for r in ok])
    sx = np.array([r[3] for r in ok])
    op = np.array([r[1] for r in ok])
    print(f"[{time.time()-t0:.0f}s] {len(ok)} succeeded, {len(res)-len(ok)} failed\n")
    print(f"INTRINSIC per-piece non-manifold edges (piece alone):")
    print(f"   TOTAL over all pieces = {nm.sum()}")
    print(f"   pieces with NM>0      = {int((nm>0).sum())} / {len(ok)}  "
          f"({(nm>0).mean():.1%})")
    print(f"   mean={nm.mean():.2f}  median={np.median(nm):.0f}  max={nm.max()}")
    print(f"   open edges total      = {op.sum()}")
    print(f"   selfX total           = {sx.sum()}")
    print(f"\n   distribution: " + "  ".join(
        f"NM={k}:{int((nm==k).sum())}" for k in sorted(set(nm.tolist()))[:10]))
    worst = sorted(ok, key=lambda r: -r[2])[:8]
    print(f"\n   worst pieces (idx, NM, faces): " +
          "  ".join(f"({r[0]},{r[2]},{r[4]})" for r in worst))
