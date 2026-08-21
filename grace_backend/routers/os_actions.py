# routers/os_actions.py
"""Operator-approved OS actions for the DESKTOP app. The frontend posts here when
he clicks Apply/Reject on an OS-action card. Every endpoint refuses unless
GRACE_DESKTOP=1, so these can never do anything on the cloud server."""
import logging

from fastapi import APIRouter
from pydantic import BaseModel

from services import os_control

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/os", tags=["OS"])


class ConfirmIn(BaseModel):
    id: str
    trust_folder: bool = False


class RejectIn(BaseModel):
    id: str


def _guard():
    return None if os_control.is_enabled() else {"ok": False, "error": "OS actions are only available in the desktop app."}


@router.post("/confirm")
def confirm(payload: ConfirmIn):
    blocked = _guard()
    if blocked:
        return blocked
    item = os_control.peek(payload.id)
    if not item:
        return {"ok": False, "error": "That action expired or was already handled."}
    # Optionally trust the action's folder so future actions there skip the prompt.
    if payload.trust_folder:
        p = item["payload"]
        target = p.get("cwd") or p.get("path") or ""
        if target:
            os_control.trust_folder(target, hours=1)
    result = os_control.execute(payload.id)
    return {"ok": "error" not in result, "kind": item["kind"], "result": result}


@router.post("/reject")
def reject(payload: RejectIn):
    blocked = _guard()
    if blocked:
        return blocked
    os_control.discard(payload.id)
    return {"ok": True}
