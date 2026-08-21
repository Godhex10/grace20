# routers/tasks.py
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel, field_validator
from datetime import datetime

from database import get_db
import models
from timeutils import utcnow

router = APIRouter(prefix="/api/tasks", tags=["Kanban Operations"])

VALID_STATUSES = {"todo", "inprog", "done"}
VALID_PRIORITIES = {"high", "med", "low"}
MAX_LIMIT = 100
DEFAULT_LIMIT = 50


class TaskCreate(BaseModel):
    title: str
    description: str = ""
    priority: str = "med"
    tag: str = "MISC"
    status: str = "todo"
    checklist: List[str] = []

    @field_validator("title")
    @classmethod
    def validate_title(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Task title cannot be empty")
        if len(v) > 500:
            raise ValueError("Task title too long (max 500 chars)")
        return v

    @field_validator("priority")
    @classmethod
    def validate_priority(cls, v: str) -> str:
        v = v.lower()
        if v not in VALID_PRIORITIES:
            raise ValueError(f"Priority must be one of {VALID_PRIORITIES}")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        v = v.lower()
        if v not in VALID_STATUSES:
            raise ValueError(f"Status must be one of {VALID_STATUSES}")
        return v

    @field_validator("tag")
    @classmethod
    def validate_tag(cls, v: str) -> str:
        v = v.strip().upper()
        if len(v) > 50:
            raise ValueError("Tag too long (max 50 chars)")
        return v or "MISC"


class TaskUpdate(BaseModel):
    status: Optional[str] = None
    checklist_done: Optional[List[bool]] = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        if v is None:
            return v
        v = v.lower()
        if v not in VALID_STATUSES:
            raise ValueError(f"Status must be one of {VALID_STATUSES}")
        return v


class TaskResponse(BaseModel):
    id: int
    title: str
    description: str = ""
    priority: str = "med"
    tag: str = "MISC"
    status: str
    checklist: List[str] = []
    checklist_done: List[bool] = []
    created_at: datetime
    due_date: Optional[datetime] = None

    # Legacy rows created before these columns existed store NULL. Coerce those
    # to the sensible defaults instead of failing validation.
    @field_validator("description", mode="before")
    @classmethod
    def _desc_default(cls, v):
        return v or ""

    @field_validator("priority", mode="before")
    @classmethod
    def _prio_default(cls, v):
        return v or "med"

    @field_validator("tag", mode="before")
    @classmethod
    def _tag_default(cls, v):
        return v or "MISC"

    @field_validator("checklist", mode="before")
    @classmethod
    def _checklist_default(cls, v):
        return v or []

    @field_validator("checklist_done", mode="before")
    @classmethod
    def _checklist_done_default(cls, v):
        return v or []

    class Config:
        from_attributes = True


class TaskListResponse(BaseModel):
    tasks: List[TaskResponse]
    total: int
    limit: int
    offset: int


@router.get("/", response_model=TaskListResponse)
def get_all_tasks(
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    status: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    query = db.query(models.Task)
    
    if status:
        if status.lower() not in VALID_STATUSES:
            raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of {VALID_STATUSES}")
        query = query.filter(models.Task.status == status.lower())
    
    total = query.count()
    tasks = query.order_by(models.Task.created_at.desc()).offset(offset).limit(limit).all()
    
    return TaskListResponse(tasks=tasks, total=total, limit=limit, offset=offset)


@router.post("/", response_model=TaskResponse, status_code=201)
def create_new_task(payload: TaskCreate, db: Session = Depends(get_db)):
    new_task = models.Task(
        title=payload.title,
        description=payload.description,
        priority=payload.priority,
        tag=payload.tag,
        status=payload.status,
        checklist=payload.checklist,
        checklist_done=[False] * len(payload.checklist),
    )
    db.add(new_task)
    db.commit()
    db.refresh(new_task)
    return new_task


@router.put("/{task_id}", response_model=TaskResponse)
def update_task_status(task_id: int, payload: TaskUpdate, db: Session = Depends(get_db)):
    task = db.query(models.Task).filter(models.Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")

    if payload.status is not None:
        # Stamp completion time when it first enters Done; clear if it leaves.
        if payload.status == "done" and task.status != "done":
            task.completed_at = utcnow()
        elif payload.status != "done":
            task.completed_at = None
        task.status = payload.status
    if payload.checklist_done is not None:
        task.checklist_done = payload.checklist_done
    db.commit()
    db.refresh(task)
    return task


@router.delete("/{task_id}")
def delete_task(task_id: int, db: Session = Depends(get_db)):
    task = db.query(models.Task).filter(models.Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")
    
    db.delete(task)
    db.commit()
    return {"status": "deleted", "id": task_id}