# Experiment scripts for the watertight+fidelity research (see ../PROBLEM_BRIEF.md)

Rescued from a session scratchpad (`/tmp`, session-scoped — these were nearly lost once).
Not production code, not imported by anything in `sbg/`. Run from the repo root.

- `slice_cdt2.py`  — **the main one.** Per-building Dual-MC remesh → slice at pad+1m →
  the shell's own boundary loop as a CDT constraint → conforming terrain with hole seeds.
  `SHRINK` env var (default 0.05, the measured optimum). ~38s on Duxton 400m.
  Reproduced 2026-08-19: openEdges=869, nonManifoldEdges=0, selfX=1966, 32 components.
- `dmc_check.py`   — per-building raw Dual-MC output quality (holes/open/non-manifold/self-X).
  This is what showed sealing makes DMC essentially clean (2 NM edges over 90 buildings).
- `measure.py`     — defect stats for any STL: holes, components, self-X, and a strict
  0.1mm re-weld edge histogram (open + non-manifold). `python measure.py a.stl b.stl`
- `where_selfx2.py`— locates self-intersections by elevation band.
- `mc_vs_dmc.py`   — standard Marching Cubes vs Dual MC comparison (DMC wins 32x).
- others           — supporting probes from the same investigation.
