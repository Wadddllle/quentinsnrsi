#!/usr/bin/env python
"""Reader for Ansys CFD-Post / Fluent `<ParticleTracks>` XML export.

Verified against data/openmc_data/particles.xml (92 MB, 342 sections,
3,415,761 track points, 38,600 tracks) -- see PARTICLES_XML_FORMAT.md for the
format writeup and for what the file does and does not contain.

The two things that are easy to get wrong:
  * FLOAT is float32 little-endian, NOT float64. The `Base64/LE` payload length
    divided by the Section's `length` attribute is 4.000 exactly. Decoding as
    '<f8' silently yields half the rows and garbage values.
  * `constant="true"` means the payload holds ONE value for the whole section,
    not `length` values. Broadcast it.

  python dose/read_particle_tracks.py data/openmc_data/particles.xml
"""
import argparse
import base64
import xml.etree.ElementTree as ET

import numpy as np

# XML type name -> numpy dtype for Base64/LE payloads.
_DTYPE = {"FLOAT": "<f4", "DOUBLE": "<f8", "INTEGER32": "<i4", "INTEGER64": "<i8"}


def read_items(path):
    """-> {item_id: {'name','type','units','options'}}. `options` maps the
    integer code stored in the data column to its label (e.g. 28387 ->
    'fluid-region-1'), and is empty for non-OPTION items."""
    items = {}
    for _, el in ET.iterparse(path, events=("end",)):
        if el.tag == "Item":
            items[int(el.get("id"))] = dict(
                name=el.get("name"),
                type=el.get("type"),
                units=el.get("units"),
                options={int(o.get("id")): o.get("data") for o in el.findall("Option")},
            )
        elif el.tag == "Items":
            el.clear()
            break
    return items


def _decode(data_el, length, itype):
    raw = data_el.text.strip()
    if data_el.get("dataFormat") == "Base64/LE":
        a = np.frombuffer(base64.b64decode(raw), _DTYPE[itype])
    else:
        int_like = itype.startswith("INTEGER") or itype == "OPTION"
        a = np.array(raw.split(), dtype=np.int64 if int_like else np.float64)
    if data_el.get("constant") == "true":
        return np.repeat(a[:1], length)
    if len(a) != length:
        raise ValueError(f"item {data_el.get('item')}: {len(a)} values, expected {length}")
    return a


def read_tracks(path, items=None):
    """-> dict of concatenated per-point columns, keyed by item id.

    Rows are ordered as written: grouped by Particle ID, and within a track by
    increasing Particle Time. Sections are a chunking artefact of the writer
    (10,000 rows each) and carry no meaning -- a single track routinely spans a
    section boundary, so never treat a section as a track.
    """
    items = items or read_items(path)
    cols = {i: [] for i in items}
    for _, el in ET.iterparse(path, events=("end",)):
        if el.tag != "Section":
            continue
        n = int(el.get("length"))
        for d in el.findall("Data"):
            i = int(d.get("item"))
            cols[i].append(_decode(d, n, items[i]["type"]))
        el.clear()
    return {i: np.concatenate(v) for i, v in cols.items() if v}


def by_name(items, cols):
    """-> {item name: column}, the form you actually want downstream."""
    return {items[i]["name"]: c for i, c in cols.items()}


def residence_time(pid, t):
    """Per-step residence time, aligned to the LEAVING point of each step.

    Fluent writes roughly one point per control-volume crossing, so the time
    between consecutive points of one track is the time that particle spent in
    the cell it just traversed. Returns (mask, dt) where mask selects the rows
    i for which row i+1 belongs to the same track.
    """
    mask = np.diff(pid) == 0
    return mask, np.diff(t)[mask]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    a = ap.parse_args()

    items = read_items(a.path)
    for i, m in sorted(items.items()):
        u = f" [{m['units']}]" if m["units"] else ""
        o = f"  options={len(m['options'])}" if m["options"] else ""
        print(f"  item {i}: {m['name']}{u} ({m['type']}){o}")

    cols = read_tracks(a.path, items)
    c = by_name(items, cols)
    pid = c["Particle ID"].astype(np.int64)
    t = c["Particle Time"].astype(np.float64)
    xyz = np.column_stack([c[f"Particle {k} Position"] for k in "XYZ"]).astype(np.float64)

    uid, cnt = np.unique(pid, return_counts=True)
    mask, dt = residence_time(pid, t)
    print(f"\n{len(pid):,} points, {len(uid):,} tracks")
    print(f"points/track  min {cnt.min()} p50 {int(np.median(cnt))} max {cnt.max()}")
    print("bbox " + "  ".join(f"{k} {lo:.1f}..{hi:.1f}" for k, lo, hi
                              in zip("XYZ", xyz.min(0), xyz.max(0))))
    print(f"time 0..{t.max():.1f} s   total residence {dt.sum():.4g} particle-s")


if __name__ == "__main__":
    main()
