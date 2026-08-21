# routers/workspace.py
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from core.sse import (
    event_generator,
    push_workspace_update,
    workspace_broadcast_queue,
    subscriber_count,
)

router = APIRouter(prefix="/api/workspace", tags=["Workspace Stream"])


@router.get("/stream")
async def stream_workspace_events():
    """
    The main, persistent single-socket channel for real-time dashboard interaction.
    """
    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/connections")
async def active_connections():
    """Diagnostic: how many SSE streams are open. Should be 1 per open dashboard tab."""
    return {"active_connections": subscriber_count()}


__all__ = ["router", "push_workspace_update", "workspace_broadcast_queue"]