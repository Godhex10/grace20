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

try:
    import ctypes  # Windows key/power/display control
except Exception:
    ctypes = None

logger = logging.getLogger(__name__)


def is_enabled() -> bool:
    return os.environ.get("GRACE_DESKTOP", "") == "1"


_KF_CACHE = None


def _known_folders() -> dict:
    """Real paths of Desktop/Documents/Pictures/Downloads/Music/Videos — reads the
    Windows shell registry so OneDrive-redirected folders resolve correctly."""
    global _KF_CACHE
    if _KF_CACHE is not None:
        return _KF_CACHE
    kf = {}
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders") as k:
                mapping = {"desktop": "Desktop", "documents": "Personal",
                           "pictures": "My Pictures", "music": "My Music",
                           "videos": "My Video",
                           "downloads": "{374DE290-123F-4565-9164-39C4925E467B}"}
                for friendly, valname in mapping.items():
                    try:
                        v, _ = winreg.QueryValueEx(k, valname)
                        p = os.path.expandvars(v)
                        if os.path.isdir(p):
                            kf[friendly] = p
                    except Exception:
                        pass
        except Exception:
            pass
    _KF_CACHE = kf
    return kf


def expand(path: str) -> str:
    """Resolve a path to an absolute one. Understands ~, %VARS%, and Windows known
    folders (Pictures/Documents/Desktop/…) — including OneDrive redirection."""
    raw = (path or "").strip().strip('"').strip("'")
    if not raw:
        return os.path.abspath(os.path.expanduser("~"))
    norm = raw.replace("\\", "/")
    stripped = "" if norm == "~" else (norm[2:] if norm.lower().startswith("~/") else norm)
    parts = [s for s in stripped.split("/") if s]
    kf = _known_folders()
    if parts and parts[0].lower() in kf:
        return os.path.abspath(os.path.join(kf[parts[0].lower()], *parts[1:]))
    full = os.path.abspath(os.path.expanduser(os.path.expandvars(raw)))
    # Fallback: a ~/Folder path that's missing but present under OneDrive.
    if not os.path.exists(full) and parts:
        od = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer")
        if od:
            cand = os.path.abspath(os.path.join(od, *parts))
            if os.path.exists(cand):
                return cand
    return full


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


# Friendly app names → the executable Windows knows them by.
_APP_ALIASES = {
    "notepad": "notepad.exe", "wordpad": "write.exe", "word": "winword.exe",
    "excel": "excel.exe", "powerpoint": "powerpnt.exe", "paint": "mspaint.exe",
    "explorer": "explorer.exe", "file explorer": "explorer.exe",
    "vscode": "code", "vs code": "code", "code": "code", "visual studio code": "code",
    "chrome": "chrome.exe", "google chrome": "chrome.exe",
    "edge": "msedge.exe", "firefox": "firefox.exe",
    "photos": "ms-photos:", "vlc": "vlc.exe",
}


def open_with(path: str, app: str) -> dict:
    """Open a file with a SPECIFIC app the operator names (e.g. 'open this with
    Notepad / VS Code / Chrome'). Falls back to the default handler if no app."""
    full = expand(path)
    if not os.path.exists(full):
        return {"error": f"Nothing exists at {full}."}
    if not (app or "").strip():
        return open_path(path)
    exe = _APP_ALIASES.get(app.strip().lower(), app.strip())
    try:
        if sys.platform == "win32":
            try:
                subprocess.Popen([exe, full])            # launch the app with the file
            except (FileNotFoundError, OSError):
                # app not on PATH → let the shell resolve the name / App Paths
                subprocess.Popen(f'start "" "{exe}" "{full}"', shell=True)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-a", exe, full])
        else:
            subprocess.Popen([exe, full])
        return {"ok": True, "opened": full, "app": app}
    except Exception as e:
        logger.warning(f"[os] open_with failed: {e}")
        return {"error": f"Couldn't open it with {app}: {e}"}


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
    if kind == "file_op":
        return do_file_op(payload.get("op", ""), payload.get("path", ""), payload.get("dest"))
    if kind == "batch_rename":
        return do_batch_rename(payload.get("folder", ""), payload.get("prefix", ""), payload.get("ext"))
    if kind == "kill_pid":
        return close_process_pid(payload.get("pid"))
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


# ══════════════════════════════════════════════════════════════════════════
#  System control (desktop only) — power, media, screen, apps, files, status
# ══════════════════════════════════════════════════════════════════════════

def _tap_key(vk: int, times: int = 1):
    KEYUP = 0x0002
    u = ctypes.windll.user32
    for _ in range(max(1, times)):
        u.keybd_event(vk, 0, 0, 0)
        u.keybd_event(vk, 0, KEYUP, 0)


# ── Power & session ─────────────────────────────────────────────────────────
def system_power(action: str, delay_minutes=0) -> dict:
    action = (action or "").lower().strip().replace(" ", "_")
    if sys.platform != "win32":
        return {"error": "Power control is Windows-only right now."}
    try:
        d = int(float(delay_minutes or 0)) * 60
    except Exception:
        d = 0
    try:
        if action in ("shutdown", "shut_down", "power_off", "poweroff"):
            secs = d if d else 15
            subprocess.run(f"shutdown /s /t {secs}", shell=True)
            return {"ok": True, "action": "shutdown", "in_seconds": secs, "cancelable": True}
        if action in ("restart", "reboot"):
            secs = d if d else 15
            subprocess.run(f"shutdown /r /t {secs}", shell=True)
            return {"ok": True, "action": "restart", "in_seconds": secs, "cancelable": True}
        if action in ("logoff", "log_off", "signout", "sign_out"):
            subprocess.run("shutdown /l", shell=True)
            return {"ok": True, "action": "logoff"}
        if action == "hibernate":
            subprocess.run("shutdown /h", shell=True)
            return {"ok": True, "action": "hibernate"}
        if action == "sleep":
            subprocess.run("rundll32.exe powrprof.dll,SetSuspendState 0,1,0", shell=True)
            return {"ok": True, "action": "sleep"}
        if action == "lock":
            ctypes.windll.user32.LockWorkStation()
            return {"ok": True, "action": "lock"}
        if action in ("cancel", "cancel_shutdown", "abort"):
            subprocess.run("shutdown /a", shell=True)
            return {"ok": True, "action": "cancel"}
        return {"error": f"Unknown power action '{action}'."}
    except Exception as e:
        return {"error": str(e)}


# ── Media & volume ──────────────────────────────────────────────────────────
_VK = {"volume_up": 0xAF, "volume_down": 0xAE, "mute": 0xAD,
       "play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2}


def _set_volume(percent: int) -> dict:
    percent = max(0, min(100, int(percent)))
    try:
        from ctypes import cast, POINTER
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        dev = AudioUtilities.GetSpeakers()
        itf = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        vol = cast(itf, POINTER(IAudioEndpointVolume))
        vol.SetMasterVolumeLevelScalar(percent / 100.0, None)
        return {"ok": True, "action": "set_volume", "level": percent}
    except Exception:
        _tap_key(_VK["volume_down"], 50)
        _tap_key(_VK["volume_up"], max(0, round(percent / 2)))
        return {"ok": True, "action": "set_volume", "level": percent, "approx": True}


def media_control(action: str, level=None) -> dict:
    action = (action or "").lower().strip().replace(" ", "_")
    if sys.platform != "win32":
        return {"error": "Media control is Windows-only right now."}
    try:
        if action in ("set_volume", "volume") and level is not None:
            return _set_volume(int(level))
        if action in ("volume_up", "louder", "up"):
            _tap_key(_VK["volume_up"], 5); return {"ok": True, "action": "volume_up"}
        if action in ("volume_down", "quieter", "down"):
            _tap_key(_VK["volume_down"], 5); return {"ok": True, "action": "volume_down"}
        if action in ("mute", "unmute", "toggle_mute"):
            _tap_key(_VK["mute"]); return {"ok": True, "action": "mute"}
        if action in ("play_pause", "play", "pause"):
            _tap_key(_VK["play_pause"]); return {"ok": True, "action": "play_pause"}
        if action in ("next", "next_track", "skip"):
            _tap_key(_VK["next"]); return {"ok": True, "action": "next"}
        if action in ("previous", "prev", "previous_track", "back"):
            _tap_key(_VK["previous"]); return {"ok": True, "action": "previous"}
        if action == "stop":
            _tap_key(_VK["stop"]); return {"ok": True, "action": "stop"}
        return {"error": f"Unknown media action '{action}'."}
    except Exception as e:
        return {"error": str(e)}


# ── Screen ──────────────────────────────────────────────────────────────────
def screenshot() -> dict:
    try:
        from PIL import ImageGrab
        from services import appconfig
        d = os.path.join(appconfig.runtime_dir(), "screenshots")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"screen_{int(time.time())}.png")
        img = ImageGrab.grab()
        img.save(path)
        return {"ok": True, "path": path, "width": img.width, "height": img.height}
    except Exception as e:
        return {"error": f"Screenshot failed: {e}"}


def display_control(action: str) -> dict:
    action = (action or "").lower().strip().replace(" ", "_")
    if sys.platform != "win32":
        return {"error": "Display control is Windows-only right now."}
    try:
        if action in ("monitor_off", "screen_off", "turn_off_monitor", "display_off"):
            ctypes.windll.user32.SendMessageW(0xFFFF, 0x0112, 0xF170, 2)
            return {"ok": True, "action": "monitor_off"}
        if action in ("show_desktop", "minimize_all", "minimise_all"):
            VK_LWIN, VK_D, KEYUP = 0x5B, 0x44, 0x0002
            u = ctypes.windll.user32
            u.keybd_event(VK_LWIN, 0, 0, 0); u.keybd_event(VK_D, 0, 0, 0)
            u.keybd_event(VK_D, 0, KEYUP, 0); u.keybd_event(VK_LWIN, 0, KEYUP, 0)
            return {"ok": True, "action": "show_desktop"}
        if action in ("extend", "duplicate", "clone", "external", "internal",
                      "second_only", "pc_only"):
            return monitor_mode(action)
        return {"error": f"Unknown display action '{action}'."}
    except Exception as e:
        return {"error": str(e)}


# ── Apps, windows & processes ───────────────────────────────────────────────
def close_window(title: str) -> dict:
    """Gracefully close top-level windows whose title contains `title` — e.g. a
    File Explorer folder window ('Downloads'), or one browser window. Sends
    WM_CLOSE so the app can prompt to save if needed."""
    if sys.platform != "win32":
        return {"error": "Window control is Windows-only right now."}
    target = (title or "").lower().strip()
    if not target:
        return {"error": "Which window?"}
    user32 = ctypes.windll.user32
    WM_CLOSE = 0x0010
    closed = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _cb(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        t = buf.value or ""
        if target in t.lower():
            user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            closed.append(t)
        return True

    try:
        user32.EnumWindows(_cb, 0)
        return {"ok": True, "action": "close_window", "closed": closed}
    except Exception as e:
        return {"error": str(e)}


def app_control(action: str, name: str) -> dict:
    action = (action or "").lower().strip()
    name = (name or "").strip()
    if not name:
        return {"error": "Which app?"}
    try:
        if action in ("close_window", "close_folder", "close_win"):
            return close_window(name)
        if action in ("launch", "open", "start", "run"):
            exe = _APP_ALIASES.get(name.lower(), name)
            try:
                subprocess.Popen([exe])
            except (FileNotFoundError, OSError):
                subprocess.Popen(f'start "" "{exe}"', shell=True)
            return {"ok": True, "action": "launch", "app": name}
        if action in ("close", "quit", "kill", "force_close", "terminate"):
            base = _APP_ALIASES.get(name.lower(), name)
            img = base if base.lower().endswith(".exe") else base + ".exe"
            r = subprocess.run(f'taskkill /IM "{img}" /F', shell=True,
                               capture_output=True, text=True)
            return {"ok": r.returncode == 0, "action": "close", "app": name,
                    "detail": ((r.stdout or "") + (r.stderr or "")).strip()[:200]}
        if action in ("focus", "switch", "activate"):
            ps = f'(New-Object -ComObject WScript.Shell).AppActivate("{name}")'
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, text=True, timeout=10)
            return {"ok": True, "action": "focus", "app": name}
        return {"error": f"Unknown app action '{action}'."}
    except Exception as e:
        return {"error": str(e)}


def list_processes(top: int = 12) -> dict:
    try:
        import psutil
        agg = {}
        for p in psutil.process_iter(["name", "memory_info"]):
            try:
                mem = p.info["memory_info"].rss if p.info.get("memory_info") else 0
                nm = p.info.get("name") or "?"
                agg[nm] = agg.get(nm, 0) + mem
            except Exception:
                pass
        rows = sorted(agg.items(), key=lambda x: -x[1])[:top]
        return {"ok": True, "processes": [{"name": n, "mem_mb": round(m / 1048576)} for n, m in rows]}
    except Exception as e:
        return {"error": str(e)}


# ── File search & operations ────────────────────────────────────────────────
def find_file(name: str, root: str = None, max_results: int = 25, max_scan: int = 200000) -> dict:
    q = (name or "").lower().strip()
    if not q:
        return {"error": "What file name should I look for?"}
    base = expand(root) if root else os.path.expanduser("~")
    if not os.path.isdir(base):
        return {"error": f"Not a folder: {base}."}
    skip = {"node_modules", ".git", "AppData", "$Recycle.Bin", "Windows", "__pycache__"}
    hits, scanned = [], 0
    try:
        for dp, dns, fns in os.walk(base):
            dns[:] = [d for d in dns if d not in skip]
            for f in fns:
                scanned += 1
                if q in f.lower():
                    hits.append(os.path.join(dp, f))
                    if len(hits) >= max_results:
                        return {"ok": True, "base": base, "matches": hits, "truncated": True}
            if scanned > max_scan:
                return {"ok": True, "base": base, "matches": hits, "truncated": True}
        return {"ok": True, "base": base, "matches": hits, "truncated": False}
    except Exception as e:
        return {"error": str(e)}


def do_file_op(op: str, path: str, dest: str = None) -> dict:
    op = (op or "").lower().strip()
    src = expand(path) if path else ""
    try:
        if op in ("create_folder", "mkdir", "new_folder"):
            os.makedirs(src, exist_ok=True)
            return {"ok": True, "op": "create_folder", "path": src}
        if op in ("delete", "remove", "trash", "recycle"):
            try:
                from send2trash import send2trash
                send2trash(src)
                return {"ok": True, "op": "delete", "path": src, "recycled": True}
            except Exception:
                if os.path.isdir(src):
                    shutil.rmtree(src)
                else:
                    os.remove(src)
                return {"ok": True, "op": "delete", "path": src, "recycled": False}
        if op in ("rename", "move"):
            d = expand(dest) if dest else ""
            if not d:
                return {"error": "Need a destination / new name."}
            shutil.move(src, d)
            return {"ok": True, "op": op, "path": src, "dest": d}
        if op == "copy":
            d = expand(dest) if dest else ""
            if not d:
                return {"error": "Need a destination."}
            if os.path.isdir(src):
                shutil.copytree(src, d)
            else:
                shutil.copy2(src, d)
            return {"ok": True, "op": "copy", "path": src, "dest": d}
        if op in ("empty_recycle_bin", "empty_recycle", "empty_trash"):
            subprocess.run(["powershell", "-NoProfile", "-Command",
                            "Clear-RecycleBin -Force -ErrorAction SilentlyContinue"], timeout=30)
            return {"ok": True, "op": "empty_recycle_bin"}
        return {"error": f"Unknown file op '{op}'."}
    except Exception as e:
        return {"error": str(e)}


# ── Clipboard & typing ──────────────────────────────────────────────────────
def clipboard_get() -> dict:
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                           capture_output=True, text=True, timeout=10)
        return {"ok": True, "text": (r.stdout or "").rstrip("\r\n")}
    except Exception as e:
        return {"error": str(e)}


def clipboard_set(text: str) -> dict:
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", "$input | Set-Clipboard"],
                       input=(text or ""), text=True, timeout=10)
        return {"ok": True}
    except Exception as e:
        return {"error": str(e)}


def type_text(text: str) -> dict:
    """Type into whatever window currently has focus (via SendKeys)."""
    if not (text or "").strip():
        return {"error": "Nothing to type."}
    try:
        import re as _re
        esc = _re.sub(r'([+^%~(){}\[\]])', r'{\1}', text)
        time.sleep(0.4)
        env = dict(os.environ, GRACE_TT=esc)
        subprocess.run(["powershell", "-NoProfile", "-Command",
                        "(New-Object -ComObject WScript.Shell).SendKeys($env:GRACE_TT)"],
                       env=env, timeout=15)
        return {"ok": True, "typed_len": len(text)}
    except Exception as e:
        return {"error": str(e)}


# ── System status ───────────────────────────────────────────────────────────
def system_status() -> dict:
    try:
        import psutil, socket
        vm = psutil.virtual_memory()
        du = psutil.disk_usage(os.path.abspath(os.sep))
        batt = None
        try:
            b = psutil.sensors_battery()
            if b:
                batt = {"percent": round(b.percent), "plugged": bool(b.power_plugged)}
        except Exception:
            pass
        try:
            ip = socket.gethostbyname(socket.gethostname())
        except Exception:
            ip = "?"
        up = int(time.time() - psutil.boot_time())
        return {"ok": True, "cpu_percent": psutil.cpu_percent(interval=0.3),
                "ram_percent": vm.percent, "ram_used_gb": round(vm.used / 1e9, 1),
                "ram_total_gb": round(vm.total / 1e9, 1), "disk_percent": du.percent,
                "disk_free_gb": round(du.free / 1e9, 1), "battery": batt, "ip": ip,
                "uptime_hours": round(up / 3600, 1)}
    except Exception as e:
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════
#  Web, network, display & handy extras (desktop only)
# ══════════════════════════════════════════════════════════════════════════
import webbrowser
import urllib.parse
import re as _re


# ── Web & play ──────────────────────────────────────────────────────────────
def open_url(url: str) -> dict:
    u = (url or "").strip()
    if not u:
        return {"error": "Which site?"}
    if not _re.match(r"^https?://", u, _re.I):
        if "." in u and " " not in u:
            u = "https://" + u
        else:
            return web_search(u)          # not a domain → treat as a search
    try:
        webbrowser.open(u)
        return {"ok": True, "url": u}
    except Exception as e:
        return {"error": str(e)}


def web_search(query: str) -> dict:
    q = (query or "").strip()
    if not q:
        return {"error": "Search for what?"}
    try:
        webbrowser.open("https://www.google.com/search?q=" + urllib.parse.quote(q))
        return {"ok": True, "query": q}
    except Exception as e:
        return {"error": str(e)}


def play_media(query: str, service: str = "youtube") -> dict:
    q = (query or "").strip()
    s = (service or "youtube").lower()
    if not q:
        return {"error": "Play what?"}
    try:
        if "spotify" in s:
            webbrowser.open("https://open.spotify.com/search/" + urllib.parse.quote(q))
            return {"ok": True, "service": "spotify", "query": q}
        webbrowser.open("https://www.youtube.com/results?search_query=" + urllib.parse.quote(q))
        return {"ok": True, "service": "youtube", "query": q}
    except Exception as e:
        return {"error": str(e)}


# ── Network & connectivity ──────────────────────────────────────────────────
def network(action: str, name: str = None) -> dict:
    action = (action or "").lower().strip().replace(" ", "_")
    try:
        if action in ("online", "internet", "is_online", "check"):
            try:
                import httpx
                httpx.head("https://www.google.com", timeout=4)
                return {"ok": True, "online": True}
            except Exception:
                r = subprocess.run("ping -n 1 -w 1500 8.8.8.8", shell=True,
                                   capture_output=True, text=True)
                return {"ok": True, "online": r.returncode == 0}
        if action in ("public_ip", "my_ip", "external_ip"):
            try:
                import httpx
                ip = httpx.get("https://api.ipify.org", timeout=5).text.strip()
                return {"ok": True, "public_ip": ip}
            except Exception as e:
                return {"error": f"Couldn't fetch public IP: {e}"}
        if action in ("flush_dns", "flushdns"):
            subprocess.run("ipconfig /flushdns", shell=True)
            return {"ok": True, "action": "flush_dns"}
        if action in ("wifi_disconnect", "disconnect"):
            subprocess.run("netsh wlan disconnect", shell=True)
            return {"ok": True, "action": "wifi_disconnect"}
        if action in ("wifi_connect", "connect"):
            if not name:
                return {"error": "Which network?"}
            r = subprocess.run(f'netsh wlan connect name="{name}"', shell=True,
                               capture_output=True, text=True)
            return {"ok": r.returncode == 0, "action": "wifi_connect", "name": name,
                    "detail": ((r.stdout or "") + (r.stderr or "")).strip()[:200]}
        if action in ("wifi_status", "status", "wifi_name", "current"):
            r = subprocess.run("netsh wlan show interfaces", shell=True,
                               capture_output=True, text=True)
            m = _re.search(r"^\s*SSID\s*:\s*(.+)$", r.stdout or "", _re.M)
            sig = _re.search(r"Signal\s*:\s*(.+)$", r.stdout or "", _re.M)
            return {"ok": True, "ssid": (m.group(1).strip() if m else None),
                    "signal": (sig.group(1).strip() if sig else None)}
        if action in ("wifi_password", "password", "show_password"):
            n = name
            if not n:
                r0 = subprocess.run("netsh wlan show interfaces", shell=True,
                                    capture_output=True, text=True)
                m = _re.search(r"^\s*SSID\s*:\s*(.+)$", r0.stdout or "", _re.M)
                n = m.group(1).strip() if m else None
            if not n:
                return {"error": "Which network's password?"}
            r = subprocess.run(f'netsh wlan show profile name="{n}" key=clear', shell=True,
                               capture_output=True, text=True)
            m = _re.search(r"Key Content\s*:\s*(.+)$", r.stdout or "", _re.M)
            return {"ok": True, "name": n, "password": (m.group(1).strip() if m else None)}
        if action in ("list_networks", "networks", "scan"):
            r = subprocess.run("netsh wlan show networks", shell=True,
                               capture_output=True, text=True)
            nets = _re.findall(r"^\s*SSID \d+\s*:\s*(.+)$", r.stdout or "", _re.M)
            return {"ok": True, "networks": [n.strip() for n in nets if n.strip()]}
        if action in ("wifi_on", "enable_wifi", "wifi_off", "disable_wifi"):
            state = "enabled" if ("on" in action or "enable" in action) else "disabled"
            r = subprocess.run(f'netsh interface set interface "Wi-Fi" admin={state}',
                               shell=True, capture_output=True, text=True)
            ok = r.returncode == 0
            return {"ok": ok, "action": action,
                    "note": None if ok else "may need to run as administrator"}
        return {"error": f"Unknown network action '{action}'."}
    except Exception as e:
        return {"error": str(e)}


# ── Brightness & monitor mode ───────────────────────────────────────────────
def _get_brightness():
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        "(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightness).CurrentBrightness"],
                       capture_output=True, text=True, timeout=10)
    try:
        return int((r.stdout or "").strip().splitlines()[0])
    except Exception:
        return None


def _set_brightness(percent):
    percent = max(0, min(100, int(percent)))
    r = subprocess.run(["powershell", "-NoProfile", "-Command",
                        f"(Get-WmiObject -Namespace root/WMI -Class WmiMonitorBrightnessMethods).WmiSetBrightness(1,{percent})"],
                       capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        return {"error": "This display doesn't support software brightness (common on desktop monitors)."}
    return {"ok": True, "level": percent}


def brightness(action: str, level=None) -> dict:
    action = (action or "").lower().strip()
    if action == "set" and level is not None:
        return _set_brightness(level)
    cur = _get_brightness()
    if cur is None:
        return {"error": "Couldn't read brightness (desktop monitor?)."}
    if action in ("up", "increase", "brighter"):
        return _set_brightness(min(100, cur + 15))
    if action in ("down", "decrease", "dimmer"):
        return _set_brightness(max(0, cur - 15))
    return {"ok": True, "level": cur}


def monitor_mode(mode: str) -> dict:
    m = (mode or "").lower().strip()
    arg = {"extend": "/extend", "duplicate": "/clone", "clone": "/clone",
           "second_only": "/external", "external": "/external",
           "internal": "/internal", "pc_only": "/internal"}.get(m)
    if not arg:
        return {"error": "Use extend | duplicate | external | internal."}
    subprocess.Popen(f"DisplaySwitch.exe {arg}", shell=True)
    return {"ok": True, "mode": m}


# ── Advanced window control ─────────────────────────────────────────────────
def _find_window(title: str):
    user32 = ctypes.windll.user32
    target = (title or "").lower().strip()
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _cb(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        b = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, b, n + 1)
        if target and target in (b.value or "").lower():
            found.append(hwnd)
        return True

    user32.EnumWindows(_cb, 0)
    return found[0] if found else None


def window_control(action: str, title: str) -> dict:
    action = (action or "").lower().strip().replace(" ", "_")
    if sys.platform != "win32":
        return {"error": "Window control is Windows-only right now."}
    hwnd = _find_window(title)
    if not hwnd:
        return {"error": f"No open window titled '{title}'."}
    u = ctypes.windll.user32
    try:
        sw = {"minimize": 6, "minimise": 6, "maximize": 3, "maximise": 3, "restore": 9}
        if action in sw:
            u.ShowWindow(hwnd, sw[action]); return {"ok": True, "action": action, "title": title}
        if action in ("always_on_top", "pin", "on_top"):
            u.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002)
            return {"ok": True, "action": "always_on_top", "title": title}
        if action in ("unpin", "not_on_top"):
            u.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0001 | 0x0002)
            return {"ok": True, "action": "unpin", "title": title}
        if action in ("snap_left", "snap_right"):
            sw_x = u.GetSystemMetrics(0); sh_y = u.GetSystemMetrics(1)
            u.ShowWindow(hwnd, 9)
            x = 0 if action == "snap_left" else sw_x // 2
            u.SetWindowPos(hwnd, 0, x, 0, sw_x // 2, sh_y, 0x0040)
            return {"ok": True, "action": action, "title": title}
        return {"error": f"Unknown window action '{action}'."}
    except Exception as e:
        return {"error": str(e)}


# ── Handy extras ────────────────────────────────────────────────────────────
def archive(op: str, path: str, dest: str = None) -> dict:
    op = (op or "").lower().strip()
    src = expand(path) if path else ""
    try:
        if op in ("zip", "compress"):
            if not os.path.exists(src):
                return {"error": f"Nothing at {src}."}
            out = expand(dest) if dest else src
            out = out[:-4] if out.lower().endswith(".zip") else out
            made = shutil.make_archive(out, "zip", root_dir=os.path.dirname(src),
                                       base_dir=os.path.basename(src))
            return {"ok": True, "op": "zip", "path": made}
        if op in ("unzip", "extract", "decompress"):
            if not os.path.isfile(src):
                return {"error": f"Not a zip file: {src}."}
            out = expand(dest) if dest else os.path.splitext(src)[0]
            os.makedirs(out, exist_ok=True)
            shutil.unpack_archive(src, out)
            return {"ok": True, "op": "unzip", "path": out}
        return {"error": f"Unknown archive op '{op}'."}
    except Exception as e:
        return {"error": str(e)}


def reveal_file(path: str) -> dict:
    full = expand(path)
    if not os.path.exists(full):
        return {"error": f"Nothing at {full}."}
    try:
        if sys.platform == "win32":
            subprocess.Popen(f'explorer /select,"{full}"', shell=True)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", full])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(full)])
        return {"ok": True, "path": full}
    except Exception as e:
        return {"error": str(e)}


def system_utility(action: str) -> dict:
    action = (action or "").lower().strip().replace(" ", "_")
    try:
        if action in ("restart_explorer", "restart_taskbar"):
            subprocess.run(["powershell", "-NoProfile", "-Command",
                            "Stop-Process -Name explorer -Force; Start-Sleep -Milliseconds 500; Start-Process explorer"],
                           timeout=20)
            return {"ok": True, "action": "restart_explorer"}
        if action in ("clear_temp", "clean_temp", "disk_cleanup"):
            tmp = os.environ.get("TEMP") or os.environ.get("TMP") or ""
            removed = 0
            if tmp and os.path.isdir(tmp):
                for entry in os.listdir(tmp):
                    p = os.path.join(tmp, entry)
                    try:
                        if os.path.isdir(p):
                            shutil.rmtree(p, ignore_errors=True)
                        else:
                            os.remove(p)
                        removed += 1
                    except Exception:
                        pass
            return {"ok": True, "action": "clear_temp", "removed": removed}
        if action in ("list_installed", "installed_programs", "programs"):
            r = subprocess.run(["powershell", "-NoProfile", "-Command",
                                "Get-ItemProperty HKLM:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\*, "
                                "HKLM:\\Software\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\* "
                                "| Where-Object { $_.DisplayName } | Select-Object -ExpandProperty DisplayName | Sort-Object -Unique"],
                               capture_output=True, text=True, timeout=25)
            progs = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
            return {"ok": True, "programs": progs}
        return {"error": f"Unknown utility '{action}'."}
    except Exception as e:
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════
#  Print & file utilities · Personalization · Image/PDF · Scheduling
# ══════════════════════════════════════════════════════════════════════════

def _suffix(path: str, suf: str) -> str:
    r, e = os.path.splitext(path)
    return r + suf + e


# ── Print & file utilities ──────────────────────────────────────────────────
def print_file(path: str) -> dict:
    full = expand(path)
    if not os.path.isfile(full):
        return {"error": f"Not a file: {full}."}
    try:
        if sys.platform == "win32":
            os.startfile(full, "print")            # type: ignore[attr-defined]
        else:
            subprocess.Popen(["lp", full])
        return {"ok": True, "path": full}
    except Exception as e:
        return {"error": str(e)}


def path_info(path: str) -> dict:
    full = expand(path)
    if not os.path.exists(full):
        return {"error": f"Nothing at {full}."}
    import datetime
    try:
        if os.path.isdir(full):
            total = files = dirs = 0
            for dp, dns, fns in os.walk(full):
                dirs += len(dns)
                for f in fns:
                    files += 1
                    try:
                        total += os.path.getsize(os.path.join(dp, f))
                    except Exception:
                        pass
            return {"ok": True, "kind": "folder", "path": full,
                    "size_mb": round(total / 1048576, 1), "files": files, "folders": dirs}
        st = os.stat(full)
        return {"ok": True, "kind": "file", "path": full,
                "size_mb": round(st.st_size / 1048576, 2),
                "modified": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                "ext": os.path.splitext(full)[1].lstrip(".") or "none"}
    except Exception as e:
        return {"error": str(e)}


def recent_files(folder: str = None, count: int = 10) -> dict:
    base = expand(folder) if folder else expand("Downloads")
    if not os.path.isdir(base):
        return {"error": f"Not a folder: {base}."}
    import datetime
    try:
        items = []
        for f in os.listdir(base):
            p = os.path.join(base, f)
            if os.path.isfile(p):
                items.append((os.path.basename(p), os.path.getmtime(p)))
        items.sort(key=lambda x: -x[1])
        return {"ok": True, "base": base,
                "files": [{"name": n, "when": datetime.datetime.fromtimestamp(m).strftime("%b %d %H:%M")}
                          for n, m in items[:count]]}
    except Exception as e:
        return {"error": str(e)}


def do_batch_rename(folder: str, prefix: str, ext: str = None) -> dict:
    base = expand(folder)
    if not os.path.isdir(base):
        return {"error": f"Not a folder: {base}."}
    if not (prefix or "").strip():
        return {"error": "Need a name prefix."}
    try:
        want = ("." + ext.lower().lstrip(".")) if ext else None
        files = sorted(f for f in os.listdir(base)
                       if os.path.isfile(os.path.join(base, f))
                       and (not want or f.lower().endswith(want)))
        for i, f in enumerate(files, 1):
            e = os.path.splitext(f)[1]
            os.rename(os.path.join(base, f), os.path.join(base, f"{prefix}_{i}{e}"))
        return {"ok": True, "count": len(files), "folder": base}
    except Exception as e:
        return {"error": str(e)}


_CRITICAL_PROCS = {"system", "registry", "memcompression", "svchost.exe", "explorer.exe",
                   "dwm.exe", "csrss.exe", "wininit.exe", "services.exe", "lsass.exe",
                   "winlogon.exe", "grace.exe", "python.exe", "pythonw.exe"}


def top_memory_process():
    import psutil
    best = None
    for p in psutil.process_iter(["name", "memory_info", "pid"]):
        try:
            nm = (p.info.get("name") or "").lower()
            if nm in _CRITICAL_PROCS:
                continue
            mem = p.info["memory_info"].rss if p.info.get("memory_info") else 0
            if best is None or mem > best[1]:
                best = (p.info.get("name"), mem, p.info.get("pid"))
        except Exception:
            pass
    return best


def close_process_pid(pid) -> dict:
    try:
        r = subprocess.run(f"taskkill /PID {int(pid)} /F", shell=True,
                           capture_output=True, text=True)
        return {"ok": r.returncode == 0, "detail": ((r.stdout or "") + (r.stderr or "")).strip()[:150]}
    except Exception as e:
        return {"error": str(e)}


# ── Personalization & settings ──────────────────────────────────────────────
def set_wallpaper(path: str) -> dict:
    full = expand(path)
    if not os.path.isfile(full):
        return {"error": f"Not a file: {full}."}
    try:
        ctypes.windll.user32.SystemParametersInfoW(0x0014, 0, full, 3)   # SPI_SETDESKWALLPAPER
        return {"ok": True, "path": full}
    except Exception as e:
        return {"error": str(e)}


def set_theme(mode: str) -> dict:
    dark = "dark" in (mode or "").lower()
    val = 0 if dark else 1
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
                0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "AppsUseLightTheme", 0, winreg.REG_DWORD, val)
            winreg.SetValueEx(k, "SystemUsesLightTheme", 0, winreg.REG_DWORD, val)
        return {"ok": True, "mode": "dark" if dark else "light"}
    except Exception as e:
        return {"error": str(e)}


def set_power_plan(mode: str) -> dict:
    m = (mode or "").lower()
    guids = {"balanced": "381b4222-f694-41f0-9685-ff5bb260df2e",
             "high": "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c",
             "performance": "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c",
             "saver": "a1841308-3541-4fab-bc81-f71556f20b4a",
             "power": "a1841308-3541-4fab-bc81-f71556f20b4a"}
    g = next((v for k, v in guids.items() if k in m), None)
    if not g:
        return {"error": "Use balanced | high performance | power saver."}
    r = subprocess.run(f"powercfg /setactive {g}", shell=True, capture_output=True, text=True)
    if r.returncode != 0:
        return {"error": f"Couldn't switch plan (it may be hidden on this PC). {(r.stderr or '').strip()[:120]}"}
    return {"ok": True, "mode": m}


def personalize(action: str, value: str = None) -> dict:
    a = (action or "").lower().strip().replace(" ", "_")
    if a in ("wallpaper", "background", "set_wallpaper"):
        return set_wallpaper(value or "")
    if a in ("dark_mode", "dark", "light_mode", "light", "theme"):
        return set_theme(a if a in ("dark", "light") else (value or a))
    if a in ("power_plan", "powerplan", "plan"):
        return set_power_plan(value or "")
    return {"error": f"Unknown personalize action '{action}'."}


# ── Image tools ─────────────────────────────────────────────────────────────
def image_op(op: str, path: str, dest: str = None, value=None) -> dict:
    op = (op or "").lower().strip()
    full = expand(path)
    if not os.path.isfile(full):
        return {"error": f"Not a file: {full}."}
    try:
        from PIL import Image
        img = Image.open(full)
        if op in ("resize", "scale"):
            v = str(value or "50")
            if "x" in v.lower():
                w, h = v.lower().split("x")
                img = img.resize((int(w), int(h)))
            else:
                pct = float(v.strip().rstrip("%")) / 100.0
                img = img.resize((max(1, int(img.width * pct)), max(1, int(img.height * pct))))
            out = expand(dest) if dest else _suffix(full, "_resized")
            img.save(out)
            return {"ok": True, "op": "resize", "path": out}
        if op in ("convert", "format"):
            fmt = (str(value).lstrip(".").lower() if value
                   else (os.path.splitext(dest)[1].lstrip(".").lower() if dest else "jpg"))
            out = expand(dest) if dest else os.path.splitext(full)[0] + "." + fmt
            if fmt in ("jpg", "jpeg") and img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            img.save(out)
            return {"ok": True, "op": "convert", "path": out}
        if op == "compress":
            out = expand(dest) if dest else _suffix(full, "_compressed")
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            img.save(out, quality=int(value or 60), optimize=True)
            return {"ok": True, "op": "compress", "path": out}
        return {"error": f"Unknown image op '{op}'."}
    except Exception as e:
        return {"error": str(e)}


# ── PDF tools ───────────────────────────────────────────────────────────────
def _parse_pages(spec: str, total: int):
    spec = (spec or "").strip()
    if not spec:
        return list(range(total))
    out = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            out += list(range(int(a) - 1, int(b)))
        elif part:
            out.append(int(part) - 1)
    return [i for i in out if 0 <= i < total]


def pdf_op(op: str, path: str, dest: str = None, pages: str = None, path2: str = None) -> dict:
    op = (op or "").lower().strip()
    try:
        from pypdf import PdfReader, PdfWriter
        if op == "merge":
            if path and "," in path:
                srcs = [expand(p.strip()) for p in path.split(",") if p.strip()]
            else:
                srcs = [expand(p) for p in [path, path2] if p]
            if len(srcs) < 2:
                return {"error": "Give me at least two PDFs to merge."}
            w = PdfWriter()
            for s in srcs:
                if not os.path.isfile(s):
                    return {"error": f"Not found: {s}."}
                for pg in PdfReader(s).pages:
                    w.add_page(pg)
            out = expand(dest) if dest else _suffix(srcs[0], "_merged")
            with open(out, "wb") as f:
                w.write(f)
            return {"ok": True, "op": "merge", "path": out, "count": len(srcs)}
        full = expand(path)
        if not os.path.isfile(full):
            return {"error": f"Not a file: {full}."}
        reader = PdfReader(full)
        if op in ("extract", "pages"):
            idxs = _parse_pages(pages, len(reader.pages))
            if not idxs:
                return {"error": "Which pages? e.g. '2-5' or '1,3,5'."}
            w = PdfWriter()
            for i in idxs:
                w.add_page(reader.pages[i])
            out = expand(dest) if dest else _suffix(full, "_pages")
            with open(out, "wb") as f:
                w.write(f)
            return {"ok": True, "op": "extract", "path": out, "count": len(idxs)}
        if op == "split":
            outdir = expand(dest) if dest else os.path.splitext(full)[0] + "_split"
            os.makedirs(outdir, exist_ok=True)
            for i, pg in enumerate(reader.pages, 1):
                w = PdfWriter()
                w.add_page(pg)
                with open(os.path.join(outdir, f"page_{i}.pdf"), "wb") as f:
                    w.write(f)
            return {"ok": True, "op": "split", "path": outdir, "count": len(reader.pages)}
        return {"error": f"Unknown pdf op '{op}'."}
    except Exception as e:
        return {"error": str(e)}


# ── Scheduling (Windows Task Scheduler) ─────────────────────────────────────
def schedule_task(action: str, name: str = None, command: str = None,
                  time: str = None, repeat: str = "once") -> dict:
    action = (action or "create").lower().strip()
    try:
        if action in ("list", "show"):
            r = subprocess.run('schtasks /query /fo LIST', shell=True,
                               capture_output=True, text=True)
            names = [n.strip() for n in _re.findall(r"TaskName:\s+\\Grace\\(.+)", r.stdout or "")]
            return {"ok": True, "tasks": names}
        if action in ("delete", "remove", "cancel"):
            if not name:
                return {"error": "Which task?"}
            tn = name if name.startswith("\\") else "\\Grace\\" + name
            r = subprocess.run(f'schtasks /delete /tn "{tn}" /f', shell=True,
                               capture_output=True, text=True)
            return {"ok": r.returncode == 0, "action": "delete", "name": tn}
        # create
        if not command or not time:
            return {"error": "Need a command and a time (HH:MM)."}
        tn = "\\Grace\\" + (name or f"task_{int(__import__('time').time())}")
        sc = "DAILY" if repeat and "da" in (repeat or "").lower() else "ONCE"
        r = subprocess.run(
            f'schtasks /create /tn "{tn}" /tr "{command}" /sc {sc} /st {time} /f',
            shell=True, capture_output=True, text=True)
        ok = r.returncode == 0
        return {"ok": ok, "action": "create", "name": tn, "time": time,
                "repeat": sc.lower(),
                "detail": None if ok else ((r.stdout or "") + (r.stderr or "")).strip()[:200]}
    except Exception as e:
        return {"error": str(e)}
