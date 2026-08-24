"""G.R.A.C.E. desktop app entry point.

Runs the FastAPI backend locally and opens Grace in a native window (PyWebView /
Edge WebView2 on Windows). Setting GRACE_DESKTOP=1 unlocks the OS-control tools
(open files/folders, edit files, run commands) — these exist ONLY here, never on
the cloud server.

Works both as `python desktop.py` (dev) and as the packaged Grace.exe.
Set GRACE_SERVER_ONLY=1 to run the backend without a window (used for testing).
"""
import os
import sys
import time
import socket
import threading

# In a windowed (no-console) build, stdout/stderr are None — guard so any stray
# print()/logging can't crash the app.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

# ── config + paths (must happen before importing the app / database) ─────────
# Make the script path absolute BEFORE we change directory — otherwise PyWebView
# resolves a relative sys.argv[0] against the new CWD and doubles the path.
sys.argv[0] = os.path.abspath(sys.argv[0])
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from services import appconfig

appconfig.load()                              # keys from %APPDATA%\Grace\.env (or local .env)
os.environ.setdefault("GRACE_DESKTOP", "1")   # enable local OS-control tools
os.chdir(appconfig.runtime_dir())             # writable CWD (exe-safe)

HOST = "127.0.0.1"
BASE_PORT = int(os.environ.get("GRACE_PORT", "8000"))


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection((HOST, port), timeout=0.4):
            return True
    except OSError:
        return False


def _serves_frontend(port: int) -> bool:
    try:
        import httpx
        r = httpx.get(f"http://{HOST}:{port}/api/health", timeout=1.5)
        return r.status_code == 200
    except Exception:
        return False


def _serve(port: int):
    """Run uvicorn in a background thread (signal handlers off — not main thread)."""
    import uvicorn
    from main import app                        # import AFTER appconfig.load()

    class _Server(uvicorn.Server):
        def install_signal_handlers(self):
            pass

    uvicorn.Server(uvicorn.Config(app, host=HOST, port=port, log_level="warning")).run()


def _resolve_port():
    if _port_open(BASE_PORT):
        if _serves_frontend(BASE_PORT):
            return BASE_PORT, False             # reuse a Grace already running
        for p in range(BASE_PORT + 1, BASE_PORT + 30):
            if not _port_open(p):
                return p, True
    return BASE_PORT, True


def main():
    port, need_start = _resolve_port()
    if need_start:
        threading.Thread(target=_serve, args=(port,), daemon=True).start()
        for _ in range(400):                    # up to ~40s (first frozen boot is slower)
            if _serves_frontend(port):
                break
            time.sleep(0.1)
        else:
            print("Grace backend failed to start.", file=sys.stderr)
            sys.exit(1)

    # Headless mode for testing the packaged backend without a GUI.
    if os.environ.get("GRACE_SERVER_ONLY") == "1":
        print(f"GRACE_SERVER_READY http://{HOST}:{port}/", flush=True)
        while True:
            time.sleep(1)

    import webview
    webview.create_window(
        "Grace",
        f"http://{HOST}:{port}/",               # "/" routes to setup on first run
        width=1440, height=920, min_size=(1024, 680),
    )
    webview.start()


if __name__ == "__main__":
    main()
