# routers/projects.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel

from database import get_db
import models
from services import projects as project_service
from core.sse import push_workspace_update
from timeutils import utcnow

router = APIRouter(prefix="/api/projects", tags=["Projects"])

VALID_STATUS = {"active", "paused", "done"}


def _serialize(p) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "next_action": p.next_action or "",
        "stall_risk": bool(p.stall_risk),
        "status": p.status,
        "days_since_progress": project_service.days_since_progress(p),
        "stalling": project_service.is_stalled(p),
        "last_progress_at": p.last_progress_at.isoformat() if p.last_progress_at else None,
    }


class ProjectCreate(BaseModel):
    name: str
    next_action: str = ""
    stall_risk: bool = False
    status: str = "active"


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    next_action: Optional[str] = None
    stall_risk: Optional[bool] = None
    status: Optional[str] = None


@router.get("/")
def list_projects(include_done: bool = False, db: Session = Depends(get_db)):
    q = db.query(models.Project)
    if not include_done:
        q = q.filter(models.Project.status != "done")
    rows = q.all()
    # Stalest first, then by name — mirrors how Grace prioritizes them.
    rows.sort(key=lambda p: (-project_service.days_since_progress(p), p.name.lower()))
    return [_serialize(p) for p in rows]


@router.post("/", status_code=201)
async def create_project(payload: ProjectCreate, db: Session = Depends(get_db)):
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Project name is required.")
    status = payload.status if payload.status in VALID_STATUS else "active"
    p = models.Project(
        name=name[:200],
        next_action=(payload.next_action or "")[:1000],
        stall_risk=bool(payload.stall_risk),
        status=status,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    await push_workspace_update("WIDGET_PROJECTS", {"action": "changed"})
    return _serialize(p)


@router.put("/{project_id}")
async def update_project(project_id: int, payload: ProjectUpdate, db: Session = Depends(get_db)):
    p = db.query(models.Project).filter(models.Project.id == project_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Project not found.")
    if payload.name is not None:
        p.name = payload.name.strip()[:200] or p.name
    if payload.next_action is not None:
        p.next_action = payload.next_action[:1000]
    if payload.stall_risk is not None:
        p.stall_risk = bool(payload.stall_risk)
    if payload.status is not None and payload.status in VALID_STATUS:
        p.status = payload.status
    db.commit()
    db.refresh(p)
    await push_workspace_update("WIDGET_PROJECTS", {"action": "changed"})
    return _serialize(p)


@router.post("/{project_id}/progress")
async def log_progress(project_id: int, db: Session = Depends(get_db)):
    """Reset a project's stall clock — the widget's 'I worked on this' button."""
    p = db.query(models.Project).filter(models.Project.id == project_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Project not found.")
    p.last_progress_at = utcnow()
    p.status = "active"
    db.commit()
    db.refresh(p)
    await push_workspace_update("WIDGET_PROJECTS", {"action": "changed"})
    return _serialize(p)


@router.delete("/{project_id}")
async def delete_project(project_id: int, db: Session = Depends(get_db)):
    p = db.query(models.Project).filter(models.Project.id == project_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Project not found.")
    db.delete(p)
    db.commit()
    await push_workspace_update("WIDGET_PROJECTS", {"action": "changed"})
    return {"status": "deleted", "id": project_id}
