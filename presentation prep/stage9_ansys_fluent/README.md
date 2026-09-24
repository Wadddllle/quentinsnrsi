# Stage 9: Ansys Fluent — CFD + particle tracking

This stage is deliberately brief — the setup itself isn't the point here, what it
produces is. Full byte-level detail on the *output* format lives in
[`dose/PARTICLES_XML_FORMAT.md`](../../dose/PARTICLES_XML_FORMAT.md); this page is the
short version plus what the specific run used.

## What Fluent actually does here

Two stages, run inside the same case:

1. **RANS flow field.** The watertight STL from Stage 8 becomes Fluent's flow domain.
   Fluent solves the steady Reynolds-Averaged Navier–Stokes equations (mass + momentum,
   closed with a turbulence model) to get one converged 3D velocity/pressure/turbulence
   field for the whole domain — wind blowing in one inlet face, out the opposite outlet
   face, no-slip on every building/terrain surface.
2. **Discrete Phase Model (DPM) particle tracking.** Once the flow field is converged,
   Fluent releases a swarm of massless Lagrangian tracer particles from a release point
   and advects each one through that frozen flow field, step by step, adding a random
   turbulent velocity kick at each step (Discrete Random Walk) so tracers actually spread
   out instead of all following one deterministic streamline. Each tracer is followed
   until it exits the domain through a boundary (or, depending on the boundary condition
   assigned to a surface, gets deleted/trapped there). Fluent writes out every point along
   every tracer's path — that trajectory log is the file this whole `dose/` folder starts
   from.

## The actual config used (Ansys Fluent Simulation Report 3)

Real parameters from the run this project settled on, not a generic setup guide:

| parameter | value |
|---|---|
| solver | 3D pressure-based, steady |
| turbulence model | SST k-ω |
| turbulence inlet spec | Intensity 20%, length scale 15m (derived from surface roughness, not left at Fluent's generic-internal-flow default — see `dose/NOTES.md`) |
| fluid | air, ρ = 1.225 kg/m³ |
| inlet | 5 m/s velocity inlet |
| outlet | pressure outlet |
| walls (buildings + terrain) | no-slip; **DPM boundary condition = reflect** (tracers bounce off, none deposit — deposition is handled later, in post-processing, not in Fluent) |
| domain sides | symmetry |
| injection | single point source, released with the Discrete Random Walk model enabled, DPM boundary condition = **escape** |
| mesh | 212,230 cells / 949,600 faces / 576,937 nodes |
| iterations | 333, continuity residual converged to 9.8×10⁻⁴ |

The two DPM boundary conditions (reflect on walls, escape at the release surface) matter
more than anything else in the table — an earlier run had them the wrong way round
(inlet set to trap, silently deleting 13% of the release at t=0; walls left on reflect,
which is actually correct for cloudshine but was assumed to be a bug at the time). Getting
these right is the difference between a physically meaningful particle set and a silently
biased one.

## The output: `particles3.xml`

Fluent/CFD-Post exports every tracked particle's trajectory as one XML file, roughly
93MB for 20,000 tracers / ~3.6 million logged points. It's a **column store**, not a
per-particle nested structure — every column (particle ID, time, X/Y/Z position) is
written as its own flat array, chunked into `<Section>` blocks of rows, and a track has
to be reassembled afterward by grouping rows on the particle ID column. `dose/PARTICLES_XML_FORMAT.md`
has the full schema, encoding (`ASCII` or base64-packed little-endian `float32`), and the
real numbers measured off this exact file. The one property that matters most downstream:
**consecutive points on a track are not evenly spaced in time** — Fluent writes roughly
one point per mesh-cell crossing, so the time gap between two consecutive points *is*
that particle's residence time in the cell it just crossed. That's the property Stage 10
(OpenMC) builds its entire source term on.

No mass, concentration, or per-particle weight is in this file — every tracer is a pure,
equal-weight kinematic tracer (`Injection = massless`). That equal-weighting is only true
because the release is a single point source; it's the reason Stage 10 can turn a plain
count of track-seconds into a physical concentration field without any correction factor.
