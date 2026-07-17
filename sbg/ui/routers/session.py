"""GET /api/session/status -- the current editing session's unsaved-change
count/summary/log, backing App.vue's "N unsaved" badge and Undo button
enable state. See sbg.ui.session.Session and the project plan's Phase 3
write-up.
"""
from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/session", tags=["session"])


@router.get("/status")
def status(request: Request):
    return request.app.state.session.to_status_dict()
