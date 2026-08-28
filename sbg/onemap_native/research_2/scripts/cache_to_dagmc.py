#!/usr/bin/env python
"""Write cached per-building solids to a DAGMC .h5m and validate it (EXPERIMENT).

Runs inside the conda env that has moab+dagmc (NOT the project .venv):
    ~/tools/mamba/root/envs/dagmc/bin/python cache_to_dagmc.py --cache X.pkl

WHY THIS FILE EXISTS
  Every claim so far about "DAGMC accepts touching volumes but not overlapping
  ones" comes from DAGMC's docs, not from a run. This turns it into a
  measurement: build a real .h5m from the cache, then let DAGMC's OWN checker
  judge it.

WHAT DAGMC ACTUALLY WANTS (docs, to be confirmed by this script)
  "A model is considered watertight if the faceting of all topologically linked
  surfaces are coincident."  -- i.e. a CELL COMPLEX of volumes sharing surfaces,
  NOT one fused manifold solid. So our strict 0/0/0/0 soup audit is the
  fused-CFD-solid metric and is the WRONG gate here:
     gap between buildings  -> fine, that is air
     shared party wall      -> REQUIRED (imprint+merge), not a defect
     volumetric overlap     -> FATAL (lost particles)
"""
import argparse, pickle, subprocess, shutil, sys
from pathlib import Path
import numpy as np


def load_cache(p):
    C = [c for c in pickle.load(open(p, "rb"))
         if c.get("v") is not None and len(c["v"]) > 0 and len(c["f"]) > 0]
    return C


def write_h5m(C, out, tag="building"):
    """One DAGMC volume per building, each with its OWN local vertex array.

    Do NOT use vertices_to_h5m for this: it passes the FULL shared vertex array
    to create_vertices() once per volume (read its add_vertices_to_moab_core),
    so cost and file size are O(volumes x total_vertices). Measured: 346 KB at
    5 volumes -> 13 MB at 40 -> 622 MB at 286, for only ~97k triangles total.
    Per-volume vertices make it linear.

    Tag layout follows DAGMC's convention (same as vertices_to_h5m's, which is
    correct -- only its vertex handling is not):
      Surface set : GEOM_DIMENSION=2, CATEGORY="Surface", GEOM_SENSE_2 -> volume
      Volume  set : GEOM_DIMENSION=3, CATEGORY="Volume", parent of the surface
      Group   set : GEOM_DIMENSION=4, CATEGORY="Group",  NAME="mat:<tag>"
    """
    from pymoab import core, types

    mb = core.Core()
    t_sense = mb.tag_get_handle("GEOM_SENSE_2", 2, types.MB_TYPE_HANDLE,
                                types.MB_TAG_SPARSE, create_if_missing=True)
    t_cat = mb.tag_get_handle(types.CATEGORY_TAG_NAME, types.CATEGORY_TAG_SIZE,
                              types.MB_TYPE_OPAQUE, types.MB_TAG_SPARSE,
                              create_if_missing=True)
    t_name = mb.tag_get_handle(types.NAME_TAG_NAME, types.NAME_TAG_SIZE,
                               types.MB_TYPE_OPAQUE, types.MB_TAG_SPARSE,
                               create_if_missing=True)
    t_dim = mb.tag_get_handle(types.GEOM_DIMENSION_TAG_NAME, 1,
                              types.MB_TYPE_INTEGER, types.MB_TAG_DENSE,
                              create_if_missing=True)
    t_gid = mb.tag_get_handle(types.GLOBAL_ID_TAG_NAME)

    group = mb.create_meshset()
    mb.tag_set_data(t_cat, group, "Group")
    mb.tag_set_data(t_name, group, f"mat:{tag}")
    mb.tag_set_data(t_dim, group, 4)

    nv_total = 0
    for vid, c in enumerate(C, 1):
        v = np.ascontiguousarray(np.asarray(c["v"], float))
        f = np.asarray(c["f"], np.int64)

        surf = mb.create_meshset()
        vol = mb.create_meshset()
        mb.tag_set_data(t_gid, vol, vid)
        mb.tag_set_data(t_gid, surf, vid)
        mb.tag_set_data(t_dim, vol, 3)
        mb.tag_set_data(t_dim, surf, 2)
        mb.tag_set_data(t_cat, vol, "Volume")
        mb.tag_set_data(t_cat, surf, "Surface")
        mb.add_parent_child(vol, surf)
        mb.tag_set_data(t_sense, surf, [vol, np.uint64(0)])

        mv = mb.create_vertices(v.flatten())          # THIS volume's verts only
        mb.add_entity(surf, mv)
        for tri in f:
            mb.add_entity(surf, mb.create_element(
                types.MBTRI, (mv[int(tri[0])], mv[int(tri[1])], mv[int(tri[2])])))
        mb.add_entity(group, vol)
        nv_total += len(v)

    mb.write_file(str(out))
    return len(C), nv_total


def run(cmd):
    exe = shutil.which(cmd[0])
    if not exe:
        return f"[{cmd[0]}: not on PATH]"
    try:
        r = subprocess.run([exe] + cmd[1:], capture_output=True, text=True,
                           timeout=1800)
        return (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return f"[{cmd[0]}: TIMEOUT 1800s]"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", required=True)
    ap.add_argument("--out", default="dagmc_duxton.h5m")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    C = load_cache(a.cache)
    if a.limit:
        C = C[:a.limit]
    print(f"{len(C)} cached building solids")

    n, nv = write_h5m(C, a.out)
    sz = Path(a.out).stat().st_size / 1e6
    print(f"wrote {a.out}: {n} volumes, {nv:,} vertices, {sz:.1f} MB\n")

    print("=" * 62)
    print("DAGMC overlap_check  (overlaps => LOST PARTICLES => fatal)")
    print("=" * 62)
    print(run(["overlap_check", a.out]) or "(no output)")

    print("\n" + "=" * 62)
    print("DAGMC make_watertight  (does DAGMC consider it already sealed?)")
    print("=" * 62)
    print(run(["make_watertight", a.out]) or "(no output)")

    zip_ = Path(a.out).with_name(Path(a.out).stem + "_zip.h5m")
    if zip_.exists():
        print(f"\nsealed file: {zip_}  ({zip_.stat().st_size/1e6:.1f} MB)")
        print("re-running overlap_check on the SEALED model:")
        print(run(["overlap_check", str(zip_)]) or "(no output)")


if __name__ == "__main__":
    main()
