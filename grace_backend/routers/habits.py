# routers/habits.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel

from database import get_db
import models
from services import habits as habit_service
from core.sse import push_workspace_update

router = APIRouter(prefix="/api/habits", tags=["Habits"])


class HabitCreate(BaseModel):
    name: str
    cadence: str = "daily"
    icon: str = ""


@router.get("/")
def list_habits(db: Session = Depends(get_db)):
    return habit_service.list_habits(db)


@router.post("/", status_code=201)
async def create_habit(payload: HabitCreate, db: Session = Depends(get_db)):
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Habit name is required.")
    h = habit_service.add_habit(db, name, payload.cadence, payload.icon)
    await push_workspace_update("WIDGET_HABITS", {"action": "changed"})
    return habit_service.serialize(db, h)


@router.post("/{habit_id}/toggle")
async def toggle_habit(habit_id: int, db: Session = Depends(get_db)):
    """Flip today's done state — the widget's check circle."""
    h = db.query(models.Habit).filter(models.Habit.id == habit_id).first()
    if not h:
        raise HTTPException(status_code=404, detail="Habit not found.")
    now_done = habit_service.toggle_today(db, h)
    await push_workspace_update("WIDGET_HABITS", {"action": "changed"})
    out = habit_service.serialize(db, h)
    out["now_done"] = now_done
    return out


@router.delete("/{habit_id}")
async def delete_habit(habit_id: int, db: Session = Depends(get_db)):
    h = db.query(models.Habit).filter(models.Habit.id == habit_id).first()
    if not h:
        raise HTTPException(status_code=404, detail="Habit not found.")
    habit_service.delete_habit(db, h)
    await push_workspace_update("WIDGET_HABITS", {"action": "changed"})
    return {"status": "deleted", "id": habit_id}
