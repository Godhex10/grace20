# main.py
import asyncio
import datetime
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse

from services import appconfig
from database import engine
import models
import timeutils
from routers import workspace, router_pipeline, tasks, logs, map_proxy, events, weather, projects, voice, upload, code, documents, habits, google_actions, os_actions, setup
from services.audio import miso_voice
from services.reminders import reminder_service
from services.proactive import proactive_monitor


STATIC_DIR = os.path.join(appconfig.runtime_dir(), "static")   # writable (exe-safe)


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(os.path.join(STATIC_DIR, "audio"), exist_ok=True)
    try:
        models.Base.metadata.create_all(bind=engine)
    except Exception as e:
        print(f"Database initialization failed: {e}")
    await miso_voice.start_cleanup_task()
    await reminder_service.start()
    await proactive_monitor.start()      # desktop-only watchers (no-op on cloud)
    _kw = asyncio.create_task(router_pipeline.keep_warm_loop())  # kill cold-start lag
    yield
    _kw.cancel()
    await reminder_service.stop()
    await proactive_monitor.stop()


app = FastAPI(
    title="G.R.A.C.E. Core System",
    version="1.0.0",
    description="Genuinely Reliable Assistant for Command & Execution Local System Engine",
    lifespan=lifespan
)

ALLOWED_ORIGINS = [
    "http://127.0.0.1:8000",
    "http://localhost:8000",
    "http://127.0.0.1:5500",
    "http://localhost:5500",
]
# Add the hosted frontend origin(s) via .env, e.g.
#   ALLOWED_ORIGINS=https://grace.example.com,https://www.grace.example.com
ALLOWED_ORIGINS += [
    o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

os.makedirs(os.path.join(STATIC_DIR, "audio"), exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Serve the single-page frontend so the desktop app (and local dev) can load Grace
# same-origin at http://127.0.0.1:8000/. In cloud production Caddy serves these
# before a request ever reaches FastAPI, so these routes are only used locally.
# FRONTEND_DIR is the repo root in dev, or the PyInstaller bundle when frozen.
FRONTEND_DIR = appconfig.bundle_dir()


@app.get("/")
async def _serve_index():
    # First launch with no keys yet → send them to the setup screen.
    if appconfig.is_configured():
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
    return FileResponse(os.path.join(FRONTEND_DIR, "setup.html"))


@app.get("/app")
async def _serve_app():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


@app.get("/setup")
async def _serve_setup():
    return FileResponse(os.path.join(FRONTEND_DIR, "setup.html"))


@app.get("/styles.css")
async def _serve_styles():
    return FileResponse(os.path.join(FRONTEND_DIR, "styles.css"), media_type="text/css")

app.include_router(workspace.router)
app.include_router(router_pipeline.router)
app.include_router(tasks.router)
app.include_router(logs.router)
app.include_router(map_proxy.router)
app.include_router(events.router)
app.include_router(weather.router)
app.include_router(projects.router)
app.include_router(voice.router)
app.include_router(upload.router)
app.include_router(code.router)
app.include_router(documents.router)
app.include_router(habits.router)
app.include_router(google_actions.router)
app.include_router(os_actions.router)   # endpoints self-guard: desktop-only
app.include_router(setup.router)


from sqlalchemy import text

@app.get("/api/health")
async def health_check():
    db_status = "connected"
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        db_status = "disconnected"
    
    return {
        "status": "operational" if db_status == "connected" else "degraded",
        "engine": "GRACE_CORE_v1.0.0",
        "timestamp": timeutils.utcnow().isoformat(),
        "subsystems": {
            "sqlite_persistence": db_status,
            "routing_pipeline": "active",
            "sse_multiplexer": "active"
        }
    }


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "type": type(exc).__name__}
    )