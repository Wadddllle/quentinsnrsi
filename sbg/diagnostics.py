"""Diagnostic: visualize SBG buildings colored by height_source, straight from
the built CityJSON (the single source of truth after any backfill passes).

Usage: .venv/bin/python -m sbg.diagnostics
"""
import json
import sys
from collections import Counter

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from sbg.config import DATA_DIR, SBG_OUTPUT
from sbg.io_cityjson import building_footprint_centroid

COLORS = {
    "osm": "#2b6cb0",
    "levels": "#dd6b20",
    "onemap": "#2f855a",
    "estimated_default": "#c53030",
}


def main():
    cm = json.load(open(SBG_OUTPUT))

    points = {}
    archetype_counts = Counter()
    total = 0

    for obj_id, obj in cm["CityObjects"].items():
        total += 1
        attrs = obj["attributes"]
        height_source = attrs.get("height_source", "unknown")
        centroid = building_footprint_centroid(cm, obj)
        if centroid is None:
            continue
        points.setdefault(height_source, ([], []))
        x, y = centroid
        points[height_source][0].append(x)
        points[height_source][1].append(y)
        if height_source == "estimated_default":
            archetype_counts[attrs.get("building_archetype")] += 1

    print(f"Total buildings: {total}", file=sys.stderr)
    for key, (xs, _ys) in sorted(points.items(), key=lambda kv: -len(kv[1][0])):
        print(f"  {key}: {len(xs)}", file=sys.stderr)
    print("Top archetypes among estimated_default:", file=sys.stderr)
    for archetype, count in archetype_counts.most_common(10):
        print(f"  {archetype}: {count}", file=sys.stderr)

    fig, ax = plt.subplots(figsize=(10, 14))
    # plot largest-count group first (background), smallest last (on top / most visible)
    for key in sorted(points, key=lambda k: -len(points[k][0])):
        xs, ys = points[key]
        color = COLORS.get(key, "#666666")
        ax.scatter(xs, ys, s=1, color=color, label=f"{key} ({len(xs)})", alpha=0.5)
    ax.set_aspect("equal")
    ax.set_title("SBG building height provenance (EPSG:3414)")
    ax.legend(markerscale=10, loc="upper right")
    ax.set_xlabel("Easting (m)")
    ax.set_ylabel("Northing (m)")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / "height_source_diagnostic.png"
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    print(f"Wrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
