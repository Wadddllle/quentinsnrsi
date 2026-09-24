"""Interactive 3D view of conc.npy via PyVista -> standalone HTML (vtk.js).

Builds a uniform grid (10x10x10 m cells), thresholds to nonzero, colors by
log10(Bq/m^3), and exports an interactive HTML you can open in any browser
(orbit/zoom/pan, no server needed).

The plume is only 2 cells wide in x and 2 cells thick in z but ~296 cells
long in y, so at true scale it renders as a hairline. XZ_EXAGGERATION
stretches x and z for legibility only -- real dimensions are shown in the
title and tooltips (values, not axis scale, are untouched).
"""
import numpy as np
import pyvista as pv
from pathlib import Path

HERE = Path(__file__).parent
CELL_M = 10.0
XZ_EXAGGERATION = 15  # visual-only stretch of x and z so the 2x2 cross-section reads as a box, not a line


def main():
    a = np.load(HERE / "conc.npy")  # (z, y, x)
    nz, ny, nx = a.shape

    grid = pv.ImageData()
    grid.dimensions = (nx + 1, ny + 1, nz + 1)  # point dims = cell dims + 1
    grid.spacing = (CELL_M, CELL_M, CELL_M)
    grid.origin = (0.0, 0.0, 0.0)

    # array is (z,y,x); VTK wants Fortran order x-fastest for cell_data
    conc_xyz = np.transpose(a, (2, 1, 0))  # -> (x,y,z)
    grid.cell_data["conc_Bq_m3"] = conc_xyz.flatten(order="F")

    nonzero = grid.threshold(1e-30, scalars="conc_Bq_m3")
    nonzero.cell_data["log10_Bq_m3"] = np.log10(nonzero.cell_data["conc_Bq_m3"]).astype(np.float32)

    # stretch x/z only, keep y (the real long axis) untouched
    nonzero.points[:, 0] *= XZ_EXAGGERATION
    nonzero.points[:, 2] *= XZ_EXAGGERATION

    pl = pv.Plotter(off_screen=True, window_size=(1200, 800))
    pl.add_mesh(nonzero, scalars="log10_Bq_m3", cmap="inferno",
                show_edges=True, edge_color="gray",
                scalar_bar_args={"title": "log10(Bq/m^3)"})

    pl.add_text(
        f"Plume concentration, nonzero cells only\n"
        f"x and z exaggerated {XZ_EXAGGERATION}x for visibility (real cell = 10x10x10 m); y is true scale",
        font_size=10, position="upper_left",
    )
    pl.show_bounds(
        grid=False, location="outer", ticks="outside",
        xtitle="x (exaggerated)", ytitle="y (m, true scale)", ztitle="z (exaggerated)",
        n_xlabels=2, n_ylabels=6, n_zlabels=2, font_size=10,
    )
    pl.camera_position = "iso"

    out = HERE / "conc_pyvista.html"
    pl.export_html(str(out))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
