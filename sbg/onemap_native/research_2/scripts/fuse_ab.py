"""Can meshlib replace Blender for the ONE remaining Blender job (voxel remesh)?

Current production split (sbg/onemap_native/build.py):
    terrain solidify        -> Python  (terrain_flat_base_solid; Blender's is skipped)
    join + VOXEL REMESH     -> Blender <-- the only remaining Blender dependency
    decimate / clip / polish-> meshlib

If meshlib matches on quality and speed, Blender drops out entirely: no ~300 MB
install, no hardcoded BLENDER_PATH, much smaller container.

Input is the REAL production intermediate (terrain.ply + buildings.ply written by
build.py), so this is like-for-like, not a synthetic case. Blender's own fused.ply
from the same run is the reference.

The soup is deliberately NOT closed (overlapping buildings plunged into a terrain
slab -- that is the whole point of remeshing rather than booleaning), so sign
detection matters: meshToLevelSet documents "closed surface is required", while
offsetMesh exposes signDetectionMode and can use winding rules instead.
"""
import sys, os, time; sys.path.insert(0, '/home/quentin/snrsi'); os.chdir('/home/quentin/snrsi')
import warnings; warnings.filterwarnings('ignore')
import numpy as np

W = ('/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583'
     '/scratchpad/fuse_ab/work')
VOXEL = float(os.environ.get('VOXEL', '2.0'))


def audit(v, f, label, secs, note=''):
    """meshlib holes is authoritative for THIS mesh class; a strict positional
    re-weld is the second opinion (they disagree, and which one is right depends
    on the mesh -- see PROBLEM_BRIEF 13.8)."""
    import meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn, trimesh
    ml = mn.meshFromFacesVerts(np.asarray(f, np.int32), np.asarray(v, float))
    holes = mr.findRegionBoundaryUndirectedEdgesInsideMesh(ml, ml.topology.getValidFaces()).count() \
        if False else len(mr.findHoleComplicatingFaces(ml)) if False else None
    try:
        holes = mr.findHoleRepresentiveEdges(ml).size()
    except Exception:
        holes = int(ml.topology.findHoleRepresentiveEdges().size())
    try:
        sx = mr.findSelfCollidingTriangles(mr.MeshPart(ml)).size()
    except Exception:
        sx = -1
    comps = mr.MeshComponents.getAllComponents(ml).size()
    t = trimesh.Trimesh(np.asarray(v, float), np.asarray(f, np.int64), process=True)
    print(f'{label:<26} {secs:>7.1f}s {len(f):>10,} {holes:>6} {comps:>6} {sx:>8} '
          f'{abs(t.volume):>14,.0f} {str(t.is_watertight):>6} {t.body_count:>5}  {note}',
          flush=True)
    return dict(faces=len(f), holes=holes, comps=comps, selfx=sx, vol=abs(t.volume))


def load_soup():
    import trimesh
    te = trimesh.load(f'{W}/terrain.ply', process=False)
    bl = trimesh.load(f'{W}/buildings.ply', process=False)
    v = np.vstack([np.asarray(te.vertices), np.asarray(bl.vertices)])
    f = np.vstack([np.asarray(te.faces), np.asarray(bl.faces) + len(te.vertices)])
    return v, f


if __name__ == '__main__':
    import meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn, trimesh

    print(f'voxel = {VOXEL} m\n')
    print(f'{"method":<26} {"time":>8} {"faces":>10} {"holes":>6} {"comps":>6} '
          f'{"selfX":>8} {"volume m3":>14} {"strict":>6} {"bod":>5}')
    print('-' * 110)

    # ---- reference: what Blender actually produced in the production run
    fp = trimesh.load(f'{W}/fused.ply', process=False)
    ref = audit(np.asarray(fp.vertices), np.asarray(fp.faces),
                'BLENDER (production)', 3.4, '<- reference')

    v, f = load_soup()
    print(f'\n  [input soup: {len(f):,} faces, {len(v):,} verts, NOT closed by design]\n')

    # meshlib needs the soup as one Mesh; it tolerates non-manifold input here
    # because every path below voxelises rather than reasoning about topology.
    src = mn.meshFromFacesVerts(np.asarray(f, np.int32), np.asarray(v, float))
    mp = mr.MeshPart(src)

    for name, mode in [('offsetMesh OpenVDB', mr.SignDetectionMode.OpenVDB),
                       ('offsetMesh HoleWinding', mr.SignDetectionMode.HoleWindingRule),
                       ('offsetMesh Unsigned', mr.SignDetectionMode.Unsigned)]:
        try:
            p = mr.OffsetParameters()
            p.voxelSize = VOXEL
            p.signDetectionMode = mode
            t0 = time.time()
            out = mr.offsetMesh(mp, 0.0, p)
            dt = time.time() - t0
            audit(mn.getNumpyVerts(out), mn.getNumpyFaces(out.topology), name, dt)
        except Exception as e:
            print(f'{name:<26} FAILED  {type(e).__name__}: {str(e)[:60]}')

    # mcOffsetMesh -- standard marching cubes rather than dual
    try:
        p = mr.OffsetParameters(); p.voxelSize = VOXEL
        p.signDetectionMode = mr.SignDetectionMode.OpenVDB
        t0 = time.time(); out = mr.mcOffsetMesh(mp, 0.0, p); dt = time.time() - t0
        audit(mn.getNumpyVerts(out), mn.getNumpyFaces(out.topology), 'mcOffsetMesh OpenVDB', dt)
    except Exception as e:
        print(f'{"mcOffsetMesh OpenVDB":<26} FAILED  {type(e).__name__}: {str(e)[:60]}')

    # meshToLevelSet + gridToMesh -- the "pure" DMC path. Documented as requiring a
    # CLOSED surface, which our soup is not; included to find out what it does anyway.
    try:
        t0 = time.time()
        grid = mr.meshToLevelSet(mp, mr.AffineXf3f(), mr.Vector3f(VOXEL, VOXEL, VOXEL), 3.0)
        s = mr.GridToMeshSettings()
        s.voxelSize = mr.Vector3f(VOXEL, VOXEL, VOXEL)
        s.isoValue = 0.0
        s.adaptivity = 0.0
        out = mr.gridToMesh(grid, s)
        dt = time.time() - t0
        audit(mn.getNumpyVerts(out), mn.getNumpyFaces(out.topology),
              'meshToLevelSet+gridToMesh', dt, '(docs: needs closed input)')
    except Exception as e:
        print(f'{"meshToLevelSet+gridToMesh":<26} FAILED  {type(e).__name__}: {str(e)[:60]}')
