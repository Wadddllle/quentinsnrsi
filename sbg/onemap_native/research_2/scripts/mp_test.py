"""ManifoldPlus vs DMC vs fTetWild on the same building.

MP marks a voxel occupied if it INTERSECTS a triangle (no signed distance), so a
sub-voxel wall still occupies its voxels, then projects extracted vertices back
onto the reference mesh. That is the mechanism that should beat DMC on thin
walls despite both using a grid.

Also tests RAW UNSEALED soup as input -- MP claims to eat triangle soup, which
would let us drop seal_piece entirely.
"""
import sys, os, subprocess, time
sys.path.insert(0, "/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import numpy as np, trimesh
import meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
from shapely.geometry import box
from pyproj import Transformer
from sbg.onemap_native.tiles import domain_leaf_tiles
from sbg.onemap_native.extract import extract_domain_buildings, seal_piece

PIECE = int(os.environ.get("PIECE", "26"))
BIN = "/home/quentin/snrsi/ManifoldPlus/build/manifold"
OUT = "data/wt_raw_test"
SCRATCH = "/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583/scratchpad"
B = (28941, 28758, 29341, 29158)


def mlmesh(v, f):
    return mn.meshFromFacesVerts(np.asarray(f, np.int32), np.asarray(v, float))


def strict(v, f):
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


def report(tag, v, f, rawv, t, out=None):
    op, nm, wd = strict(v, f)
    m = mlmesh(v, f)
    sx = mr.findSelfCollidingTriangles(mr.MeshPart(m)).size()
    tm = trimesh.Trimesh(np.asarray(v, float), np.asarray(f), process=False)
    d = np.abs(trimesh.proximity.ProximityQuery(tm).signed_distance(rawv))
    print(f"{tag:26s} f={len(f):>8,} comp={mr.MeshComponents.getAllComponents(m).size():>4} "
          f"open={op:>5} NM={nm:>4} wind={wd:>5} selfX={sx:>6,} vol={tm.volume:>11,.0f} "
          f"cov<10cm={float((d<0.10).mean()):6.1%} cov<25cm={float((d<0.25).mean()):6.1%} "
          f"{t:6.1f}s", flush=True)
    if out:
        tm.export(os.path.join(OUT, out))


tr = Transformer.from_crs("EPSG:3414", "EPSG:4326", always_xy=True)
lo, la = tr.transform([B[0], B[2]], [B[1], B[3]])
pieces = extract_domain_buildings(domain_leaf_tiles(min(lo), min(la), max(lo), max(la)),
                                  box(*B), store_dir="data/onemap_store")
rv = np.asarray(pieces[PIECE]["verts"], float)
rf = np.asarray(pieces[PIECE]["faces"])
sv, sf = seal_piece(rv, rf)
sv = np.asarray(sv, float); sf = np.asarray(sf)
ctr = sv.mean(axis=0)
sv, rv = sv - ctr, rv - ctr
os.makedirs(OUT, exist_ok=True)
print(f"piece {PIECE}: raw soup {len(rf):,} faces, sealed {len(sf):,} faces")
print(f"bbox diag {np.linalg.norm(sv.max(0)-sv.min(0)):.1f}m\n")

for name, (iv, iff) in {"sealed": (sv, sf), "RAWsoup": (rv, rf)}.items():
    inp = f"{SCRATCH}/mp_in_{name}.obj"
    trimesh.Trimesh(iv, iff, process=False).export(inp)
    for depth in (8, 9, 10):
        outp = f"{SCRATCH}/mp_out_{name}_{depth}.obj"
        t0 = time.time()
        r = subprocess.run([BIN, "--input", inp, "--output", outp, "--depth", str(depth)],
                           capture_output=True, text=True, timeout=1800)
        dt = time.time() - t0
        if not os.path.exists(outp):
            print(f"MP {name} depth={depth}   FAILED rc={r.returncode} "
                  f"{(r.stderr or r.stdout)[-120:]}")
            continue
        m = trimesh.load(outp, process=False)
        report(f"MP {name} depth={depth}", m.vertices, m.faces, sv, dt,
               out=f"P{PIECE}_mp_{name}_d{depth}.stl")
    print()
