"""Lean v2 web UI for the tile-native OneMap -> watertight CFD STL pipeline.

Wraps `sbg.onemap_native.build` behind a friendly local web app: draw a CFD
domain boundary, generate a watertight STL, view + download it. Deliberately
lean -- no CityJSON semantic editing (see the project plan's V2 FRONTEND PLAN):
the dose-relevant per-building semantics OneMap lacks are carried instead as an
optional `building_archetype` sidecar from sg_buildings_v5.geojson.

Run: `python -m sbg.onemap_native.ui`  (opens a browser tab).
"""
