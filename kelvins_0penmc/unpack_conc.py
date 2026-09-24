"""Unpack conc.npz -> conc.npy.

Array is (z, y, x), units Bq/m^3, 10x10x10 m grid cells.
"""
import numpy as np
from pathlib import Path

HERE = Path(__file__).parent

def main():
    d = np.load(HERE / "conc.npz")
    a = d["conc"]
    print(f"shape (z,y,x) = {a.shape}, dtype={a.dtype}")
    np.save(HERE / "conc.npy", a)
    print(f"wrote {HERE / 'conc.npy'}")

if __name__ == "__main__":
    main()
