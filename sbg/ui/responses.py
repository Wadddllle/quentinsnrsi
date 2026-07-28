"""Shared JSON-response helper for sbg/ui's routers.

fastapi.responses.ORJSONResponse is deprecated as of the FastAPI version
pinned here -- its own recommended replacement is a Pydantic response_model,
which serializes via pydantic-core instead of jsonable_encoder. Not adopted:
these endpoints return plain dicts shaped like arbitrary CityJSON (dynamic
CityObjects keys, several incompatible geometry-type shapes) -- modeling
that in Pydantic is a real schema-design task, not a drop-in swap, and risks
quietly reintroducing the exact jsonable_encoder-style overhead this project
already measured and eliminated (see dataset.py: 28s/42MB -> 0.76s for
/footprints, ~13s -> cached for /buildings). orjson_response() keeps the
same orjson.dumps() call the deprecated class made internally, just via a
plain Response instead of a now-deprecated subclass -- zero behavior
change, warning gone.
"""
import orjson
from fastapi import Response


def orjson_response(data):
    body = orjson.dumps(data, option=orjson.OPT_NON_STR_KEYS | orjson.OPT_SERIALIZE_NUMPY)
    return Response(content=body, media_type="application/json")
