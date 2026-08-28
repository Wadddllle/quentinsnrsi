#!/usr/bin/env python
"""Island-wide per-building fTetWild cache builder (EXPERIMENT).

Cleans every building in data/onemap_store into an individually
closed + manifold + intersection-free solid, ONCE, so per-cutout cost is a
lookup instead of a mesh repair.

WHY THIS MIGHT WORK WHERE PER-DOMAIN fTetWild FAILED (see PROBLEM_BRIEF s10):
  ~89% of domain-level non-manifold edges come from buildings IN CONTACT,
  not from inside buildings (356 domain NM vs 40 intrinsic on Duxton).
  Processing each building ALONE means no tolerance operation ever spans two
  buildings, so the 2.2 cm inter-building gap is never seen, never closed,
  and never becomes a pinch.

DESIGN RULES LEARNED THE HARD WAY:
  * BOUNDED ladder. eps=0.005 on a 12k-face piece is effectively unbounded and
    a handful of tail pieces will eat the whole run. Hard per-attempt timeout;
    anything that blows it goes to the retry queue instead of blocking.
  * VERIFY BEFORE STORE. Never cache an unaudited solid. 1 of 287 Duxton pieces
    came back EMPTY from fTetWild while still reporting success.
  * fTetWild is NON-DETERMINISTIC (s9.9) -- a stored+verified solid is clean by
    construction, which is exactly why caching is the right shape for it.
  * Local frame + centre offset, so the cache is domain-independent.

Resumable: one .npz per tile, existing outputs are skipped.

  python sbg/onemap_native/research_2/scripts/build_ftw_cache.py \
      --store data/onemap_store --out data/ftw_cache --procs 10
"""
import argparse, io, contextlib, os, sys, time, signal, traceback
from pathlib import Path
from multiprocessing import Pool
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

LADDER = [0.15, 0.05, 0.015]        # metres; bounded on purpose
TIMEOUT_S = {0.15: 90, 0.05: 180, 0.015: 300}
MIN_FACES = 4

# fTetWild is NON-DETERMINISTIC (randomised perturbation, PROBLEM_BRIEF s9.9), so
# a piece can fail once and succeed on an identical re-run. Measured: piece 216 of
# Duxton returned EMPTY once, then produced 804/817/788 tets on three retries at the
# SAME eps -- a perfectly healthy 612 m^3 building, 20 verts, watertight, euler 2.
# Retry the same eps before descending: descending is far more expensive, and a
# smaller eps does not fix a coin-flip.
ATTEMPTS_PER_EPS = 3


def _boundary(tv, tt):
    q = np.vstack([tt[:, [0, 1, 2]], tt[:, [0, 1, 3]],
                   tt[:, [0, 2, 3]], tt[:, [1, 2, 3]]])
    opp = np.concatenate([tt[:, 3], tt[:, 2], tt[:, 1], tt[:, 0]])
    _, idx, cnt = np.unique(np.sort(q, axis=1), axis=0,
                            return_index=True, return_counts=True)
    k = idx[cnt == 1]
    bf = q[k]
    a, b, c = tv[bf[:, 0]], tv[bf[:, 1]], tv[bf[:, 2]]
    flip = np.einsum('ij,ij->i', np.cross(b - a, c - a), tv[opp[k]] - a) > 0
    bf[flip] = bf[flip][:, [0, 2, 1]]
    used = np.unique(bf)
    rm = np.full(len(tv), -1, np.int64)
    rm[used] = np.arange(len(used))
    return tv[used], rm[bf]


def _audit(v, f):
    """Strict exact-position float32 weld -- the weld STL forces. Returns
    (open_edges, non_manifold_edges). meshlib's hole count disagrees with this
    and has been wrong in BOTH directions on this data; trust the weld."""
    v = np.ascontiguousarray(np.asarray(v, np.float32))
    uq, inv = np.unique(v.view([('', np.float32)] * 3).ravel(), return_inverse=True)
    i = inv.astype(np.int64)[np.ascontiguousarray(f).astype(np.int64)]
    i = i[(i[:, 0] != i[:, 1]) & (i[:, 1] != i[:, 2]) & (i[:, 0] != i[:, 2])]
    n = np.int64(len(uq))
    d = np.vstack([i[:, [0, 1]], i[:, [1, 2]], i[:, [2, 0]]])
    a, b = d[:, 0], d[:, 1]
    _, c = np.unique(np.minimum(a, b) * n + np.maximum(a, b), return_counts=True)
    return int((c == 1).sum()), int((c > 2).sum())


class _Timeout(Exception):
    pass


def _alarm(signum, frame):
    raise _Timeout()


class _MuteFD:
    """Silence fTetWild at the OS level.

    contextlib.redirect_stdout only rebinds Python's sys.stdout; fTetWild writes
    from C++ straight to fd 1, so it leaks straight past it. Unmuted this run
    produces GBs of log over a full island pass.
    """

    def __enter__(self):
        self._null = os.open(os.devnull, os.O_WRONLY)
        self._saved = [os.dup(1), os.dup(2)]
        os.dup2(self._null, 1)
        os.dup2(self._null, 2)
        return self

    def __exit__(self, *exc):
        os.dup2(self._saved[0], 1)
        os.dup2(self._saved[1], 2)
        for fd in (*self._saved, self._null):
            os.close(fd)
        return False


def clean_one(vsrc, fsrc):
    """-> (verts, faces, centre, eps, tries) or (None, None, None, None, tries)."""
    import wildmeshing as wm
    import meshlib.mrmeshpy as mr, meshlib.mrmeshnumpy as mn
    from sbg.onemap_native.extract import seal_piece

    try:
        sv, sf = seal_piece(vsrc, fsrc)
    except Exception:
        sv, sf = vsrc, fsrc
    sv = np.asarray(sv, float)
    sf = np.asarray(sf, np.int32)
    if len(sf) < MIN_FACES:
        return None, None, None, None, 0

    ctr = sv.mean(axis=0)
    vl = sv - ctr
    diag = float(np.linalg.norm(vl.max(axis=0) - vl.min(axis=0)))
    if not np.isfinite(diag) or diag <= 0:
        return None, None, None, None, 0

    tries = 0
    for eps in LADDER:
      for _attempt in range(ATTEMPTS_PER_EPS):
          tries += 1
          signal.signal(signal.SIGALRM, _alarm)
          signal.alarm(TIMEOUT_S[eps])
          try:
              with _MuteFD():
                  t = wm.Tetrahedralizer(epsilon=eps / diag, edge_length_r=0.05,
                                         coarsen=True, max_its=0, stop_quality=10,
                                         max_threads=1)
                  t.set_mesh(vl, sf)
                  t.tetrahedralize()
                  o = t.get_tet_mesh(floodfill=True, manifold_surface=False,
                                     correct_surface_orientation=True)
              tv, tt = np.asarray(o[0], float), np.asarray(o[1])
              if len(tv) == 0 or len(tt) == 0:      # fTetWild can "succeed" empty
                  continue
              bv, bf = _boundary(tv, tt)
              if len(bv) == 0 or len(bf) < MIN_FACES:
                  continue
              op, nm = _audit(bv, bf)
              if op or nm:
                  continue
              ml = mn.meshFromFacesVerts(np.asarray(bf, np.int32),
                                         np.asarray(bv, float))
              if mr.findSelfCollidingTriangles(mr.MeshPart(ml)).size():
                  continue
              return bv.astype(np.float32), bf.astype(np.int32), ctr, eps, tries
          except (_Timeout, Exception):
              continue
          finally:
              signal.alarm(0)
    return None, None, None, None, tries


def do_tile(args):
    store, out, rel = args
    dst = Path(out) / rel
    if dst.exists():
        return None
    try:
        pieces = list(np.load(Path(store) / rel.replace(".npz", ".npy"),
                              allow_pickle=True))
    except Exception:
        return dict(rel=rel, n=0, ok=0, fail=0, t=0.0, err="load")

    t0 = time.time()
    blobs, meta, nfail = {}, [], 0
    for j, p in enumerate(pieces):
        v, f, ctr, eps, tries = clean_one(np.asarray(p["verts"], float),
                                          np.asarray(p["faces"], np.int32))
        if v is None:
            nfail += 1
            meta.append((j, -1.0, tries, 0))
            continue
        blobs[f"v{j}"] = v
        blobs[f"f{j}"] = f
        blobs[f"c{j}"] = np.asarray(ctr, np.float64)
        meta.append((j, eps, tries, len(f)))
    dst.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(dst, meta=np.array(meta, float), **blobs)
    return dict(rel=rel, n=len(pieces), ok=len(pieces) - nfail,
                fail=nfail, t=time.time() - t0, err="")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default="data/onemap_store")
    ap.add_argument("--out", default="data/ftw_cache")
    ap.add_argument("--procs", type=int, default=max(1, os.cpu_count() - 2))
    ap.add_argument("--limit", type=int, default=0, help="first N tiles (smoke test)")
    a = ap.parse_args()

    store = Path(a.store)
    tiles = sorted(str(p.relative_to(store)).replace(".npy", ".npz")
                   for p in store.rglob("*.npy"))
    if a.limit:
        tiles = tiles[:a.limit]
    done = sum(1 for t in tiles if (Path(a.out) / t).exists())
    todo = [(a.store, a.out, t) for t in tiles if not (Path(a.out) / t).exists()]
    print(f"{len(tiles)} tiles, {done} already done, {len(todo)} to do, "
          f"{a.procs} procs", flush=True)

    t0 = time.time()
    N = OK = FAIL = 0
    with Pool(a.procs) as pool:
        for k, r in enumerate(pool.imap_unordered(do_tile, todo, chunksize=1), 1):
            if r is None:
                continue
            N += r["n"]; OK += r["ok"]; FAIL += r["fail"]
            if k % 25 == 0 or k == len(todo):
                el = time.time() - t0
                eta = el / k * (len(todo) - k)
                print(f"[{k}/{len(todo)} tiles] {N:,} bldgs  "
                      f"ok {OK:,} ({OK/max(N,1):.1%})  fail {FAIL:,}  "
                      f"{el/60:.1f} min elapsed, ETA {eta/60:.0f} min", flush=True)
    print(f"\nDONE  {N:,} buildings, {OK:,} cached ({OK/max(N,1):.1%}), "
          f"{FAIL:,} failed, {(time.time()-t0)/60:.1f} min")
    print(f"cache size: {sum(f.stat().st_size for f in Path(a.out).rglob('*.npz'))/1e9:.2f} GB")


if __name__ == "__main__":
    main()
