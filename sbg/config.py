from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"

SG_BUILDINGS_GEOJSON = BASE_DIR / "sg_buildings_v5.geojson"
HDB_CITYJSON = BASE_DIR / "hdb.json"
NATIONAL_MAP_LINE_GEOJSON = BASE_DIR / "NationalMapLine.geojson"

SBG_OUTPUT = DATA_DIR / "sbg.city.json"

SOURCE_CRS = "EPSG:4326"
TARGET_CRS = "EPSG:3414"
# CityJSON 1.1 metadata.referenceSystem must use the OGC URL form, not the
# "urn:ogc:def:crs:..." form hdb.json uses (cjio chokes on the latter).
CITYJSON_REFERENCE_SYSTEM = "https://www.opengis.net/def/crs/EPSG/0/3414"

CITYJSON_VERSION = "1.1"
TRANSFORM_SCALE = 0.001  # 1mm quantization, far finer than source data needs

# Zero/missing-height policy (see plan doc): coalesce osm height -> levels*LEVEL_HEIGHT -> DEFAULT_HEIGHT
LEVEL_HEIGHT = 3.2
DEFAULT_HEIGHT = 3.2

# Standalone Blender install used by sbg/ui/pipeline.py's STL job (see Phase 5
# of the project plan) -- not `pip install bpy` (PyPI wheel pinned to a
# Python version that clashes with this venv), not on PATH, invoked by full
# path. Not user-configurable yet (see plan's own "settings screen with a
# test button" follow-up) -- change this constant if Blender is reinstalled
# elsewhere.
BLENDER_PATH = Path.home() / "tools" / "blender-4.5.11-linux-x64" / "blender"
