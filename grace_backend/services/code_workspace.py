# services/code_workspace.py
"""Read-only, sandboxed access to the project's code so Grace can browse, read,
and debug files. STRICTLY read-only and confined to CODE_ROOT — every path is
resolved and checked to stay inside the root (no `..` escapes, no absolute
paths outside). No writing, ever, in this phase.
"""
import os
import time
import uuid
import shutil
import difflib
import logging

logger = logging.getLogger(__name__)

# Sandbox root = the GRACE project folder (parent of grace_backend). Override
# with GRACE_CODE_ROOT.
_here = os.path.dirname(os.path.abspath(__file__))            # .../grace_backend/services
_default_root = os.path.abspath(os.path.join(_here, "..", ".."))  # .../gracev2-main
CODE_ROOT = os.path.abspath(os.environ.get("GRACE_CODE_ROOT", _default_root))

# Directories never worth showing / walking.
_SKIP_DIRS = {
    ".venv", "venv", "node_modules", ".git", "__pycache__", ".grace",
    ".idea", ".vscode", "dist", "build", ".pytest_cache", ".mypy_cache",
}
# Only read files that are plausibly text/code.
_TEXT_EXTS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css", ".json", ".md",
    ".txt", ".csv", ".xml", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".env",
    ".sh", ".bat", ".php", ".sql", ".c", ".cpp", ".h", ".java", ".go", ".rs",
    ".rb", ".vue", ".svelte", ".gitignore", ".dockerfile",
}
_MAX_READ_BYTES = 200_000       # cap a single file read
_MAX_LIST = 400                  # cap entries returned
_MAX_SEARCH_HITS = 60


def _resolve(rel: str) -> str:
    """Resolve `rel` against CODE_ROOT and guarantee it stays inside. Raises
    ValueError on any escape attempt."""
    rel = (rel or "").strip().lstrip("/\\")
    if rel in (".", ""):
        return CODE_ROOT
    target = os.path.abspath(os.path.join(CODE_ROOT, rel))
    if target != CODE_ROOT and not target.startswith(CODE_ROOT + os.sep):
        raise ValueError("Path is outside the allowed project folder.")
    return target


def _rel(path: str) -> str:
    return os.path.relpath(path, CODE_ROOT).replace("\\", "/")


def list_dir(rel: str = ".") -> dict:
    try:
        base = _resolve(rel)
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.isdir(base):
        return {"error": f"'{rel}' is not a folder."}

    dirs, files = [], []
    try:
        for name in sorted(os.listdir(base)):
            if name in _SKIP_DIRS:
                continue
            full = os.path.join(base, name)
            if os.path.isdir(full):
                dirs.append(_rel(full))
            else:
                files.append(_rel(full))
    except Exception as e:
        return {"error": f"Could not list '{rel}': {e}"}
    return {"path": _rel(base), "dirs": dirs[:_MAX_LIST], "files": files[:_MAX_LIST]}


def read_file(rel: str) -> dict:
    try:
        target = _resolve(rel)
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.isfile(target):
        return {"error": f"'{rel}' is not a file."}
    ext = os.path.splitext(target)[1].lower()
    name = os.path.basename(target)
    if ext and ext not in _TEXT_EXTS and name not in _TEXT_EXTS:
        return {"error": f"'{rel}' isn't a readable text/code file."}
    try:
        size = os.path.getsize(target)
        with open(target, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(_MAX_READ_BYTES)
        truncated = size > _MAX_READ_BYTES
    except Exception as e:
        return {"error": f"Could not read '{rel}': {e}"}
    return {
        "path": _rel(target),
        "name": name,
        "ext": ext.lstrip("."),
        "content": content,
        "truncated": truncated,
    }


def search(query: str, rel: str = ".") -> dict:
    query = (query or "").strip()
    if not query:
        return {"error": "No search query given."}
    try:
        base = _resolve(rel)
    except ValueError as e:
        return {"error": str(e)}

    hits = []
    low = query.lower()
    for root, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext and ext not in _TEXT_EXTS:
                continue
            full = os.path.join(root, fn)
            try:
                with open(full, "r", encoding="utf-8", errors="ignore") as f:
                    for i, line in enumerate(f, 1):
                        if low in line.lower():
                            hits.append({"path": _rel(full), "line": i, "text": line.strip()[:200]})
                            if len(hits) >= _MAX_SEARCH_HITS:
                                return {"query": query, "hits": hits, "capped": True}
            except Exception:
                continue
    return {"query": query, "hits": hits, "capped": False}


# ── Phase B: staged edits (propose → diff → approve) ────────────────────────
# Grace never writes directly. She STAGES a proposed edit here; it's applied to
# the real file only when the operator approves it (apply_edit). The original is
# backed up first, so there's always an undo.
_pending = {}  # edit_id -> {path, orig, new, summary}
_BACKUP_DIR = os.path.join(CODE_ROOT, ".grace", "backups")


def _unified_diff(orig: str, new: str, path: str):
    lines = list(difflib.unified_diff(
        orig.splitlines(), new.splitlines(),
        fromfile=f"a/{path}", tofile=f"b/{path}", lineterm="",
    ))
    return lines


def is_project_file(rel: str) -> bool:
    """True if `rel` points to a real file inside the sandbox."""
    try:
        return os.path.isfile(_resolve(rel))
    except ValueError:
        return False


def _stage(name: str, content: str, old_code: str, new_code: str, summary: str, kind: str) -> dict:
    """Shared staging: swap a unique `old_code` for `new_code` in `content`."""
    old_code = old_code or ""
    if not old_code:
        return {"error": "No original snippet given to locate the change."}
    count = content.count(old_code)
    if count == 0:
        return {"error": "Couldn't find that exact code — it may have changed; re-read it."}
    if count > 1:
        return {"error": f"That snippet appears {count} times — add more surrounding context so it's unique."}
    new_content = content.replace(old_code, new_code, 1)
    if new_content == content:
        return {"error": "That edit wouldn't change anything."}

    edit_id = uuid.uuid4().hex[:10]
    _pending[edit_id] = {
        "path": name, "orig": content, "new": new_content,
        "summary": (summary or "").strip(), "kind": kind,
    }
    if len(_pending) > 40:
        for k in list(_pending)[:-40]:
            _pending.pop(k, None)

    out = {
        "edit_id": edit_id, "path": name, "summary": summary,
        "diff": _unified_diff(content, new_content, name), "kind": kind,
    }
    if kind == "upload":
        # The browser has no disk path for an uploaded file, so Apply = download.
        out["filename"] = name
        out["new_content"] = new_content
    return out


def stage_edit(rel: str, old_code: str, new_code: str, summary: str = "") -> dict:
    """Stage a surgical edit to a PROJECT file (written to disk on approval)."""
    res = read_file(rel)
    if "error" in res:
        return {"error": res["error"]}
    if res.get("truncated"):
        return {"error": "File is too large to safely edit whole; narrow it down."}
    return _stage(res["path"], res["content"], old_code, new_code, summary, "project")


def stage_edit_content(name: str, content: str, old_code: str, new_code: str, summary: str = "") -> dict:
    """Stage an edit to an UPLOADED file (content in memory; Apply downloads it)."""
    return _stage(name or "file", content or "", old_code, new_code, summary, "upload")


def get_pending(edit_id: str):
    return _pending.get(edit_id)


def apply_edit(edit_id: str) -> dict:
    """Write a staged edit to the real file, backing up the original first."""
    edit = _pending.get(edit_id)
    if not edit:
        return {"error": "That edit is no longer pending (already applied or expired)."}
    if edit.get("kind") == "upload":
        # Uploaded files have no disk path — they're applied by download client-side.
        _pending.pop(edit_id, None)
        return {"ok": True, "kind": "upload", "path": edit["path"], "content": edit["new"]}
    try:
        target = _resolve(edit["path"])
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.isfile(target):
        return {"error": "The file no longer exists."}

    try:
        # Back up the current on-disk version before overwriting.
        os.makedirs(_BACKUP_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safe = edit["path"].replace("/", "__")
        shutil.copy2(target, os.path.join(_BACKUP_DIR, f"{safe}.{stamp}.bak"))
        with open(target, "w", encoding="utf-8", newline="") as f:
            f.write(edit["new"])
    except Exception as e:
        logger.error(f"apply_edit write failed: {e}", exc_info=True)
        return {"error": f"Failed to write the file: {e}"}

    _pending.pop(edit_id, None)
    logger.info(f"[CodeWorkspace] Applied edit to {edit['path']} (backup saved).")
    return {"ok": True, "path": edit["path"], "content": edit["new"]}


def reject_edit(edit_id: str) -> dict:
    existed = _pending.pop(edit_id, None) is not None
    return {"ok": existed}
