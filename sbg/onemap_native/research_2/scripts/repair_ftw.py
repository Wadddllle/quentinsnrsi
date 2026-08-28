"""Does GENERIC mesh repair fix post-fTetWild PER-BUILDING defects?

Never tested before. The standing argument against it (displacement exits a
zero-thickness pinch) came from VOXEL/DMC output and whole-domain soup, NOT from
per-building fTetWild -- and Sec 9.11 contradicts it at this scale.

Design notes, each earned by a bug in the first version of this script:
  * N TRIALS per piece. fTetWild is non-deterministic (Sec 9.9); a single run
    cannot distinguish a flake from a persistent failure. Piece 185 read NM=8
    on one run and 0/0/0/0 on the next.
  * BOWTIE (non-manifold vertex) is a separate property from non-manifold EDGE
    and must be checked separately -- 185's "clean" rerun still had euler=1,
    which is impossible for a closed orientable manifold. One bowtie vertex.
  * ORIENTATION: our tet-boundary extraction does not order faces outward, so
    volume comes out negative. Flip before judging winding.
  * pymeshfix.repair() takes (joincomp, remove_smallest_components) and NO
    verbose kwarg.
"""
import sys, os; sys.path.insert(0, '/home/quentin/snrsi'); os.chdir('/home/quentin/snrsi')
import io, contextlib, warnings
import numpy as np
from multiprocessing import Pool
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from shapely.geometry import box
from pyproj import Transformer

warnings.filterwarnings('ignore')
B = (28941, 28758, 29341, 29158)      # Duxton 400 m
EPS = 0.10
TRIALS = 3


# ---------------------------------------------------------------- audit
def bowties(f, nv):
    """Vertices whose incident faces do not form ONE fan."""
    c = np.vstack([f[:, [0, 1, 2]], f[:, [1, 2, 0]], f[:, [2, 0, 1]]])
    ctr, a, b = c[:, 0], c[:, 1], c[:, 2]
    key = np.concatenate([ctr * nv + a, ctr * nv + b])
    uq, inv = np.unique(key, return_inverse=True)
    m = len(c)
    r, cc = inv[:m], inv[m:]
    g = coo_matrix((np.ones(m), (r, cc)), shape=(len(uq), len(uq)))
    n, lab = connected_components(g, directed=False)
    owner = uq // nv
    order = np.lexsort((lab, owner))
    o_s, l_s = owner[order], lab[order]
    newv = np.r_[True, o_s[1:] != o_s[:-1]]
    newl = np.r_[True, (l_s[1:] != l_s[:-1]) | newv[1:]]
    per = np.add.reduceat(newl.astype(int), np.flatnonzero(newv))
    return int((per > 1).sum())


def audit(v, f):
    import meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
    v = np.asarray(v, float); f = np.asarray(f, np.int64)
    if len(f) < 4:
        return dict(open=-1, nm=-1, bow=-1, wind=-1, selfx=-1, comps=-1,
                    vol=0.0, faces=len(f), euler=0)
    # exact-position weld (STL-safe: float32 grid, which is what a consumer sees)
    v32 = np.ascontiguousarray(v.astype(np.float32))
    uq, inv = np.unique(v32.view([('', np.float32)] * 3).ravel(), return_inverse=True)
    ii = inv.astype(np.int64)[f]
    ii = ii[(ii[:, 0] != ii[:, 1]) & (ii[:, 1] != ii[:, 2]) & (ii[:, 0] != ii[:, 2])]
    n = np.int64(len(uq))
    d = np.vstack([ii[:, [0, 1]], ii[:, [1, 2]], ii[:, [2, 0]]])
    a, b = d[:, 0], d[:, 1]
    ek, ecnt = np.unique(np.minimum(a, b) * n + np.maximum(a, b), return_counts=True)
    _, dcnt = np.unique(a * n + b, return_counts=True)
    ml = mn.meshFromFacesVerts(np.asarray(f, np.int32), v)
    try:
        sx = mr.findSelfCollidingTriangles(mr.MeshPart(ml)).size()
    except Exception:
        sx = -1
    return dict(open=int((ecnt == 1).sum()), nm=int((ecnt > 2).sum()),
                bow=bowties(ii, n), wind=int((dcnt > 1).sum()), selfx=int(sx),
                comps=int(mr.MeshComponents.getAllComponents(ml).size()),
                vol=abs(float(ml.volume())), faces=int(len(f)),
                euler=int(n - len(ek) + len(ii)))


def is_clean(a):
    return a['open'] == 0 and a['nm'] == 0 and a['bow'] == 0 and a['selfx'] == 0


def m3d_ok(v, f):
    """The REAL consumer gate: manifold3d either accepts a solid or it does not.
    Kept separate from is_clean() because our own audit missed winding entirely
    and passed 281 meshes manifold3d refused."""
    import manifold3d as m3
    try:
        msh = m3.Mesh(vert_properties=np.ascontiguousarray(v, np.float32),
                      tri_verts=np.ascontiguousarray(f, np.uint32))
        msh.merge()
        return m3.Manifold(msh).status() == m3.Error.NoError
    except Exception:
        return False


# ---------------------------------------------------------------- fTetWild
def ftw(v, f, eps, seed_hint=0):
    import wildmeshing as wm
    diag = float(np.linalg.norm(v.max(axis=0) - v.min(axis=0)))
    with contextlib.redirect_stdout(io.StringIO()):
        t = wm.Tetrahedralizer(epsilon=eps / diag, edge_length_r=0.05, coarsen=True,
                               max_its=0, stop_quality=10, max_threads=1)
        t.set_mesh(v, f); t.tetrahedralize()
        o = t.get_tet_mesh(floodfill=True, manifold_surface=False,
                           correct_surface_orientation=True)
    tv, tt = np.asarray(o[0], float), np.asarray(o[1])
    q = np.vstack([tt[:, [0, 1, 2]], tt[:, [0, 1, 3]], tt[:, [0, 2, 3]], tt[:, [1, 2, 3]]])
    _, idx, cnt = np.unique(np.sort(q, axis=1), axis=0, return_index=True, return_counts=True)
    bf = q[idx[cnt == 1]]
    u = np.unique(bf); rm = np.full(len(tv), -1, np.int64); rm[u] = np.arange(len(u))
    bv, bff = tv[u], rm[bf]
    # Collecting tet faces used once gives the right SET of boundary faces but
    # arbitrary per-face WINDING -- measured 305-578 conflicts per piece, which
    # made signed volume cancel (135.3 vs a true 2412.0) and made manifold3d
    # refuse 281/287 pieces as NotManifold. A single global flip does not fix
    # per-face inconsistency; orient properly.
    import trimesh
    m = trimesh.Trimesh(bv, bff, process=True)
    trimesh.repair.fix_winding(m)
    trimesh.repair.fix_normals(m)
    if m.volume < 0:
        m.invert()
    return np.asarray(m.vertices, float), np.asarray(m.faces, np.int64)


# ---------------------------------------------------------------- repair
def rep_pymeshfix(v, f):
    # 0.18.1 renamed .v/.f -> .points/.faces, and repair() has NO verbose kwarg
    import pymeshfix
    m = pymeshfix.MeshFix(np.asarray(v, float), np.asarray(f, np.int32))
    m.repair(remove_smallest_components=False)
    return np.asarray(m.points, float), np.asarray(m.faces, np.int64)


def rep_pymeshfix_join(v, f):
    import pymeshfix
    m = pymeshfix.MeshFix(np.asarray(v, float), np.asarray(f, np.int32))
    m.repair(joincomp=True, remove_smallest_components=False)
    return np.asarray(m.points, float), np.asarray(m.faces, np.int64)


def rep_pymeshfix_steps(v, f):
    """MeshFix's individual stages, rather than the whole repair()."""
    import pymeshfix
    m = pymeshfix.MeshFix(np.asarray(v, float), np.asarray(f, np.int32))
    m.degeneracy_removal()
    m.intersection_removal()
    m.fill_holes()
    return np.asarray(m.points, float), np.asarray(m.faces, np.int64)


def rep_manifold3d(v, f):
    """manifold3d's Merge -- documented as the fix for 'slightly non-manifold'."""
    import manifold3d as m3
    msh = m3.Mesh(vert_properties=np.ascontiguousarray(v, np.float32),
                  tri_verts=np.ascontiguousarray(f, np.uint32))
    msh.merge()
    mm = m3.Manifold(msh)
    if mm.status() != m3.Error.NoError:
        raise RuntimeError(str(mm.status()))
    out = mm.to_mesh()
    return (np.asarray(out.vert_properties, float)[:, :3],
            np.asarray(out.tri_verts, np.int64))


def rep_meshlib(v, f):
    import meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
    ml = mn.meshFromFacesVerts(np.asarray(f, np.int32), np.asarray(v, float))
    for _ in range(4):
        mr.resolveMeshDegenerations(ml)
    ml.packOptimally(False)
    return (mn.getNumpyVerts(ml).astype(float),
            mn.getNumpyFaces(ml.topology).astype(np.int64))


def rep_trimesh(v, f):
    import trimesh
    m = trimesh.Trimesh(np.asarray(v, float), np.asarray(f, np.int64), process=True)
    m.merge_vertices()
    m.update_faces(m.nondegenerate_faces())
    m.update_faces(m.unique_faces())
    m.remove_unreferenced_vertices()
    trimesh.repair.fix_normals(m)
    trimesh.repair.fill_holes(m)
    return np.asarray(m.vertices, float), np.asarray(m.faces, np.int64)


TOOLS = [('pymeshfix', rep_pymeshfix), ('pymeshfix+join', rep_pymeshfix_join),
         ('pymeshfix steps', rep_pymeshfix_steps), ('manifold3d merge', rep_manifold3d),
         ('meshlib', rep_meshlib), ('trimesh', rep_trimesh)]
CACHE = '/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583/scratchpad/ftw_persistent.npz'


def trial(arg):
    i, k, v, f = arg
    try:
        bv, bf = ftw(np.asarray(v, float), np.asarray(f, np.int32), EPS, k)
        return i, k, audit(bv, bf), bv, bf
    except Exception as e:
        return i, k, {'err': str(e)[:60]}, None, None


def repair_one(arg):
    i, bv, bf, before = arg
    out = {'i': i, 'before': before}
    for name, fn in TOOLS:
        try:
            rv, rf = fn(bv, bf)
            out[name] = audit(rv, rf)
        except Exception as e:
            out[name] = {'err': type(e).__name__ + ': ' + str(e)[:40]}
    return out


def load_cache():
    if not os.path.exists(CACHE):
        return None
    z = np.load(CACHE, allow_pickle=True)
    return [(int(i), z[f'v{i}'], z[f'f{i}'], dict(z[f'a{i}'].item()))
            for i in z['ids']]


def save_cache(never):
    d = {'ids': np.array([i for i, _, _, _ in never])}
    for i, bv, bf, a in never:
        d[f'v{i}'], d[f'f{i}'], d[f'a{i}'] = bv, bf, np.array(a, dtype=object)
    np.savez_compressed(CACHE, **d)


if __name__ == '__main__':
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
            S.append((sv - sv.mean(axis=0), sf))
    print(f'{len(S)} sealed pieces, eps={EPS}, {TRIALS} trials each', flush=True)

    never = load_cache()
    if never is None:
        jobs = [(i, k, v, f) for i, (v, f) in enumerate(S) for k in range(TRIALS)]
        with Pool(10) as pool:
            res = pool.map(trial, jobs)

        per = {}
        for i, k, a, bv, bf in res:
            per.setdefault(i, []).append((a, bv, bf))

        always, ever, never = 0, 0, []
        for i, runs in per.items():
            ok = [is_clean(a) for a, _, _ in runs if 'err' not in a]
            if ok and all(ok):
                always += 1
            elif any(ok):
                ever += 1
            else:
                worst = min((r for r in runs if 'err' not in r[0]),
                            key=lambda r: r[0]['nm'] + r[0]['bow'] + r[0]['open'] + r[0]['selfx'],
                            default=None)
                if worst:
                    never.append((i, worst[1], worst[2], worst[0]))

        print(f'\n--- {TRIALS}-trial outcome over {len(per)} pieces ---')
        print(f'  clean on ALL {TRIALS} trials     : {always}')
        print(f'  clean on SOME trials (flake) : {ever}   <- a retry fixes these')
        print(f'  clean on NO trial (persistent): {len(never)}')
        save_cache(never)
    else:
        print(f'\n[loaded {len(never)} persistent failures from cache]')

    if not never:
        print('\nNothing persistently defective. Repair is unnecessary at eps=0.10.')
        sys.exit()

    print(f'\n--- repair on the {len(never)} persistent failures (best of {TRIALS} taken) ---')
    with Pool(10) as pool:
        rep = pool.map(repair_one, [(i, bv, bf, a) for i, bv, bf, a in never])

    def code(r):
        if 'err' in r:
            return 'ERR'
        return 'CLEAN' if is_clean(r) else f"{r['nm']}/{r['bow']}/{r['open']}/{r['selfx']}"

    print(f'\nformat: nm/bowtie/open/selfX   (CLEAN = all zero)\n')
    print(f'{"piece":>6} {"faces":>6} {"before":>12} | ' +
          ' | '.join(f'{n:>16}' for n, _ in TOOLS))
    for a in sorted(rep, key=lambda x: -(x['before']['nm'] + x['before']['bow'])):
        b = a['before']
        print(f"{a['i']:>6} {b['faces']:>6} {code(b):>12} | " +
              ' | '.join(f'{code(a[n]):>16}' for n, _ in TOOLS))

    print('\nvolume change vs the fTetWild input (a big number = the tool fabricated geometry):')
    print(f'{"piece":>6} | ' + ' | '.join(f'{n:>16}' for n, _ in TOOLS))
    for a in sorted(rep, key=lambda x: -(x['before']['nm'] + x['before']['bow'])):
        b = a['before']
        cells = []
        for n, _ in TOOLS:
            r = a[n]
            cells.append(f"{'--':>16}" if 'err' in r else
                         f"{100 * (r['vol'] - b['vol']) / b['vol'] if b['vol'] else 0:>+15.1f}%")
        print(f"{a['i']:>6} | " + ' | '.join(cells))

    print('\n=== SUMMARY (persistent failures only) ===')
    for n, _ in TOOLS:
        ok = sum(1 for a in rep if 'err' not in a[n] and is_clean(a[n]))
        err = sum(1 for a in rep if 'err' in a[n])
        vols = [100 * abs(a[n]['vol'] - a['before']['vol']) / a['before']['vol']
                for a in rep if 'err' not in a[n] and a['before']['vol']]
        print(f'  {n:>16}: FIXED {ok}/{len(rep)}   err {err}   '
              f'median |dVol| {np.median(vols) if vols else 0:.2f}%')
