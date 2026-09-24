# Stage 1: scraping the `.b3dm` archive — tileset format + real LOD examples

Everything in this folder was fetched live from SLA's public OneMap 3D-Tiles API
(`https://www.onemap.gov.sg/omapi/tilesets/sg_noterrain_tiles/tileset.json`) this
session, decoded with this repo's own code, and exported to plain STL so the geometry
can be opened in any 3D viewer (Blender, MeshLab, an online STL viewer) without needing
the pipeline installed. `manifest.json` in this folder has the exact numbers below in
machine-readable form.

Code used (all already in the repo, none written new): `sbg/onemap/client.py`
(`load_tileset`, headers, `search_buildings`), `sbg/onemap/b3dm.py`
(`extract_batch_table`), `sbg/onemap_native/tiles.py` (`feature_table`, `fetch_tile`,
`load_gltf`), `sbg/onemap_native/transform.py` (`local_to_svy21`, `node_transform`),
`DracoPy.decode`.

## The tileset format (3D Tiles 1.0)

`tileset.json` is a tree. Every node has a `boundingVolume` (here always a `region`:
`[west, south, east, north, minHeight, maxHeight]` in **radians** for the lon/lat
corners, metres for height), a `geometricError` (a "how wrong is it to render this node
instead of refining further" metric — lower is finer), and either `children`, a
`content` reference (a tile file — a `.b3dm` or a nested `tileset.json`), or both.

**The real root node**, fetched this session:

```json
{
  "boundingVolume": {"region": [1.8083, 0.0202, 1.8161, 0.0257, 0, 288.56]},
  "geometricError": 1415.28,
  "refine": "ADD",
  "children": "<4 children, no direct content>"
}
```

`refine: "ADD"` at the root means: don't replace the parent's content with the
children's — **add** the children's content alongside whatever the parent already drew.
The root itself has no `content` at all (`region` covers the whole island, height 0–289m
— the tallest building in the whole `sg_buildings_v5` dataset). Each of its 4 children is
a quadtree quadrant, itself with no `content` yet, itself splitting into 4 more — this
repeats for several levels before any node actually has a b3dm to fetch. This is how
`sbg/onemap_native/tiles.py::domain_leaf_tiles()` prunes: it only descends into children
whose `region` intersects the requested domain, so most of the tree is never even
touched for a small cutout.

**The first node with real content**, walking down toward the National Stadium /
Kallang area (`1.3008°N, 103.8744°E`), 4 quadtree levels down:

```json
{
  "boundingVolume": {"region": [1.81273, 0.02267, 1.81333, 0.02297, 0, 176.25]},
  "geometricError": 367.7,
  "refine": "REPLACE",
  "content": {
    "uri": "3/5/1_4.b3dm",
    "boundingVolume": {"region": [1.81292, 0.02274, 1.81297, 0.02282, 0, 84.20]}
  },
  "children": "<1 child>"
}
```

Two bounding volumes matter here and they're different: the **node's** `boundingVolume`
is the culling box (matches its parent — hasn't shrunk yet); the **content's own**
`boundingVolume` is the real, tight extent of what's actually in `1_4.b3dm` (a ~50m×80m
box — one building). `refine: "REPLACE"` from here down means: once a viewer decides to
descend into this node's child, throw away this node's content and use the child's
instead (unlike the root's `ADD`).

**The real REPLACE chain**, all 5 rungs, single node per level (`3/5/1_4.b3dm` down to
`3/5/1_0.b3dm`):

| depth | file | `geometricError` | children |
|---|---|---|---|
| 4 | `3/5/1_4.b3dm` | 367.7 (coarsest) | 1 |
| 5 | `3/5/1_3.b3dm` | 8.0 | 1 |
| 6 | `3/5/1_2.b3dm` | 4.0 | 1 |
| 7 | `3/5/1_1.b3dm` | 2.0 | 1 |
| 8 | `3/5/1_0.b3dm` | 0.0 (finest — leaf, no children) | 0 |

A viewer walks this chain top-down comparing each node's screen-space error (derived
from `geometricError` and camera distance) against a threshold: far away, it stops at a
coarse rung and renders that content; close up, it keeps descending to `1_0.b3dm`.

## What actually changes between LOD rungs — the real finding

The natural assumption is "finer LOD = simpler-to-more-detailed **mesh**." Measured
directly (below), that's **not what's happening here** — at least not for these two real
chains. What scales dramatically between rungs is the **embedded diffuse texture image**,
not the geometry.

### Chain A — single building (National Stadium), `3/5/1_4.b3dm` → `3/5/1_0.b3dm`

| rung | `geometricError` | `.b3dm` size | Draco geometry bytes | embedded texture bytes | STL faces |
|---|---|---|---|---|---|
| coarsest | 367.7 | 68,880 B | **65,690 B** | 317 B (a tiny placeholder PNG) | 5,976 |
| finest | 0.0 | 9,476,792 B | **65,690 B** | 9,408,235 B (a TIFF) | 5,976 |

The Draco-compressed mesh payload is **byte-for-byte identical** at both ends of the
chain (confirmed at the `bufferView` level, not just the decoded face count) — the same
5,976-triangle mesh is reused unchanged at every rung. The 138× growth in file size is
entirely the texture: a 317-byte placeholder at the coarsest rung, a 9.4MB TIFF at the
finest. `stadium_coarse_1_4.stl` and `stadium_fine_1_0.stl` in this folder are that exact
mesh, exported from the two opposite ends of the chain — open them side by side and
they're the same building at the same detail.

### Chain B — a 20-building batch (Kallang Park / Stadium MRT / Kallang Theatre /
Singapore Indoor Stadium), `5/21/4_5.b3dm` → `5/21/4_0.b3dm`

| rung | `geometricError` | `.b3dm` size | Draco geometry bytes | embedded texture bytes | batch-table buildings |
|---|---|---|---|---|---|
| coarsest | 249.0 | 90,464 B | 76,563 B | 7,467 B | 20 |
| finest | 0.0 | 15,905,052 B | 82,501 B | 15,812,306 B | 20 |

Same story, ~176× file-size growth almost entirely from the texture. Geometry grows only
~8% (76,563 → 82,501 Draco bytes, one extra glTF mesh object) despite the huge size
difference, and the exported STL face count is identical (6,872) at both ends — the extra
bytes decode to the same batch-relevant geometry we actually extract; whatever the small
delta is (a minor prop/fixture, not investigated further) it doesn't change the pipeline's
extracted face count. Both `.stl` files are 343,684 bytes — genuinely the same mesh.
`n_buildings_in_batch_table` (20) is also identical at both rungs, for both chains — this
tileset's REPLACE chains don't add or drop *which* buildings are present as you refine;
that's decided by *which spatial node* you're in (a different chain covering a different
area), not by which rung of one chain you're looking at.

### Why this matters for the rest of the pipeline

`sbg/onemap_native/extract.py` only ever reads `POSITION`/`NORMAL`/`_BATCHID` out of the
Draco payload — it never touches `TEXCOORD_0` or the embedded image. Given the finding
above, that means **the coarsest tile in a REPLACE chain already carries essentially all
the geometry the pipeline cares about** for a building like this — the far larger file
size of a "finer" tile is mostly a texture the pipeline discards anyway. This is also why
`sbg/onemap_native/tiles.py::domain_leaf_tiles()` deliberately walks to the actual
**leaf** (finest rung, `refine` chain exhausted) rather than stopping early: it's not
chasing texture detail, it's making sure it has the *complete* building set for the area
(the `refine: "ADD"` structural levels above content, not the `REPLACE` chain within one
node, is what actually changes which buildings exist) — but this session's specific
measurement suggests a coarser rung would very likely have sufficed for geometry alone,
which is worth someone's follow-up if fetch bandwidth ever becomes a real constraint (see
the main plan's tile-native investigation for the actual archive-size numbers: ~123GB
full-resolution vs a fraction of that if geometry could be pulled from coarse rungs).

## Files in this folder

- `stadium_coarse_1_4.stl` / `stadium_fine_1_0.stl` — Chain A, opposite ends, same mesh.
- `kallang_cluster_coarse_4_5.stl` / `kallang_cluster_fine_4_0.stl` — Chain B, opposite
  ends, same mesh, 20 buildings batched into one file (Kallang Park structures + Stadium
  MRT Station + Kallang Theatre + Singapore Indoor Stadium, all sharing one `_BATCHID`
  per building inside the mesh — this is the batching the main pipeline's `extract.py`
  splits apart by `_BATCHID` before placing each building individually).
- `manifest.json` — the exact numbers in the tables above, plus per-file vertex/face
  counts and z-bounds, machine-readable.

All four STLs are in real EPSG:3414 (SVY21) metres, already through the corrected
`local_to_svy21` transform — they'll sit at their true Singapore coordinates if loaded
into a viewer that doesn't recentre.
