"""GET /api/onemap/search -- location search box backing (see project plan,
Phase 6: no basemap tiles in v1, but precise navigation to a named place is
still needed). Thin wrapper around the existing sbg.onemap.client function;
coordinate-entry (the guaranteed-precise fallback) is handled client-side,
no backend endpoint needed for it.
"""
from fastapi import APIRouter, HTTPException

from sbg.onemap.client import search_buildings

router = APIRouter(prefix="/api/onemap", tags=["onemap"])


@router.get("/search")
def search(q: str, max_results: int = 10):
    if not q or not q.strip():
        raise HTTPException(400, "q must not be empty")
    return {"results": search_buildings(q, max_results=max_results)}
