"""Run the tile-native STL pipeline (`sbg.onemap_native.build`) as a background
job, reusing sbg.ui.jobs' in-process async manager. Streams the pipeline's own
stage logs into the job and verifies each result with meshlib (fast, authoritative
-- NOT a trimesh process=True load, which is the misleading check on STL soup).

ONE JOB, N OUTPUTS. A wind-rose run builds the heavy stages once and then emits one
STL + sidecar per direction, so it must stay a single job -- N separate jobs would
rebuild everything N times against a 2-worker pool. `result["outputs"]` is the list;
the single-output fields stay at the top level pointing at the first entry so the
existing download route and result box keep working.
"""
import re
import time
from pathlib import Path

import meshlib.mrmeshpy as mr

from sbg.onemap_native.build import build_domain_stl

# pipeline log-prefix -> friendly stage label for the progress UI
_STAGES = {
    "[tiles]": "tiles", "[extract]": "extract", "[terrain]": "terrain",
    "[obj]": "write", "[scene]": "write", "[blender]": "fuse", "[fuse]": "fuse",
    "[decimate]": "decimate", "[clip]": "clip", "[polish]": "polish", "[done]": "done",
}
_DIRECTION_RE = re.compile(r"^\[direction\] (\d+)/(\d+) wind from ([\d.]+)")


def _verify(stl_path, extra=None):
    """Load the SHIPPED file back and measure it. Deliberately re-reads from disk
    rather than trusting an in-memory count: closure at float32 STL precision is not
    the same claim as closure in memory, and that gap has shipped a hole before."""
    m = mr.loadMesh(str(stl_path))
    holes = len(m.topology.findHoleRepresentiveEdges())
    multi = bool(mr.hasMultipleEdges(m.topology))
    bb = m.computeBoundingBox()
    try:
        volume = float(m.volume())
    except Exception:
        volume = None
    out = {
        "stl_path": str(stl_path),
        "filename": Path(stl_path).name,
        "watertight": holes == 0 and not multi,
        "holes": holes,
        "non_manifold": multi,
        "faces": int(m.topology.numValidFaces()),
        "volume_m3": volume,
        "bounds": [[bb.min.x, bb.min.y, bb.min.z], [bb.max.x, bb.max.y, bb.max.z]],
        "size_mb": round(Path(stl_path).stat().st_size / 1e6, 1),
    }
    out.update(extra or {})
    return out


def run_stl_job(job, domain_polygon, jobs_root, store_dir=None, log_extra=None,
                core_polygon=None, wind=None, **build_kwargs):
    """Job fn: build the STL(s), then return a JSON-able result summary.

    Output goes to <jobs_root>/<job.id>/ -- derived here so the caller can create the
    job (and get its id) before the path exists. A plain run writes `domain.stl`; a
    wind run writes `wind_<bearing>.stl` + `wind_<bearing>.wind.json` per direction.
    """
    job_dir = Path(jobs_root) / job.id
    job_dir.mkdir(parents=True, exist_ok=True)
    out_stl = job_dir / "domain.stl"

    def sink(line):
        job.log_line(line)
        m = _DIRECTION_RE.match(line)
        if m:
            job.substage = (f"direction {m.group(1)}/{m.group(2)} "
                            f"(wind from {float(m.group(3)):g} deg)")
            return
        for prefix, stage in _STAGES.items():
            if line.startswith(prefix):
                job.stage = stage
                break

    if log_extra:
        job.log_line(log_extra)
    t0 = time.perf_counter()
    res = build_domain_stl(domain_polygon, out_stl, store_dir=store_dir, log=sink,
                           core_polygon=core_polygon, wind=wind, **build_kwargs)

    job.set_stage("verify")
    if isinstance(res, list):        # wind run: one entry per direction
        outputs = [_verify(r["stl"], {
            "wind_from_deg": r["wind_from_deg"],
            "sidecar": Path(r["sidecar"]).name,
        }) for r in res]
    else:
        outputs = [_verify(res)]

    result = dict(outputs[0])        # back-compat: single-output fields at the top
    result.update({
        "outputs": outputs,
        "count": len(outputs),
        "wind": bool(wind),
        "all_watertight": all(o["watertight"] for o in outputs),
        "elapsed_s": round(time.perf_counter() - t0, 1),
    })

    for o in outputs:
        vol = f"{o['volume_m3']:.4e}" if o["volume_m3"] is not None else "n/a"
        job.log_line(f"[verify] {o['filename']}: watertight={o['watertight']} "
                     f"faces={o['faces']:,} vol={vol} ({o['size_mb']}MB)")
    if len(outputs) > 1:
        job.log_line(f"[verify] {sum(o['watertight'] for o in outputs)}/{len(outputs)} "
                     f"direction(s) strictly watertight")
    return result
