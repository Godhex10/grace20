# routers/logs.py
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel
from datetime import datetime

from database import get_db
import models

router = APIRouter(prefix="/api/logs", tags=["System Audit Logs"])

MAX_LIMIT = 100
DEFAULT_LIMIT = 20


class LogResponse(BaseModel):
    id: int
    timestamp: datetime
    user_input: str
    input_type: str
    grace_response: str
    response_modality: str
    triggered_widgets: str
    layout_context: str

    class Config:
        from_attributes = True


class LogListResponse(BaseModel):
    logs: List[LogResponse]
    total: int
    limit: int
    offset: int


@router.get("/", response_model=LogListResponse)
def get_interaction_audit_trail(
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    input_type: Optional[str] = Query(None),
    layout_context: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """
    Pulls a chronological history of system executions and vocal modal exchanges.
    """
    query = db.query(models.InteractionLog)
    
    if input_type:
        if input_type not in ("text", "voice"):
            raise HTTPException(status_code=400, detail="input_type must be 'text' or 'voice'")
        query = query.filter(models.InteractionLog.input_type == input_type)
    
    if layout_context:
        query = query.filter(models.InteractionLog.layout_context == layout_context)
    
    total = query.count()
    logs = query.order_by(models.InteractionLog.timestamp.desc()).offset(offset).limit(limit).all()
    
    return LogListResponse(logs=logs, total=total, limit=limit, offset=offset)