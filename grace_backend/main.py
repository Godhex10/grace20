# main.py
import datetime
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse

from database import engine
import models
import timeutils
from routers import workspace, router_pipeline, tasks, logs, map_proxy, events, weather, projects, voice, upload, code, documents, habits, google_actions
from services.audio import miso_voice
from services.reminders import reminder_service


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs("static/audio", exist_ok=True)
    try:
        models.Base.metadata.create_all(bind=engine)
    except Exception as e:
        print(f"Database initialization failed: {e}")
    await miso_voice.start_cleanup_task()
    await reminder_service.start()
    yield
    await reminder_service.stop()


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

app.mount("/static", StaticFiles(directory="static"), name="static")

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