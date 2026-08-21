# routers/events.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel, field_validator
from datetime import datetime

from database import get_db
import models

router = APIRouter(prefix="/api/events", tags=["Calendar Events"])

VALID_PRIORITIES = {"hi", "med", "low"}


class EventCreate(BaseModel):
    date_key: str   # "YYYY-MM-DD"
    time: str = "00:00"
    title: str
    priority: str = "med"

    @field_validator("title")
    @classmethod
    def _title(cls, v):
        v = v.strip()
        if not v:
            raise ValueError("Title cannot be empty")
        return v[:500]

    @field_validator("priority")
    @classmethod
    def _priority(cls, v):
        v = v.lower()
        return v if v in VALID_PRIORITIES else "med"

    @field_validator("date_key")
    @classmethod
    def _date_key(cls, v):
        v = v.strip()
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("date_key must be YYYY-MM-DD")
        return v


class EventResponse(BaseModel):
    id: int
    date_key: str
    time: str
    title: str
    priority: str
    created_at: datetime

    class Config:
        from_attributes = True


@router.get("/", response_model=List[EventResponse])
def get_all_events(db: Session = Depends(get_db)):
    return db.query(models.CalendarEvent).order_by(
        models.CalendarEvent.date_key, models.CalendarEvent.time
    ).all()


@router.post("/", response_model=EventResponse, status_code=201)
def create_event(payload: EventCreate, db: Session = Depends(get_db)):
    ev = models.CalendarEvent(
        date_key=payload.date_key,
        time=payload.time,
        title=payload.title,
        priority=payload.priority,
    )
    db.add(ev)
    db.commit()
    db.refresh(ev)
    return ev


class EventUpdate(BaseModel):
    date_key: Optional[str] = None
    time: Optional[str] = None
    title: Optional[str] = None
    priority: Optional[str] = None

    @field_validator("priority")
    @classmethod
    def _priority(cls, v):
        if v is None: return v
        v = v.lower()
        return v if v in {"hi", "med", "low"} else "med"

    @field_validator("date_key")
    @classmethod
    def _date_key(cls, v):
        if v is None: return v
        try: datetime.strptime(v.strip(), "%Y-%m-%d")
        except ValueError: raise ValueError("date_key must be YYYY-MM-DD")
        return v.strip()


@router.put("/{event_id}", response_model=EventResponse)
def update_event(event_id: int, payload: EventUpdate, db: Session = Depends(get_db)):
    ev = db.query(models.CalendarEvent).filter(models.CalendarEvent.id == event_id).first()
    if not ev:
        raise HTTPException(status_code=404, detail="Event not found.")
    if payload.date_key is not None: ev.date_key = payload.date_key
    if payload.time     is not None: ev.time     = payload.time
    if payload.title    is not None: ev.title    = payload.title[:500]
    if payload.priority is not None: ev.priority = payload.priority
    db.commit(); db.refresh(ev)
    return ev


@router.delete("/{event_id}")
def delete_event(event_id: int, db: Session = Depends(get_db)):
    ev = db.query(models.CalendarEvent).filter(models.CalendarEvent.id == event_id).first()
    if not ev:
        raise HTTPException(status_code=404, detail="Event not found.")
    db.delete(ev)
    db.commit()
    return {"status": "deleted", "id": event_id}
