# routers/voice.py
"""On-demand streaming voice endpoint.

Rather than pre-rendering the whole MP3 and serving a static file, the pipeline
registers the reply text and hands the frontend a streaming URL. When the
browser requests it, edge-tts audio is streamed out chunk-by-chunk (and cached),
so playback starts within a few hundred ms of her text landing.
"""
import collections

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from services.audio import miso_voice

router = APIRouter(prefix="/api/voice", tags=["Voice"])

# token -> text awaiting synthesis. Capped so it can't grow without bound; the
# browser fetches each token once, right after it's registered.
_pending: "collections.OrderedDict[str, str]" = collections.OrderedDict()
_PENDING_MAX = 100


def register_speech(token: str, text: str) -> str:
    """Store `text` under `token` and return the streaming URL for the UI."""
    _pending[token] = text
    _pending.move_to_end(token)
    while len(_pending) > _PENDING_MAX:
        _pending.popitem(last=False)
    return f"/api/voice/{token}.mp3"


@router.get("/{token}.mp3")
async def stream_voice(token: str):
    text = _pending.get(token)
    if not text:
        raise HTTPException(status_code=404, detail="Voice line not found or expired.")
    return StreamingResponse(
        miso_voice.stream_speech(text),
        media_type="audio/mpeg",
        headers={"Cache-Control": "no-store"},
    )
