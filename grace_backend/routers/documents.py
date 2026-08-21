# routers/documents.py
"""Document editor endpoints: keep the backend's working copy in sync with what
the operator types in the editor widget, and serve generated files for download.
"""
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from services import documents
from routers.upload import get_active_doc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/doc", tags=["Documents"])


class SyncIn(BaseModel):
    text: str = ""


class ExportIn(BaseModel):
    text: str = ""
    fmt: str = "txt"
    name: str = "document"


@router.post("/sync")
def sync(payload: SyncIn):
    """Mirror the editor's current text into the active document so Grace edits
    the latest version even after the operator types by hand."""
    doc = get_active_doc()
    if not doc:
        return {"ok": False, "error": "No active document."}
    doc["text"] = payload.text or ""
    return {"ok": True}


@router.post("/export")
def export(payload: ExportIn):
    """Render the editor's current text to a file and stream it straight back for
    the in-widget Download button (no registry round-trip)."""
    built = documents.build_document(payload.text or "", payload.fmt or "txt", payload.name or "document")
    item = documents.get_download(built["id"])
    return Response(
        content=item["data"],
        media_type=item["mime"],
        headers={"Content-Disposition": f'attachment; filename="{item["filename"]}"'},
    )


@router.get("/download/{did}")
def download(did: str):
    item = documents.get_download(did)
    if not item:
        raise HTTPException(status_code=404, detail="That document has expired or wasn't found.")
    return Response(
        content=item["data"],
        media_type=item["mime"],
        headers={"Content-Disposition": f'attachment; filename="{item["filename"]}"'},
    )
