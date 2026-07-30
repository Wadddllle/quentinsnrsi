"""Run the tile-native STL pipeline (`sbg.onemap_native.build`) as a background
job, reusing sbg.ui.jobs' in-process async manager. Streams the pipeline's own
stage logs into the job and verifies the result with meshlib (fast, authoritative
-- NOT a trimesh process=True load, which is the misleading check on STL soup)."""
import time
from pathlib import Path

import meshlib.mrmeshpy as mr

from sbg.onemap_native.build import build_domain_stl

# pipeline log-prefix -> friendly stage label for the progress UI
_STAGES = {
    "[tiles]": "tiles", "[extract]": "extract", "[terrain]": "terrain",
    "[obj]": "write", "[scene]": "write", "[blender]": "fuse",
    "[decimate]": "decimate", "[clip]": "clip", "[polish]": "polish", "[done]": "done",
}


def run_stl_job(job, domain_polygon, jobs_root, store_dir=None, log_extra=None, **build_kwargs):
    """Job fn: build the STL, then return a JSON-able result summary. Progress is
    surfaced by mapping the pipeline's own '[stage] ...' log lines onto job.stage.
    Output goes to <jobs_root>/<job.id>/domain.stl (derived here so the caller can
    create the job -- and get its id -- before the path exists)."""
    out_stl = Path(jobs_root) / job.id / "domain.stl"
    out_stl.parent.mkdir(parents=True, exist_ok=True)

    def sink(line):
        job.log_line(line)
        for prefix, stage in _STAGES.items():
            if line.startswith(prefix):
                job.stage = stage
                break

    if log_extra:
        job.log_line(log_extra)
    t0 = time.time()
    build_domain_stl(domain_polygon, out_stl, store_dir=store_dir, log=sink, **build_kwargs)

    # Authoritative verification via meshlib (holes + non-manifold edges).
    job.set_stage("verify")
    m = mr.loadMesh(str(out_stl))
    holes = len(m.topology.findHoleRepresentiveEdges())
    multi = bool(mr.hasMultipleEdges(m.topology))
    bb = m.computeBoundingBox()
    try:
        volume = float(m.volume())
    except Exception:
        volume = None
    result = {
        "stl_path": str(out_stl),
        "filename": out_stl.name,
        "watertight": holes == 0 and not multi,
        "holes": holes,
        "non_manifold": multi,
        "faces": int(m.topology.numValidFaces()),
        "volume_m3": volume,
        "bounds": [[bb.min.x, bb.min.y, bb.min.z], [bb.max.x, bb.max.y, bb.max.z]],
        "size_mb": round(out_stl.stat().st_size / 1e6, 1),
        "elapsed_s": round(time.time() - t0, 1),
    }
    vol_str = f"{volume:.4e}" if volume is not None else "n/a"
    job.log_line(f"[verify] watertight={result['watertight']} faces={result['faces']:,} "
                 f"vol={vol_str} ({result['size_mb']}MB)")
    return result
