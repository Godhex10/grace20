# routers/code.py
"""Approve / reject Grace's staged code edits.

Grace only ever STAGES an edit (via the propose_edit tool). The real file is
written only when the operator clicks Apply here — the human-in-the-loop gate.
"""
from fastapi import APIRouter, HTTPException

from services import code_workspace
from core.sse import push_workspace_update

router = APIRouter(prefix="/api/code", tags=["Code Edits"])


@router.post("/apply/{edit_id}")
async def apply_edit(edit_id: str):
    res = code_workspace.apply_edit(edit_id)
    if "error" in res:
        raise HTTPException(status_code=400, detail=res["error"])
    # Re-open the freshly written file in the Code Panel so he sees the result.
    await push_workspace_update(
        "WIDGET_CODE",
        {"action": "open_file", "filename": res["path"].split("/")[-1], "content": res["content"]},
    )
    return res


@router.post("/reject/{edit_id}")
async def reject_edit(edit_id: str):
    return code_workspace.reject_edit(edit_id)
