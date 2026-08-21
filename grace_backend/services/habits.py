# services/habits.py
"""Habits & streaks — the read/analysis + write helpers.

A habit is checked off per LOCAL day (HabitCheckin.done_on = 'YYYY-MM-DD'). The
current streak is the run of consecutive days ending today (or yesterday, if
today isn't done yet) — so a streak stays "alive but at risk" until the day
actually lapses. longest_streak is cached on the Habit for cheap display.
"""
from datetime import date, timedelta

import models


def today_key() -> str:
    return date.today().isoformat()


def _key(d: date) -> str:
    return d.isoformat()


def _done_dates(db, habit_id) -> set:
    rows = db.query(models.HabitCheckin).filter(models.HabitCheckin.habit_id == habit_id).all()
    return {r.done_on for r in rows}


def done_today(db, habit) -> bool:
    return today_key() in _done_dates(db, habit.id)


def current_streak(db, habit, done=None) -> int:
    """Consecutive days ending today or yesterday. 0 if the streak has lapsed."""
    done = done if done is not None else _done_dates(db, habit.id)
    if not done:
        return 0
    today = date.today()
    cur = today
    if _key(cur) not in done:
        cur = today - timedelta(days=1)
        if _key(cur) not in done:
            return 0
    streak = 0
    while _key(cur) in done:
        streak += 1
        cur -= timedelta(days=1)
    return streak


def total_done(db, habit) -> int:
    return db.query(models.HabitCheckin).filter(models.HabitCheckin.habit_id == habit.id).count()


def serialize(db, habit) -> dict:
    done = _done_dates(db, habit.id)
    streak = current_streak(db, habit, done)
    # Keep the cached best up to date opportunistically.
    if streak > (habit.longest_streak or 0):
        habit.longest_streak = streak
        db.commit()
    return {
        "id": habit.id,
        "name": habit.name,
        "icon": habit.icon or "",
        "cadence": habit.cadence or "daily",
        "current_streak": streak,
        "longest_streak": habit.longest_streak or 0,
        "done_today": today_key() in done,
        "total_done": len(done),
    }


def list_habits(db, include_inactive=False):
    q = db.query(models.Habit)
    if not include_inactive:
        q = q.filter(models.Habit.active == True)  # noqa: E712
    rows = q.all()
    data = [serialize(db, h) for h in rows]
    # Not-done-today first (so pending ones surface), then longest streak.
    data.sort(key=lambda d: (d["done_today"], -d["current_streak"], d["name"].lower()))
    return data


def find(db, name_or_id):
    """Match a habit by id or name (case-insensitive, exact then unique partial)."""
    if name_or_id is None:
        return None
    s = str(name_or_id).strip()
    if not s:
        return None
    if s.isdigit():
        h = db.query(models.Habit).filter(models.Habit.id == int(s)).first()
        if h:
            return h
    low = s.lower()
    rows = db.query(models.Habit).filter(models.Habit.active == True).all()  # noqa: E712
    exact = [h for h in rows if (h.name or "").strip().lower() == low]
    if exact:
        return exact[0]
    partial = [h for h in rows if low in (h.name or "").strip().lower()]
    return partial[0] if len(partial) == 1 else None


def add_habit(db, name, cadence="daily", icon=""):
    name = (name or "").strip()
    if not name:
        return None
    h = models.Habit(name=name[:120], cadence=(cadence or "daily"), icon=(icon or "")[:8], active=True)
    db.add(h)
    db.commit()
    db.refresh(h)
    return h


def check_in(db, habit, on=None):
    """Mark the habit done for a day (default today). Idempotent. Returns the new
    current streak."""
    key = on or today_key()
    exists = (
        db.query(models.HabitCheckin)
        .filter(models.HabitCheckin.habit_id == habit.id, models.HabitCheckin.done_on == key)
        .first()
    )
    if not exists:
        db.add(models.HabitCheckin(habit_id=habit.id, done_on=key))
        db.commit()
    streak = current_streak(db, habit)
    if streak > (habit.longest_streak or 0):
        habit.longest_streak = streak
        db.commit()
    return streak


def undo_today(db, habit):
    """Remove today's check-in (un-check)."""
    row = (
        db.query(models.HabitCheckin)
        .filter(models.HabitCheckin.habit_id == habit.id, models.HabitCheckin.done_on == today_key())
        .first()
    )
    if row:
        db.delete(row)
        db.commit()
    return current_streak(db, habit)


def toggle_today(db, habit) -> bool:
    """Flip today's done state. Returns True if now done, False if now un-done."""
    if done_today(db, habit):
        undo_today(db, habit)
        return False
    check_in(db, habit)
    return True


def delete_habit(db, habit):
    db.query(models.HabitCheckin).filter(models.HabitCheckin.habit_id == habit.id).delete()
    db.delete(habit)
    db.commit()


def pending_today(db):
    """Active habits not yet checked off today, most-at-risk (longest live streak)
    first — for the briefing nudge."""
    out = []
    for h in db.query(models.Habit).filter(models.Habit.active == True).all():  # noqa: E712
        done = _done_dates(db, h.id)
        if today_key() not in done:
            out.append((h, current_streak(db, h, done)))
    out.sort(key=lambda t: -t[1])
    return out


def snapshot_text(db) -> str:
    """Context block so Grace knows today's habits, their streaks, and what's
    still pending — to answer 'how's my streak' and nudge naturally."""
    rows = list_habits(db)
    if not rows:
        return ""
    lines = []
    for r in rows:
        mark = "✓ done today" if r["done_today"] else "◻ not done yet"
        streak = f"{r['current_streak']}d streak" if r["current_streak"] else "no active streak"
        lines.append(f"  - {r['name']} (id {r['id']}): {mark}, {streak} (best {r['longest_streak']}d)")
    pending = [r["name"] for r in rows if not r["done_today"]]
    tail = ""
    if pending:
        tail = (f" Still pending today: {', '.join(pending)}. If it fits the moment, give a "
                "light nudge — never nag.")
    return (
        " Here are the operator's HABITS and streaks (a check-in is one local day). "
        "When he says he did one, call track_habit with action 'done' to log it and "
        "keep the streak alive; when he wants to start or stop tracking one, use "
        "'add'/'delete'.\n" + "\n".join(lines) + tail
    )
