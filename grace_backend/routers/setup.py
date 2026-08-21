# routers/setup.py
"""First-run setup for the packaged app: enter API keys once; they're saved to
%APPDATA%\\Grace\\.env (never into the binary). Read back masked so a partly-filled
config can be topped up."""
import logging

from fastapi import APIRouter
from pydantic import BaseModel

from services import appconfig

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/setup", tags=["Setup"])


def _mask(v: str) -> str:
    v = v or ""
    if len(v) <= 6:
        return "••••" if v else ""
    return v[:3] + "••••" + v[-3:]


@router.get("/status")
def status():
    vals = appconfig.current_values()
    return {
        "configured": appconfig.is_configured(),
        "fields": [
            {"key": k, "label": label, "required": req,
             "has_value": bool(vals.get(k)), "masked": _mask(vals.get(k))}
            for k, label, req in appconfig.FIELDS
        ],
    }


class SaveIn(BaseModel):
    values: dict


@router.post("")
def save(payload: SaveIn):
    if not (payload.values.get("GEMINI_API_KEY") or "").strip() and not appconfig.is_configured():
        return {"ok": False, "error": "A Gemini API key is required to get started."}
    try:
        path = appconfig.save(payload.values or {})
    except Exception as e:
        logger.warning(f"[setup] save failed: {e}")
        return {"ok": False, "error": f"Couldn't save: {e}"}
    return {"ok": True, "saved_to": str(path), "configured": appconfig.is_configured()}
