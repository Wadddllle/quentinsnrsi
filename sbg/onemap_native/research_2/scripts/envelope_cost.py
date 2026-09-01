"""De-risk the 'build once' envelope: does a buffered domain actually fit in memory?

The wind feature builds ONE domain covering every direction, then rotates+clips per
direction. That envelope is much larger than any domain built here before (Kent Ridge
8-direction @ H_CAP=60 is 8.78 km^2, vs the CBD's 4 km^2 at ~220 s). If it OOMs, the
whole 'build once' premise fails and the fallback is per-direction builds.

Also exercises the core_polygon / domain_polygon split for real: buildings come from the
ROI, terrain and clip from the envelope.

    /usr/bin/time -v .venv/bin/python -m sbg.onemap_native.research_2.scripts.envelope_cost
"""
import sys, os, time
sys.path.insert(0, "/home/quentin/snrsi"); os.chdir("/home/quentin/snrsi")
import warnings; warnings.filterwarnings("ignore")

from shapely.geometry import box

from sbg.onemap_native.build import build_domain_stl
from sbg.onemap_native.wind import build_envelope, buffer_distances, roi_centre, rose

OUT = ("/tmp/claude-1001/-home-quentin-snrsi/e94fd686-4895-49c7-be10-7af85f471583"
       "/scratchpad/fuse_ab")

if __name__ == "__main__":
    n = int(os.environ.get("NDIRS", "8"))
    h = float(os.environ.get("H", "100.2"))

    roi = box(21950, 30250, 22850, 31150)          # Kent Ridge, 900 m
    c = roi_centre(roi)
    up, down, lat, warns = buffer_distances(h)
    env = build_envelope(roi, c, rose(n), up, down, lat)
    for w in warns:
        print(f"  warning: {w}")
    print(f"ROI {roi.area/1e6:.2f} km^2 -> envelope {env.area/1e6:.2f} km^2 "
          f"({env.area/roi.area:.1f}x), buffer {up:.0f}/{down:.0f}/{lat:.0f} m, {n} directions")

    t0 = time.time()
    build_domain_stl(env, f"{OUT}/envelope_{n}dir.stl",
                     core_polygon=roi, store_dir="data/onemap_store")
    print(f"\nTOTAL {time.time() - t0:.1f}s")
