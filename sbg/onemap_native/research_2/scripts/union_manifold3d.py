"""Full-domain assembly: fTetWild + retry + pymeshfix per building, then
manifold3d guaranteed-manifold boolean union of the whole domain.

This is the test that killed every previous assembly attempt:
  Sec 12.5 GATE 1  -- plain concatenation FAILS (180/684 overlapping pairs
                      genuinely intersect, median 22 verts strictly inside)
  Sec 12.6         -- CSG difference REJECTED by DAGMC (fixed 0 of 149, made 27)
  Sec 12.7/12.8    -- group-merge only reached 88% of groups, missing 48% of
                      the domain by building count
  Sec 10.2         -- meshlib native union: NM=0 xor selfX=0, never both

manifold3d's contract is guaranteed-manifold output for manifold input, which
Sec 12-repair now supplies. IMPORTANT: pieces must be unioned in REAL domain
coordinates -- the per-piece recentring used for the fTetWild numerics has to be
added back, or every building lands on the origin.
"""
import sys, os; sys.path.insert(0, '/home/quentin/snrsi'); os.chdir('/home/quentin/snrsi')
import io, time, contextlib, warnings
import numpy as np
from multiprocessing import Pool
from shapely.geometry import box
from pyproj import Transformer

warnings.filterwarnings('ignore')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from repair_ftw import audit, is_clean, ftw, rep_pymeshfix, m3d_ok   # noqa: E402

B = tuple(float(x) for x in os.environ.get('BBOX', '28941,28758,29341,29158').split(','))
EPS = float(os.environ.get('EPS', '0.10'))
TRIALS = int(os.environ.get('TRIALS', '3'))
OUT = os.environ.get('OUT', 'data/wt_raw_test/duxton_manifold3d_union.stl')


def prepare(arg):
    """fTetWild with retry, then pymeshfix if still defective. Returns REAL-coord mesh."""
    i, v, f, ctr = arg
    v = np.asarray(v, float); f = np.asarray(f, np.int32)
    # Acceptance is BOTH our topology audit AND manifold3d actually taking it --
    # our audit alone missed winding and passed 281 meshes manifold3d refused.
    best, tag = None, 'FAIL'
    for k in range(TRIALS):
        try:
            bv, bf = ftw(v, f, EPS, k)
        except Exception:
            continue
        if is_clean(audit(bv, bf)) and m3d_ok(bv, bf):
            best, tag = (bv, bf), ('first' if k == 0 else f'retry{k}')
            break
        if best is None:
            best = (bv, bf)
    if best is None:
        return i, None, 'FTW_FAIL', 0.0
    if tag == 'FAIL':
        try:
            rv, rf = rep_pymeshfix(*best)
            tag = 'pymeshfix' if (is_clean(audit(rv, rf)) and m3d_ok(rv, rf)) else 'STILL_BAD'
            if tag == 'pymeshfix':
                best = (rv, rf)
        except Exception:
            tag = 'PMF_ERR'
    bv, bf = best
    import trimesh
    return i, (bv + ctr, bf), tag, abs(trimesh.Trimesh(bv, bf, process=False).volume)


if __name__ == '__main__':
    import trimesh, manifold3d as m3
    import meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
    from sbg.onemap_native.tiles import domain_leaf_tiles
    from sbg.onemap_native.extract import extract_domain_buildings, seal_piece

    t0 = time.time()
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
    print(f'[extract] {len(S)} sealed pieces  ({time.time()-t0:.1f}s)', flush=True)

    t1 = time.time()
    with Pool(10) as pool:
        res = pool.map(prepare, [(i, v, f, c) for i, (v, f, c) in enumerate(S)])
    tags = {}
    for _, _, tg, _ in res:
        tags[tg] = tags.get(tg, 0) + 1
    solids = [(i, m) for i, m, tg, _ in res if m is not None]
    print(f'[per-building] {time.time()-t1:.1f}s   ' +
          '  '.join(f'{k}={v}' for k, v in sorted(tags.items())), flush=True)

    # ---- ingest into manifold3d (the gate: it refuses anything non-manifold)
    #
    # CRITICAL: manifold3d.Mesh takes float32 vert_properties. At real EPSG:3414
    # coords (~29,000 m) float32 spacing is ~2 mm, so merge() welds genuinely
    # distinct vertices and MANUFACTURES non-manifoldness -- 279/287 refused on the
    # first run of this script. Union in a domain-local frame (|coord| < ~200 m,
    # spacing ~15 um) and restore the offset at export. Same trap as meshlib's 1 mm
    # quantisation at these coordinates.
    ORIGIN = np.mean([v.mean(axis=0) for _, (v, f) in solids], axis=0)
    print(f'[frame] domain-local origin {ORIGIN.round(1)} '
          f'(float32 at ~{np.abs(ORIGIN).max():.0f} m spacing '
          f'{np.spacing(np.float32(np.abs(ORIGIN).max())):.1e} m)', flush=True)

    t2 = time.time()
    mans, refused, src_vol = [], [], 0.0
    for i, (v, f) in solids:
        msh = m3.Mesh(vert_properties=np.ascontiguousarray(v - ORIGIN, np.float32),
                      tri_verts=np.ascontiguousarray(f, np.uint32))
        msh.merge()
        mm = m3.Manifold(msh)
        if mm.status() != m3.Error.NoError:
            refused.append((i, str(mm.status())))
            continue
        if mm.volume() < 0:
            mm = mm.mirror([1, 0, 0]).mirror([1, 0, 0])   # no-op guard; flip handled above
        mans.append(mm)
        src_vol += abs(mm.volume())
    print(f'[ingest] {len(mans)} accepted, {len(refused)} refused  ({time.time()-t2:.1f}s)',
          flush=True)
    for i, s in refused[:10]:
        print(f'    refused piece {i}: {s}')

    # ---- the union
    t3 = time.time()
    u = m3.Manifold.batch_boolean(mans, m3.OpType.Add)
    tu = time.time() - t3
    print(f'\n[union] batch_boolean of {len(mans)} solids in {tu:.1f}s')
    print(f'  status={u.status()}  genus={u.genus()}  tris={u.num_tri():,}  '
          f'vol={u.volume():,.1f} m3   (sum of parts {src_vol:,.1f}, '
          f'overlap removed {src_vol - u.volume():,.1f})', flush=True)

    # did the boolean actually do CSG? check a pair we KNOW interpenetrates
    import itertools
    pair = None
    for x, y in itertools.combinations(range(min(len(mans), 60)), 2):
        vx, vy = mans[x].volume(), mans[y].volume()
        s = (mans[x] + mans[y]).volume()
        if vx + vy - s > 1.0:
            pair = (x, y, vx, vy, s); break
    if pair:
        x, y, vx, vy, s = pair
        print(f'[csg check] pair ({x},{y}) A={vx:.1f} B={vy:.1f} A+B={vx+vy:.1f} '
              f'union={s:.1f} -> overlap {vx+vy-s:.1f} m3  CSG IS WORKING')
    else:
        print('[csg check] no interpenetrating pair found in first 60 -- '
              'either they really are disjoint, or Add is not doing CSG')

    om = u.to_mesh()
    uv_local = np.asarray(om.vert_properties, float)[:, :3]
    uf = np.asarray(om.tri_verts, np.int64)
    uv = uv_local + ORIGIN

    # ---- audit in the LOCAL frame. A float32 weld at absolute SVY21 coords
    # quantises to ~2 mm and manufactures defects that are not in the geometry.
    al = audit(uv_local, uf)
    print(f'\n[audit LOCAL frame] open={al["open"]} nm={al["nm"]} bowtie={al["bow"]} '
          f'wind={al["wind"]} selfX={al["selfx"]} comps={al["comps"]}')
    a = audit(uv, uf)
    t = trimesh.Trimesh(uv, uf, process=True)
    ml = mn.meshFromFacesVerts(np.asarray(uf, np.int32), uv)
    print(f'[audit ABS  frame] our metric : open={a["open"]} nm={a["nm"]} bowtie={a["bow"]} '
          f'selfX={a["selfx"]} euler={a["euler"]}')
    print(f'[audit] trimesh    : watertight={t.is_watertight} bodies={t.body_count} '
          f'euler={t.euler_number} vol={abs(t.volume):,.1f}')
    print(f'[audit] meshlib    : selfX={mr.findSelfCollidingTriangles(mr.MeshPart(ml)).size()} '
          f'comps={mr.MeshComponents.getAllComponents(ml).size()}')

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    trimesh.Trimesh(uv, uf, process=False).export(OUT)
    print(f'\nwrote {OUT}   total {time.time()-t0:.1f}s')
