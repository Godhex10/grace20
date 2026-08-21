# routers/google_actions.py
"""Actions the operator approves in the UI — currently sending an email. The
send only happens when the frontend posts here (i.e. he clicked Send on the
approval card); Grace herself never calls this."""
import logging

from fastapi import APIRouter
from pydantic import BaseModel

from services import google_integration, documents

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/email", tags=["Email"])


class SendIn(BaseModel):
    to: str
    subject: str = ""
    body: str = ""
    attach_id: str = ""     # id of a built document to attach (from the approval card)


@router.post("/send")
def send(payload: SendIn):
    attachments = None
    if payload.attach_id:
        item = documents.get_download(payload.attach_id)
        if item:
            attachments = [{"filename": item["filename"], "mime": item["mime"],
                            "data": item["data"]}]
        else:
            logger.warning(f"[email] attach_id {payload.attach_id} not found — sending without it")
    res = google_integration.send_email(payload.to, payload.subject, payload.body,
                                        attachments=attachments)
    if "error" in res:
        return {"ok": False, "error": res["error"]}
    return {"ok": True, "id": res.get("id")}
