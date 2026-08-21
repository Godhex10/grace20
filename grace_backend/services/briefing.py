# services/briefing.py
"""Daily briefing composer.

Builds a short, spoken-friendly rundown of the operator's day from live data —
the calendar, the task board, and the time of day. Deterministic (assembled
from the database, not the language model) so it's always accurate, and phrased
for the voice engine.
"""
from datetime import datetime

from database import SessionLocal
import models
from services import weather as weather_service
from services import habits as habit_service

# How the operator is addressed (matches Grace's persona).
ADDRESS = "Boss"


def _greeting_for(hour: int) -> str:
    if hour < 12:
        return "Good morning"
    if hour < 17:
        return "Good afternoon"
    return "Good evening"


def _fmt_time(t: str) -> str:
    """"14:30" -> "2:30 PM". Returns "" for an all-day / unparseable time."""
    t = (t or "").strip()
    if not t or t == "00:00":
        return ""
    try:
        return datetime.strptime(t, "%H:%M").strftime("%I:%M %p").lstrip("0")
    except ValueError:
        return ""


def _events_line(db, today_key: str) -> str:
    rows = (
        db.query(models.CalendarEvent)
        .filter(models.CalendarEvent.date_key == today_key)
        .order_by(models.CalendarEvent.time)
        .all()
    )
    if not rows:
        return "Your calendar is clear today."

    parts = []
    for e in rows:
        when = _fmt_time(e.time)
        parts.append(f"{e.title} at {when}" if when else f"{e.title}")

    count = len(parts)
    noun = "event" if count == 1 else "events"
    # Join naturally: "A at 9 AM, B at 2 PM, and C".
    if count == 1:
        listing = parts[0]
    elif count == 2:
        listing = f"{parts[0]} and {parts[1]}"
    else:
        listing = ", ".join(parts[:-1]) + f", and {parts[-1]}"
    return f"You have {count} {noun} today: {listing}."


def _tasks_line(db) -> str:
    pending = (
        db.query(models.Task)
        .filter(models.Task.status.in_(["todo", "inprog"]))
        .all()
    )
    if not pending:
        return "Your task board is clear — nothing pending."

    inprog = [t for t in pending if t.status == "inprog"]
    count = len(pending)
    noun = "task" if count == 1 else "tasks"
    line = f"You have {count} pending {noun}"

    # Name a few so the briefing is useful, leading with anything in progress.
    highlight = inprog + [t for t in pending if t.status != "inprog"]
    names = [t.title for t in highlight[:3]]
    if names:
        listing = names[0] if len(names) == 1 else (
            f"{names[0]} and {names[1]}" if len(names) == 2
            else ", ".join(names[:-1]) + f", and {names[-1]}"
        )
        if inprog:
            line += f", with {inprog[0].title} already in progress"
        line += f". Top of the list: {listing}."
    else:
        line += "."
    return line


def _habits_line(db) -> str:
    """A gentle habit nudge: what's still pending today, protecting any streaks."""
    try:
        pending = habit_service.pending_today(db)
    except Exception:
        return ""
    if not pending:
        # Only cheer if there are habits at all (all done).
        if db.query(models.Habit).filter(models.Habit.active == True).count():  # noqa: E712
            return "And you've already checked off every habit today — nice."
        return ""
    names = [h.name for h, _ in pending[:3]]
    listing = names[0] if len(names) == 1 else (
        f"{names[0]} and {names[1]}" if len(names) == 2
        else ", ".join(names[:-1]) + f", and {names[-1]}"
    )
    # Mention a streak worth protecting, if one stands out.
    top_h, top_streak = pending[0]
    streak_bit = (f" Don't break that {top_streak}-day {top_h.name} streak."
                  if top_streak >= 2 else "")
    return f"Still on your habit list today: {listing}.{streak_bit}"


def _mail_line() -> str:
    """A short 'new mail' nudge from Gmail (or '' if not connected)."""
    try:
        from services import google_integration as gi
        if not gi.is_connected():
            return ""
        d = gi.unread_emails(max_results=3)
        msgs = d.get("messages", []) if isinstance(d, dict) else []
        if "error" in d or not msgs:
            return ""
        senders = ", ".join(m["from"] for m in msgs[:3])
        nt = d.get("new_today", 0)
        if nt and nt > 0:
            return f"You've got around {nt} new email{'s' if nt != 1 else ''} since yesterday — latest from {senders}."
        return f"Recent unread mail from {senders}."
    except Exception:
        return ""


async def _weather_line() -> str:
    """A short live-weather sentence, or "" if the lookup fails (never blocks
    the rest of the briefing)."""
    try:
        w = await weather_service.get_weather()
    except Exception:
        return ""
    if not w or "error" in w:
        return ""
    return (
        f"It's {w['temp_c']}°C and {w['condition'].lower()} in {w['location']}."
    )


async def compose_briefing(db=None) -> str:
    """Return the full briefing text. Opens its own DB session if none given."""
    own = False
    if db is None:
        db = SessionLocal()
        own = True
    try:
        now = datetime.now()
        greeting = _greeting_for(now.hour)
        # Cross-platform day-of-month with no leading zero.
        date_str = f"{now.strftime('%A')}, {now.strftime('%B')} {now.day}"
        today_key = now.strftime("%Y-%m-%d")

        lead = f"{greeting}, {ADDRESS}. It's {date_str}."
        weather = await _weather_line()
        events = _events_line(db, today_key)
        tasks = _tasks_line(db)
        habits = _habits_line(db)
        mail = _mail_line()
        parts = [lead, weather, events, tasks, habits, mail]
        return " ".join(p for p in parts if p)
    finally:
        if own:
            db.close()
