# routers/upload.py
"""File upload → the set of 'active documents' Grace can read.

The frontend sends each file (base64 for images/PDFs/DOCX, raw text otherwise).
We hold a small LIST of active documents (capped) so Grace can reason across
several at once — compare two contracts, summarise three notes, cross-reference
a PDF and a spreadsheet. The reasoning loop feeds every active doc to Gemini as a
vision/PDF part or inline text.

`get_active_doc()` returns the single "primary" doc for the document editor and
the edit/create tools (the most-recently-added editable one); `get_active_docs()`
returns them all for context.
"""
import base64
import logging

from fastapi import APIRouter
from pydantic import BaseModel

from services import documents

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/upload", tags=["Uploads"])

# The active documents (most-recent last). In memory — transient context.
_active_docs = []
_MAX_DOCS = 6                 # cap so a pile of files can't blow up the prompt
_MAX_TEXT = 200_000          # cap inlined text per file


class UploadIn(BaseModel):
    name: str
    mime: str = ""
    kind: str = "text"          # image | pdf | docx | text
    content: str = ""           # base64 (image/pdf/docx) or raw text


def get_active_docs():
    """All active documents, oldest first."""
    return list(_active_docs)


def get_active_doc():
    """The primary doc for the editor / edit tools: the most-recently-added
    editable one, else the most recent of any kind, else None (used by callers to
    mean 'nothing attached')."""
    if not _active_docs:
        return None
    for d in reversed(_active_docs):
        if d.get("editable"):
            return d
    return _active_docs[-1]


def _add_doc(doc: dict):
    """Append a doc, replacing any existing one with the same name, and keep only
    the most recent _MAX_DOCS."""
    global _active_docs
    name = doc.get("name")
    _active_docs = [d for d in _active_docs if d.get("name") != name]
    _active_docs.append(doc)
    if len(_active_docs) > _MAX_DOCS:
        _active_docs = _active_docs[-_MAX_DOCS:]


def remove_doc(name: str) -> bool:
    global _active_docs
    before = len(_active_docs)
    _active_docs = [d for d in _active_docs if d.get("name") != name]
    return len(_active_docs) != before


def clear_active_doc():
    global _active_docs
    _active_docs = []


class RemoveIn(BaseModel):
    name: str


@router.post("/")
def upload(payload: UploadIn):
    name = (payload.name or "file").strip()[:200]
    kind = (payload.kind or "text").lower()

    if kind == "docx":
        # Word doc: decode the base64 and pull out editable text so Grace can
        # both read AND edit it in the document editor.
        text = documents.extract_text("docx", payload.mime, payload.content or "")
        if text is None:
            return {"ok": False, "error": "Could not read that Word document."}
        _add_doc({"name": name, "kind": "text", "mime": documents._DOCX_MIME,
                  "text": text[:_MAX_TEXT], "editable": True, "fmt": "docx"})
        logger.info(f"[Upload] +doc {name} (docx, {len(text)} chars) — {len(_active_docs)} active")
        return {"ok": True, "name": name, "kind": "docx", "editable": True,
                "fmt": "docx", "text": text[:_MAX_TEXT], "count": len(_active_docs)}

    if kind == "pdf":
        raw = payload.content or ""
        if "," in raw[:64] and raw[:5].lower() == "data:":
            raw = raw.split(",", 1)[1]
        try:
            data = base64.b64decode(raw)
        except Exception as e:
            logger.warning(f"Upload decode failed: {e}")
            return {"ok": False, "error": "Could not decode the file."}
        # A real text layer -> editable; a scanned/image PDF -> vision fallback.
        text = documents.extract_pdf_text(data) if hasattr(documents, "extract_pdf_text") else ""
        if text and len(text.strip()) >= 20:
            _add_doc({"name": name, "kind": "text", "mime": "application/pdf",
                      "text": text[:_MAX_TEXT], "editable": True, "fmt": "pdf"})
            logger.info(f"[Upload] +doc {name} (pdf-text) — {len(_active_docs)} active")
            return {"ok": True, "name": name, "kind": "pdf", "editable": True,
                    "fmt": "pdf", "text": text[:_MAX_TEXT], "count": len(_active_docs)}
        _add_doc({"name": name, "kind": "pdf", "mime": payload.mime or "application/pdf", "bytes": data})
        logger.info(f"[Upload] +doc {name} (pdf-scanned/vision) — {len(_active_docs)} active")
        return {"ok": True, "name": name, "kind": "pdf", "editable": False,
                "scanned": True, "count": len(_active_docs)}

    if kind == "image":
        raw = payload.content or ""
        if "," in raw[:64] and raw[:5].lower() == "data:":
            raw = raw.split(",", 1)[1]  # strip the data: URL prefix
        try:
            data = base64.b64decode(raw)
        except Exception as e:
            logger.warning(f"Upload decode failed: {e}")
            return {"ok": False, "error": "Could not decode the file."}
        mime = payload.mime or "image/png"
        _add_doc({"name": name, "kind": "image", "mime": mime, "bytes": data})
        logger.info(f"[Upload] +doc {name} (image) — {len(_active_docs)} active")
        return {"ok": True, "name": name, "kind": "image", "count": len(_active_docs)}

    # Plain text / markdown / etc. — editable prose.
    text = (payload.content or "")[:_MAX_TEXT]
    fmt = documents.fmt_from_name(name)
    editable = name.rsplit(".", 1)[-1].lower() in documents.EDITABLE_EXTS if "." in name else True
    _add_doc({"name": name, "kind": "text", "mime": payload.mime or "text/plain",
              "text": text, "editable": editable, "fmt": fmt})
    logger.info(f"[Upload] +doc {name} (text) — {len(_active_docs)} active")
    return {"ok": True, "name": name, "kind": "text", "editable": editable,
            "fmt": fmt, "text": text, "count": len(_active_docs)}


@router.get("/list")
def list_active():
    """The currently attached files (names + kind), for the panel/diagnostics."""
    return {
        "count": len(_active_docs),
        "files": [
            {"name": d.get("name"), "kind": d.get("kind"),
             "editable": bool(d.get("editable")), "fmt": d.get("fmt")}
            for d in _active_docs
        ],
    }


@router.post("/remove")
def remove(payload: RemoveIn):
    """Detach one file by name (the per-file ✕ in the upload panel)."""
    removed = remove_doc((payload.name or "").strip())
    return {"ok": True, "removed": removed, "count": len(_active_docs)}


@router.post("/clear")
def clear():
    clear_active_doc()
    return {"ok": True}
