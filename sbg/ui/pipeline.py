"""STL pipeline orchestration -- wraps the existing CLI-scriptable chain
(topo.dtm -> topo.conforming_mesh -> `cjio export obj` -> Blender's
export_stl.py -> repair_stl.verify_and_repair) as one function runnable from
a background job (see jobs.py). No pipeline logic lives here, only
orchestration -- every stage reuses the exact same functions/scripts already
built and verified in Phase 3/Phase 5, run against whatever domain ring the
2D boundary tool committed.

Deliberately independent of the plain-cutout .city.json /api/cutout/commit
writes: this always re-derives buildings+terrain fresh from the full
in-memory dataset (app.state.cm) via conforming_overlay's own domain
filtering, since that's what a terrain-draped conforming mesh needs anyway --
the plain cutout file isn't an input to this chain at all.
"""
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from shapely.geometry import Polygon

from sbg.config import BLENDER_PATH
from sbg.io_cityjson import finalize_and_save, split_composite_solids
from sbg.topo.conforming_mesh import conforming_overlay
from sbg.topo.dtm import ElevationLookup, build_dtm, load_points, write_geotiff

BASE_DIR = Path(__file__).resolve().parent.parent.parent
EXPORT_STL_SCRIPT = BASE_DIR / "sbg" / "blender" / "export_stl.py"

# Local-CFD-domain DTM convention (see topo/dtm.py's own docstring): bbox
# already bounds the point count, so no decimation needed; 3m step matches
# a real CFD mesh resolution rather than the whole-island path's coarse 20m.
DTM_STEP = 3.0
DTM_STRIDE = 1


def domain_polygon_from_ring(ring):
    poly = Polygon(ring)
    if not poly.is_valid or poly.area <= 0:
        raise ValueError("domain ring is not a valid polygon (self-intersecting or zero area?)")
    return poly


def _run_subprocess(job, cmd, cwd=None):
    """Runs cmd, streaming stderr into job.log as it happens (this project's
    existing scripts already print their own stage-transition lines to
    stderr -- see export_stl.py -- so this gets real progress for free
    without needing separate instrumentation in each stage).
    """
    job.log_line(f"$ {' '.join(str(c) for c in cmd)}")
    proc = subprocess.Popen(
        cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        universal_newlines=True, bufsize=1,
    )
    for line in proc.stdout:
        job.log_line(line.rstrip())
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} exited with code {proc.returncode} (see the job log for output)")


def run_stl_pipeline(job, sbg_cm, ring, jobs_root, spatial_index=None):
    """Full chain for one drawn domain. Writes into jobs_root/<job.id>/ so
    concurrent jobs never collide on intermediate filenames.

    spatial_index (sbg.ui.spatial_index.SpatialIndex, already built over
    sbg_cm): passed through to conforming_overlay() so it can look up
    in-domain buildings directly instead of scanning and reconstructing all
    118,782 buildings' footprints on every job -- measured at ~15.6s wasted
    per job regardless of domain size before this was wired through.
    """
    job_dir = Path(jobs_root) / job.id
    job_dir.mkdir(parents=True, exist_ok=True)

    domain_polygon = domain_polygon_from_ring(ring)
    xmin, ymin, xmax, ymax = domain_polygon.bounds

    if not BLENDER_PATH.exists():
        raise RuntimeError(
            f"Blender not found at {BLENDER_PATH} (see sbg/config.py's BLENDER_PATH) -- "
            "install it or update that path before running the STL pipeline."
        )

    job.set_stage("dtm")
    xs, ys, zs = load_points(bbox=(xmin, ymin, xmax, ymax), stride=DTM_STRIDE)
    job.log_line(f"{len(xs)} contour points in domain bbox")
    if len(xs) == 0:
        # A real, expected case, not just a hypothetical: even domains well
        # within known-good coverage areas can have zero 20m-contour
        # crossings if the enclosed terrain is genuinely flat (confirmed
        # directly -- a 400m box inside the already-proven CBD test area
        # came back with 0 points, an 800m box in a nearby spot had 1404).
        # Fail with a clear, actionable message instead of a bare numpy
        # ValueError from build_dtm().
        raise RuntimeError(
            "No contour data found inside this domain -- it's likely too small "
            "and/or over flat terrain with no nearby 20m contour crossings. "
            "Try drawing a larger domain."
        )
    grid_z, transform = build_dtm(xs, ys, zs, step=DTM_STEP)
    dtm_path = job_dir / "dtm.tif"
    write_geotiff(grid_z, transform, path=dtm_path)
    job.log_line(f"wrote {dtm_path}")

    job.set_stage("conforming_overlay")
    elevation_lookup = ElevationLookup(dtm_path)
    conforming_cm, pool = conforming_overlay(
        sbg_cm, elevation_lookup, domain_polygon,
        spatial_index=spatial_index, log_fn=job.log_line,
    )
    conforming_path = job_dir / "conforming.city.json"
    finalize_and_save(conforming_cm, pool, conforming_path)
    job.log_line(f"wrote {conforming_path} ({len(conforming_cm['CityObjects'])} CityObjects)")

    job.set_stage("obj_export")
    # cjio 0.10.1's OBJ exporter silently drops all geometry for
    # CompositeSolid CityObjects (confirmed by reading cjio/cityjson.py's
    # export2obj() directly: it only handles MultiSurface/CompositeSurface/
    # Solid, so a CompositeSolid object's `o <id>` marker gets written with
    # zero faces after it -- no error, no warning). CompositeSolid is
    # exactly what extrude.py produces for any MultiPolygon footprint (an
    # OSM relation with disjoint parts, e.g. twin towers under one building)
    # -- real, not rare: this is what caused the reported "holes" in STL
    # output (buildings that render fine in the plain CityJSON 3D viewer,
    # which never goes through cjio's OBJ path). Worked around by exporting
    # a throwaway split copy instead of patching cjio itself -- see
    # split_composite_solids()'s docstring for why splitting is safe here
    # (Blender's later join step fuses every object regardless of identity).
    obj_export_cm = split_composite_solids(conforming_cm)
    obj_export_path = job_dir / "conforming_for_obj.city.json"
    with open(obj_export_path, "w") as f:
        json.dump(obj_export_cm, f)

    obj_path = job_dir / "conforming.obj"
    cjio_bin = Path(sys.executable).parent / "cjio"
    _run_subprocess(job, [str(cjio_bin), str(obj_export_path), "export", "obj", str(obj_path)])

    # export_stl.py no longer decimates (moved to fast_simplification below,
    # ~9-10x faster than Blender's COLLAPSE on this scale -- see repair_stl.py's
    # module docstring for the measured numbers) -- raw.stl here is the
    # full-resolution, debris-dropped mesh straight out of voxel remesh.
    job.set_stage("blender_export")
    raw_stl_path = job_dir / "raw.stl"
    _run_subprocess(job, [
        str(BLENDER_PATH), "--background", "--python", str(EXPORT_STL_SCRIPT), "--",
        "--input", str(obj_path), "--output", str(raw_stl_path),
    ])

    job.set_stage("decimate_and_repair")
    from sbg.blender.repair_stl import verify_and_repair
    final_stl_path = job_dir / "final.stl"
    mesh = verify_and_repair(str(raw_stl_path), str(final_stl_path), log_fn=job.log_line)
    job.log_line(f"final: watertight={mesh.is_watertight}, volume={mesh.volume:.1f} m^3")

    job.set_stage("done")
    return {
        # Absolute, not relative-to-DATA_DIR.parent -- a real test run
        # confirmed relative_to() raises if the STL ever ends up outside that
        # tree for any reason (it doesn't in normal use, JOBS_ROOT is fixed,
        # but there's no reason to have a job fail at its very last line
        # over a path-formatting choice). download_stl() reads this directly.
        "stl_path": str(final_stl_path),
        "watertight": bool(mesh.is_watertight),
        "volume_m3": float(mesh.volume),
        "faces": len(mesh.faces),
        "vertices": len(mesh.vertices),
        "domain": {"ring": ring, "bbox": [xmin, ymin, xmax, ymax], "area_m2": domain_polygon.area},
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
