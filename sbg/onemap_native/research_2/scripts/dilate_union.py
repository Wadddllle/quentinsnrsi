"""dilate -> manifold3d union.  The ordering that has never been tried.

Sec 10.2 tried dilate -> fTetWild (356->235 NM at 10cm, OOM at 20cm). That could
not converge: fTetWild's eps IS the pinch generator (Sec 10.1), so dilating closes
the original gaps and eps manufactures fresh ones at the new near-contacts.

manifold3d's tolerance is 2.29e-06 m -- 43,000x smaller than fTetWild's eps=0.10 --
so nothing downstream of the dilation can fuse a sub-tolerance gap into a pinch.

Measured cost of the dilation itself (all 287 sealed buildings, first order):
    dilate    dVol/Vol     dFrontal/F      <- frontal area is what drives blockage
      1cm       0.68%         0.10%
      2cm       1.36%         0.20%        <- below the 0.27-0.31% frontal noise
      5cm       3.39%         0.51%           floor already measured for the voxel path
against production decimate_error 2.5 m and fTetWild eps 0.10 m.
Target gap distribution (Sec 9.7): median 2.2 cm, p90 5.4 cm, 176/199 pairs < 5 cm.
"""
import sys, os; sys.path.insert(0, '/home/quentin/snrsi'); os.chdir('/home/quentin/snrsi')
import time, warnings
import numpy as np
from multiprocessing import Pool
from shapely.geometry import box
from pyproj import Transformer

warnings.filterwarnings('ignore')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from repair_ftw import audit                       # noqa: E402
from union_manifold3d import prepare               # noqa: E402

B = tuple(float(x) for x in os.environ.get('BBOX', '28941,28758,29341,29158').split(','))
DILATIONS = [float(x) for x in os.environ.get('DILATE', '0,0.02,0.05').split(',')]
SEG = 8
SCRATCH = '/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583/scratchpad'
CACHE = f'{SCRATCH}/prepared_solids.npz'


def frontal(v, f):
    """Projected frontal area per axis -- the blockage quantity that drives dispersion."""
    import trimesh
    m = trimesh.Trimesh(v, f, process=False)
    n, a = m.face_normals, m.area_faces
    return np.array([0.5 * np.sum(a * np.abs(n[:, k])) for k in range(3)])


def build_solids():
    from sbg.onemap_native.tiles import domain_leaf_tiles
    from sbg.onemap_native.extract import extract_domain_buildings, seal_piece
    tr = Transformer.from_crs('EPSG:3414', 'EPSG:4326', always_xy=True)
    lo, la = tr.transform([B[0], B[2]], [B[1], B[3]])
    P = extract_domain_buildings(domain_leaf_tiles(min(lo), min(la), max(lo), max(la)),
                                 box(*B), store_dir='data/onemap_store')
    S = []
    for p in P:
        try:
            sv, sf = seal_piece(p['verts'], p['faces'])
        except Exception:
            continue
        sv = np.asarray(sv, float); sf = np.asarray(sf)
        if len(sf) >= 4:
            c = sv.mean(axis=0)
            S.append((sv - c, sf, c))
    with Pool(10) as pool:
        res = pool.map(prepare, [(i, v, f, c) for i, (v, f, c) in enumerate(S)])
    tags = {}
    for _, _, tg, _ in res:
        tags[tg] = tags.get(tg, 0) + 1
    print('[per-building] ' + '  '.join(f'{k}={v}' for k, v in sorted(tags.items())), flush=True)
    return [(i, m) for i, m, tg, _ in res if m is not None]


def dilate_one(arg):
    """minkowski_sum with a small sphere. A per-vertex normal offset self-intersects
    at concave features; minkowski is exact."""
    import manifold3d as m3
    i, v, f, d = arg
    msh = m3.Mesh(vert_properties=np.ascontiguousarray(v, np.float32),
                  tri_verts=np.ascontiguousarray(f, np.uint32))
    msh.merge()
    mm = m3.Manifold(msh)
    if d > 0:
        mm = mm.minkowski_sum(m3.Manifold.sphere(d, SEG))
    om = mm.to_mesh()
    return (i, np.asarray(om.vert_properties, float)[:, :3],
            np.asarray(om.tri_verts, np.int64))


if __name__ == '__main__':
    import trimesh, manifold3d as m3
    import meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn

    if os.path.exists(CACHE):
        z = np.load(CACHE, allow_pickle=True)
        solids = [(int(i), (z[f'v{i}'], z[f'f{i}'])) for i in z['ids']]
        print(f'[cache] {len(solids)} prepared solids loaded')
    else:
        solids = build_solids()
        d = {'ids': np.array([i for i, _ in solids])}
        for i, (v, f) in solids:
            d[f'v{i}'], d[f'f{i}'] = v, f
        np.savez_compressed(CACHE, **d)
        print(f'[cache] wrote {len(solids)} prepared solids')

    ORIGIN = np.mean([v.mean(axis=0) for _, (v, f) in solids], axis=0)
    local = [(i, v - ORIGIN, f) for i, (v, f) in solids]

    print(f'\n{"dilate":>7} {"dilate s":>9} {"union s":>8} {"tris":>10} {"comps":>6} '
          f'{"NM":>6} {"bowtie":>7} {"selfX":>7} {"vol m3":>12} {"dVol":>7} {"frontalXY":>11} {"dFrontal":>9}')
    base = None
    for d in DILATIONS:
        t0 = time.time()
        with Pool(10) as pool:
            dil = pool.map(dilate_one, [(i, v, f, d) for i, v, f in local])
        td = time.time() - t0

        mans = []
        for i, v, f in dil:
            msh = m3.Mesh(vert_properties=np.ascontiguousarray(v, np.float32),
                          tri_verts=np.ascontiguousarray(f, np.uint32))
            msh.merge()
            mm = m3.Manifold(msh)
            if mm.status() == m3.Error.NoError:
                mans.append(mm)

        t1 = time.time()
        u = m3.Manifold.batch_boolean(mans, m3.OpType.Add)
        _ = u.num_tri()
        tu = time.time() - t1

        om = u.to_mesh()
        uv = np.asarray(om.vert_properties, float)[:, :3]
        uf = np.asarray(om.tri_verts, np.int64)
        a = audit(uv, uf)                       # LOCAL frame -- never at absolute SVY21
        fr = frontal(uv, uf)
        vol = abs(u.volume())
        if base is None:
            base = (vol, fr)
        dv = 100 * (vol - base[0]) / base[0]
        df = 100 * (fr[:2].mean() - base[1][:2].mean()) / base[1][:2].mean()
        print(f'{d*100:>5.0f}cm {td:>9.1f} {tu:>8.1f} {u.num_tri():>10,} {a["comps"]:>6} '
              f'{a["nm"]:>6} {a["bow"]:>7} {a["selfx"]:>7} {vol:>12,.0f} {dv:>+6.2f}% '
              f'{fr[:2].mean():>11,.0f} {df:>+8.2f}%', flush=True)

        out = f'data/wt_raw_test/duxton_dilate{int(d*100)}cm.stl'
        trimesh.Trimesh(uv + ORIGIN, uf, process=False).export(out)
