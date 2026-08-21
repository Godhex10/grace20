"""Config + path resolution that works both in dev and as a packaged .exe.

Packaged (PyInstaller) specifics:
- bundled read-only files (the frontend) live in sys._MEIPASS
- writable runtime files (TTS cache, a fallback SQLite db) go under %APPDATA%\\Grace\\runtime
- the operator's keys live in %APPDATA%\\Grace\\.env, entered once via the first-run
  setup screen — never baked into the binary.

In dev nothing changes: paths resolve to the repo as before.
"""
import os
import sys

from dotenv import load_dotenv, dotenv_values

APP_NAME = "Grace"
_FROZEN = bool(getattr(sys, "frozen", False))
_HERE = os.path.dirname(os.path.abspath(__file__))          # .../grace_backend/services


def _grace_backend_dir() -> str:
    return os.path.dirname(_HERE)                            # .../grace_backend


def bundle_dir() -> str:
    """Read-only bundled resources (index.html, styles.css, setup.html)."""
    if _FROZEN:
        return getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    return os.path.dirname(_grace_backend_dir())             # repo root (dev)


def config_dir() -> str:
    """Persistent per-user config (keys, google token). %APPDATA%\\Grace when frozen."""
    if _FROZEN:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        d = os.path.join(base, APP_NAME)
    else:
        d = _grace_backend_dir()
    os.makedirs(d, exist_ok=True)
    return d


def runtime_dir() -> str:
    """Writable working dir (static/audio cache, fallback SQLite). Becomes CWD."""
    d = os.path.join(config_dir(), "runtime") if _FROZEN else _grace_backend_dir()
    os.makedirs(d, exist_ok=True)
    return d


def config_env_path() -> str:
    return os.path.join(config_dir(), ".env")


# Keys the first-run setup screen collects. (label, required)
FIELDS = [
    ("GEMINI_API_KEY",      "Google Gemini API key — her brain (required)",        True),
    ("DATABASE_URL",        "Supabase URL — shares your data across machines",     False),
    ("AZURE_SPEECH_KEY",    "Azure Speech key — fast voice",                       False),
    ("AZURE_SPEECH_REGION", "Azure Speech region (e.g. germanywestcentral)",       False),
    ("TAVILY_API_KEY",      "Tavily key — live web search",                        False),
    ("TOMTOM_API_KEY",      "TomTom key — traffic + maps",                         False),
    ("SHODAN_API_KEY",      "Shodan key — host lookups",                           False),
    ("GOOGLE_CLIENT_ID",    "Google OAuth client id — Gmail/Calendar",             False),
    ("GOOGLE_CLIENT_SECRET","Google OAuth client secret",                          False),
]


def load() -> None:
    """Populate os.environ from the per-user .env first (packaged), then a local
    .env (dev) as fallback. Call this BEFORE importing anything that reads env."""
    p = config_env_path()
    if os.path.exists(p):
        load_dotenv(p, override=False)
    load_dotenv(override=False)   # a .env in CWD / next to the exe, if any


def is_configured() -> bool:
    return bool((os.environ.get("GEMINI_API_KEY") or "").strip())


def current_values() -> dict:
    p = config_env_path()
    saved = dotenv_values(p) if os.path.exists(p) else {}
    return {k: (saved.get(k) or os.environ.get(k) or "") for k, _, _ in FIELDS}


def save(values: dict) -> str:
    """Merge + persist the given keys to the per-user .env and apply to this process."""
    p = config_env_path()
    merged = dict(dotenv_values(p)) if os.path.exists(p) else {}
    for k, _, _ in FIELDS:
        v = (values.get(k) or "").strip()
        if v:
            merged[k] = v
    with open(p, "w", encoding="utf-8") as f:
        for k, v in merged.items():
            if v:
                f.write(f"{k}={v}\n")
    for k, v in merged.items():
        if v:
            os.environ[k] = v
    return p
