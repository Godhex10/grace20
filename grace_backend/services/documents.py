# services/documents.py
"""Document editing support — extract editable text from uploads (TXT/DOCX now,
PDF later), build downloadable output files (TXT/DOCX now), and hold the
generated files in a small in-memory registry the download endpoint serves.

Grace edits the *text*; when she produces a new document we render it back to the
requested format and hand the operator a download link. DOCX round-trips via
python-docx. PDF is a planned follow-up (needs a reader + writer library).
"""
import io
import os
import base64
import logging
import uuid

logger = logging.getLogger(__name__)

# A unicode TTF for PDF output (fpdf2 core fonts are latin-1 only). First that
# exists wins; if none, we fall back to Helvetica + latin-1 sanitisation.
_UNICODE_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\calibri.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

# Generated files waiting to be downloaded: id -> {filename, mime, data(bytes)}.
# In-memory + transient, matching the rest of the local-first design.
_downloads: dict = {}
_MAX_DOWNLOADS = 40
# id of the most recently built document — so "email the summary you just made"
# can grab it without the model juggling ids.
_last_build_id = None

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# File extensions we open in the document editor (prose, not code). PDFs are
# text-extracted and editable when they have a real text layer; scanned/image
# PDFs (no text) fall back to Grace's vision. (.md stays in the Code Panel;
# legacy .doc is not supported.)
EDITABLE_EXTS = ("txt", "docx", "pdf")


def fmt_from_name(name: str) -> str:
    """Best-effort output format from a filename ('report.docx' -> 'docx')."""
    ext = (name or "").rsplit(".", 1)[-1].lower() if "." in (name or "") else ""
    if ext in ("docx", "doc"):
        return "docx"
    if ext == "pdf":
        return "pdf"
    return "txt"


def _strip_data_url(raw: str) -> bytes:
    """Decode a base64 payload that may still carry a `data:...;base64,` prefix."""
    raw = raw or ""
    if raw[:5].lower() == "data:" and "," in raw[:128]:
        raw = raw.split(",", 1)[1]
    return base64.b64decode(raw)


def extract_docx_text(data: bytes) -> str:
    """DOCX bytes -> plain text (paragraphs joined by newlines)."""
    from docx import Document  # local import; python-docx already used elsewhere
    doc = Document(io.BytesIO(data))
    lines = [p.text for p in doc.paragraphs]
    # Pull table cell text too, so simple tabular docs aren't lost.
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                lines.append("\t".join(cells))
    return "\n".join(lines).strip()


def extract_pdf_text(data: bytes) -> str:
    """PDF bytes -> plain text (pages joined by blank lines). Empty string for a
    scanned/image PDF with no text layer."""
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages:
        try:
            pages.append((page.extract_text() or "").strip())
        except Exception:
            pages.append("")
    return "\n\n".join(p for p in pages if p).strip()


def extract_text(kind: str, mime: str, content: str) -> str:
    """Uploaded doc -> editable text. `content` is raw text for txt/md, or a
    base64 (data-URL) string for docx/pdf. Returns None on failure so the caller
    can fall back gracefully."""
    kind = (kind or "").lower()
    try:
        if kind == "docx" or mime == _DOCX_MIME:
            return extract_docx_text(_strip_data_url(content))
        if kind == "pdf" or mime == "application/pdf":
            return extract_pdf_text(_strip_data_url(content))
        # txt / md / anything already-text
        return content or ""
    except Exception as e:
        logger.warning(f"[documents] extract_text failed ({kind}): {e}")
        return None


def _build_docx(text: str) -> bytes:
    from docx import Document
    doc = Document()
    for line in (text or "").split("\n"):
        doc.add_paragraph(line)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _unicode_font_path():
    for p in _UNICODE_FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def _build_pdf(text: str) -> bytes:
    """Render plain text to a simple, clean A4 PDF (wrapped paragraphs). Uses a
    unicode TTF when one is available; otherwise falls back to Helvetica with
    latin-1 sanitisation so it never hard-fails on an odd character."""
    from fpdf import FPDF
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    font_path = _unicode_font_path()
    if font_path:
        pdf.add_font("Doc", "", font_path)
        pdf.set_font("Doc", size=11)
        body = text or ""
    else:
        pdf.set_font("Helvetica", size=11)
        body = (text or "").encode("latin-1", "replace").decode("latin-1")

    # multi_cell wraps long lines and honours the newlines already in the text.
    pdf.multi_cell(0, 6, body)
    out = pdf.output()
    return bytes(out)  # fpdf2 returns a bytearray


def build_document(text: str, fmt: str, base_name: str) -> dict:
    """Render `text` to `fmt` ('txt'|'docx'|'pdf'), register it for download, and
    return {id, filename, mime}. Unknown formats fall back to txt."""
    fmt = (fmt or "txt").lower()
    stem = (base_name or "document").rsplit(".", 1)[0] or "document"

    if fmt == "docx":
        data = _build_docx(text or "")
        filename, mime = f"{stem}.docx", _DOCX_MIME
    elif fmt == "pdf":
        try:
            data = _build_pdf(text or "")
            filename, mime = f"{stem}.pdf", "application/pdf"
        except Exception as e:
            logger.warning(f"[documents] PDF build failed, falling back to txt: {e}")
            data = (text or "").encode("utf-8")
            filename, mime = f"{stem}.txt", "text/plain"
    else:  # txt
        if fmt not in ("txt", "text"):
            logger.info(f"[documents] format '{fmt}' unknown — saving as .txt")
        data = (text or "").encode("utf-8")
        filename, mime = f"{stem}.txt", "text/plain"

    did = uuid.uuid4().hex[:16]
    _downloads[did] = {"filename": filename, "mime": mime, "data": data}
    global _last_build_id
    _last_build_id = did
    # Trim oldest if the registry grows unbounded.
    if len(_downloads) > _MAX_DOWNLOADS:
        for k in list(_downloads.keys())[: len(_downloads) - _MAX_DOWNLOADS]:
            _downloads.pop(k, None)
    logger.info(f"[documents] built {filename} ({len(data)} bytes) id={did}")
    return {"id": did, "filename": filename, "mime": mime}


def get_download(did: str):
    return _downloads.get(did)


def get_last_download():
    """(id, item) for the most recently built document, or (None, None) — used by
    'email the document you just made'."""
    if _last_build_id and _last_build_id in _downloads:
        return _last_build_id, _downloads[_last_build_id]
    return None, None
