"""Local OS control — DESKTOP APP ONLY (GRACE_DESKTOP=1).

Safe, read-only ops (open a path, list a folder, read a file) run immediately.
Mutating ops (write a file, run a command) are STAGED as pending actions that the
operator must confirm in the UI before they execute — the same human-in-the-loop
model as email/propose_edit. A folder can be "trusted" for a while to skip repeat
prompts.

None of this is importable behaviour on the cloud server: is_enabled() is False
unless GRACE_DESKTOP=1, which only desktop.py sets.
"""
import os
import sys
import time
import uuid
import shutil
import subprocess
import logging

logger = logging.getLogger(__name__)


def is_enabled() -> bool:
    return os.environ.get("GRACE_DESKTOP", "") == "1"


def expand(path: str) -> str:
    """Expand ~ and %VARS% and return an absolute path."""
    p = os.path.expanduser(os.path.expandvars((path or "").strip()))
    return os.path.abspath(p)


# ── SAFE (run immediately) ──────────────────────────────────────────────────
def open_path(path: str) -> dict:
    """Open a file or folder with the OS default handler."""
    full = expand(path)
    if not os.path.exists(full):
        return {"error": f"Nothing exists at {full}."}
    try:
        if sys.platform == "win32":
            os.startfile(full)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", full])
        else:
            subprocess.Popen(["xdg-open", full])
        return {"ok": True, "opened": full,
                "kind": "folder" if os.path.isdir(full) else "file"}
    except Exception as e:
        logger.warning(f"[os] open_path failed: {e}")
        return {"error": str(e)}


def list_directory(path: str, max_items: int = 200) -> dict:
    full = expand(path or "~")
    if not os.path.isdir(full):
        return {"error": f"Not a folder: {full}."}
    try:
        names = sorted(os.listdir(full))
        entries = []
        for name in names[:max_items]:
            p = os.path.join(full, name)
            is_dir = os.path.isdir(p)
            entries.append({
                "name": name,
                "dir": is_dir,
                "size": (os.path.getsize(p) if (not is_dir and os.path.isfile(p)) else None),
            })
        return {"ok": True, "path": full, "count": len(names), "entries": entries}
    except Exception as e:
        return {"error": str(e)}


def read_local_file(path: str, max_chars: int = 20000) -> dict:
    full = expand(path)
    if not os.path.isfile(full):
        return {"error": f"Not a file: {full}."}
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as f:
            data = f.read(max_chars + 1)
        return {"ok": True, "path": full, "content": data[:max_chars],
                "truncated": len(data) > max_chars}
    except Exception as e:
        return {"error": str(e)}


# ── Trust tokens ────────────────────────────────────────────────────────────
_trusted: dict = {}   # absolute folder -> expiry epoch seconds


def _dir_of(path: str) -> str:
    full = expand(path)
    return full if os.path.isdir(full) else os.path.dirname(full)


def is_trusted(path: str) -> bool:
    now = time.time()
    for k in [k for k, v in _trusted.items() if v < now]:
        _trusted.pop(k, None)
    target = expand(path)
    for tdir, exp in _trusted.items():
        if exp >= now and (target == tdir or target.startswith(tdir + os.sep)):
            return True
    return False


def trust_folder(path: str, hours: float = 1.0) -> str:
    d = _dir_of(path)
    _trusted[d] = time.time() + hours * 3600
    return d


# ── Pending (gated) actions ─────────────────────────────────────────────────
_pending: dict = {}
_MAX_PENDING = 30


def stage(kind: str, payload: dict) -> str:
    aid = uuid.uuid4().hex[:12]
    _pending[aid] = {"kind": kind, "payload": payload, "ts": time.time()}
    if len(_pending) > _MAX_PENDING:
        for k in list(_pending)[: len(_pending) - _MAX_PENDING]:
            _pending.pop(k, None)
    return aid


def peek(aid: str):
    return _pending.get(aid)


def discard(aid: str):
    _pending.pop(aid, None)


def execute(aid: str) -> dict:
    """Run a previously-staged action (called after the operator approves)."""
    item = _pending.pop(aid, None)
    if not item:
        return {"error": "That action expired or was already handled."}
    return run_now(item["kind"], item["payload"])


def run_now(kind: str, payload: dict) -> dict:
    """Execute a mutating action immediately (used on approve, or when trusted)."""
    if kind == "run_command":
        return _run_command(payload.get("command", ""), payload.get("cwd"))
    if kind == "write_file":
        return _write_file(payload.get("path", ""), payload.get("content", ""))
    return {"error": f"Unknown action '{kind}'."}


def _run_command(command: str, cwd: str = None) -> dict:
    if not (command or "").strip():
        return {"error": "No command given."}
    cwd_full = expand(cwd) if cwd else None
    if cwd_full and not os.path.isdir(cwd_full):
        return {"error": f"Working folder doesn't exist: {cwd_full}."}
    try:
        proc = subprocess.run(command, shell=True, cwd=cwd_full,
                              capture_output=True, text=True, timeout=180)
        return {"ok": proc.returncode == 0, "code": proc.returncode,
                "stdout": (proc.stdout or "")[-4000:], "stderr": (proc.stderr or "")[-2000:]}
    except subprocess.TimeoutExpired:
        return {"error": "Command timed out (180s)."}
    except Exception as e:
        return {"error": str(e)}


def _write_file(path: str, content: str) -> dict:
    full = expand(path)
    try:
        backup = None
        if os.path.isfile(full):
            backup = full + ".grace.bak"
            shutil.copy2(full, backup)
        parent = os.path.dirname(full)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content or "")
        return {"ok": True, "path": full, "backup": backup,
                "bytes": len((content or "").encode("utf-8"))}
    except Exception as e:
        return {"error": str(e)}
