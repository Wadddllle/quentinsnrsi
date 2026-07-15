"""Decimation, verification, and (only if actually needed) repair pass for
export_stl.py's output. Plain project .venv Python (fast_simplification /
pymeshfix / trimesh), not Blender -- run after export_stl.py, not inside it.

Decimation moved here from export_stl.py's Blender-side DECIMATE/COLLAPSE
modifier -- measured directly on a real ~3.46M-polygon mesh from this exact
pipeline: COLLAPSE took 142.2-155.2s in Blender vs. 16.5s for
fast_simplification.simplify() here (~9-10x faster), with better volume
preservation (0.0003% delta vs. the 0.001% bar COLLAPSE was already held
to), and pymeshfix repairing its output faster too (14.1s vs 22.9-25.5s for
the actual .repair() call). Blender's COLLAPSE is confirmed genuinely
single-threaded; fast_simplification isn't importable inside Blender's own
bundled Python interpreter (it's a project .venv package), so this had to
move to this process rather than just swap which modifier Blender runs.
fast_simplification needs vertex-welded input to have any real edge
connectivity to collapse -- a raw STL is unwelded "triangle soup" (every
triangle owns private vertices, none shared), confirmed the hard way: the
first attempt skipped welding and produced ~1M single-triangle "bodies" and
a 570%-wrong volume. This is exactly why decimation now happens here, after
merge_vertices() (already a required step for the watertight check below)
rather than as an earlier separate stage -- one weld serves both.

Debris (disconnected boundary slivers from the voxel remesh) is still
dropped inside export_stl.py itself, in Blender -- NOT here. Confirmed on
the full 2,605-building CBD domain (10.5M faces) that doing that cleanup
via trimesh in this .venv gets OOM-killed on this box's 7.4GB RAM (both the
connected-component split and the STL re-export independently pushed past
the memory ceiling), while Blender's own compiled mesh ops handle the exact
same file natively without issue. So this script assumes it's already
looking at a single clean body (after decimating) and just verifies that --
it does not repeat Blender's debris-dropping work.

pymeshfix is only invoked as a fallback if the input turns out NOT already
watertight (confirmed on the small 27-building test that voxel remesh can
leave a tiny residual: 2 bad edges out of ~600,000, a single marching-cubes
boundary artifact; also confirmed decimation itself doesn't reliably produce
watertight output either, from either COLLAPSE or fast_simplification) --
skipped entirely when unnecessary, both because it's extra work and because
pymeshfix's own memory footprint at multi-million-face scale hasn't been
proven safe on this box -- exactly why decimating BEFORE this check matters,
keeping pymeshfix's input small regardless of which decimator produced it.

Usage: .venv/bin/python -m sbg.blender.repair_stl data/cbd_town_test.stl -o data/cbd_town_test_final.stl -r 0.85
"""
import argparse
import shutil
import sys
import time

import trimesh

DEFAULT_TARGET_REDUCTION = 0.85  # fraction of faces to REMOVE -- matches the old Blender COLLAPSE default of ratio=0.15 (fraction to KEEP)


def verify_and_repair(input_path, output_path=None, log_fn=None, target_reduction=DEFAULT_TARGET_REDUCTION):
    """log_fn: optional callable(str) for progress lines, e.g. a Job's
    log_line -- defaults to print(..., file=sys.stderr) for CLI use. Needed
    because this function runs in-process from sbg/ui/pipeline.py (not a
    subprocess), so its own print() calls never reached the job's log --
    only the server's own stderr -- making it impossible to tell from a
    job's log alone whether the cheap direct-pass-through branch or the
    expensive pymeshfix-repair branch actually ran.

    target_reduction: fraction of faces to remove via fast_simplification
    before verifying/repairing, or None/0 to skip decimation entirely (e.g.
    when verifying an already-decimated file). See module docstring for why
    decimation lives here now instead of in export_stl.py's Blender step.

    Explicit per-step timing plus explicit logging of WHICH branch ran --
    added because a third party's advice about this stage being slow
    attributed it to trimesh's watertight check being "pure Python and
    slow," without knowing whether the much heavier pymeshfix.MeshFix()
    repair() call (only reached if the mesh ISN'T already watertight) was
    what actually ran. Measured directly on a real 1M+-face job: the
    is_watertight check itself was ~2s; pymeshfix init+repair was ~47s
    (76% of the stage) once triggered -- confirming the advice's specific
    attribution was wrong, but its instinct that repair (not the check)
    is the real cost was right.
    """
    if log_fn is None:
        log_fn = lambda msg: print(msg, file=sys.stderr)  # noqa: E731

    t0 = time.perf_counter()

    def _lap(label):
        nonlocal t0
        now = time.perf_counter()
        log_fn(f"  [{label}] {now - t0:.1f}s")
        t0 = now

    m = trimesh.load(input_path, process=False)
    _lap("trimesh.load")

    # Required before decimating (see module docstring: raw STL is unwelded
    # triangle soup, no edge connectivity for fast_simplification to collapse)
    # and before the watertight/body_count checks below either way -- one
    # weld serves both, done once, in the right order.
    m.merge_vertices(digits_vertex=4)
    _lap("merge_vertices")

    if target_reduction:
        import fast_simplification

        pre_faces = len(m.faces)
        points_out, faces_out = fast_simplification.simplify(m.vertices, m.faces, target_reduction=target_reduction)
        m = trimesh.Trimesh(vertices=points_out, faces=faces_out, process=False)
        log_fn(f"Decimated: {pre_faces} -> {len(m.faces)} faces (target_reduction={target_reduction})")
        _lap("fast_simplification.simplify")

    log_fn(f"Loaded: {len(m.vertices)} vertices, {len(m.faces)} faces, {m.body_count} bodies")
    _lap("body_count (connected-component check)")

    if m.body_count != 1:
        raise RuntimeError(
            f"Expected a single clean body (export_stl.py drops debris itself, in Blender), got "
            f"{m.body_count} bodies. Debris-dropping didn't run or something else broke -- investigate "
            "export_stl.py's output before proceeding, this script deliberately does not guess which "
            "pieces are safe to discard."
        )

    is_watertight = m.is_watertight  # trimesh computes this lazily on first access -- time the access itself
    _lap("is_watertight check")

    if is_watertight:
        log_fn(f"  watertight: True, volume: {m.volume:.1f} m^3, bounds: {m.bounds.tolist()}")
        log_fn("  BRANCH: already watertight -- pymeshfix repair NOT invoked")
        if output_path and output_path != input_path:
            shutil.copyfile(input_path, output_path)
            log_fn(f"Copied {input_path} -> {output_path} (already clean, no repair needed)")
        return m

    log_fn("  watertight: False -- attempting pymeshfix repair...")
    log_fn("  BRANCH: pymeshfix.MeshFix().repair() IS being invoked -- this is the expensive fallback path")
    import pymeshfix

    fixer = pymeshfix.MeshFix(m.vertices, m.faces)
    _lap("pymeshfix.MeshFix() construction")

    fixer.repair(remove_smallest_components=False)
    _lap("pymeshfix .repair() -- the actual repair algorithm, expect this to dominate if it ran")

    fixed = trimesh.Trimesh(vertices=fixer.points, faces=fixer.faces)

    log_fn(f"Repaired: {len(fixed.vertices)} vertices, {len(fixed.faces)} faces")
    log_fn(f"  watertight: {fixed.is_watertight}, volume: {fixed.volume:.1f} m^3")
    _lap("re-check watertight on repaired mesh")
    if not fixed.is_watertight:
        raise RuntimeError("Still not watertight after pymeshfix repair -- needs manual investigation")

    if output_path:
        fixed.export(output_path)
        log_fn(f"Wrote {output_path}")
        _lap("export")
    return fixed


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", help="STL from export_stl.py")
    ap.add_argument("-o", "--output", help="Output path (default: verify only, no write)")
    ap.add_argument(
        "-r", "--target-reduction", type=float, default=DEFAULT_TARGET_REDUCTION,
        help=f"Fraction of faces to remove via fast_simplification before verifying (default: {DEFAULT_TARGET_REDUCTION}). "
             "Use 0 to skip decimation (e.g. verifying an already-decimated file).",
    )
    args = ap.parse_args()
    verify_and_repair(args.input, args.output, target_reduction=args.target_reduction)


if __name__ == "__main__":
    main()
