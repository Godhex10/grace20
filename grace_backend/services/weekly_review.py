# services/weekly_review.py
"""Weekly review — the accountability bookend to the daily briefing.

Gathers the past 7 days from the database (tasks completed, projects moved,
ideas captured, events, what's stalling) into a factual brief, then lets Grace
phrase it warmly in her own voice. Falls back to a plain recap if the LLM is
unavailable.
"""
import logging
from datetime import datetime, timedelta

from database import SessionLocal
from timeutils import utcnow
import models
from services import projects as project_service

logger = logging.getLogger(__name__)


def _gather(db):
    """Return a plain-text brief of the week's activity."""
    since_utc = utcnow() - timedelta(days=7)
    now_local = datetime.now()
    week_start = (now_local - timedelta(days=7)).strftime("%Y-%m-%d")
    today_key = now_local.strftime("%Y-%m-%d")

    done = (
        db.query(models.Task)
        .filter(models.Task.status == "done")
        .filter(models.Task.completed_at.isnot(None))
        .filter(models.Task.completed_at >= since_utc)
        .all()
    )
    active_projects = db.query(models.Project).filter(models.Project.status == "active").all()
    advanced = [
        p for p in active_projects
        if p.last_progress_at and p.last_progress_at >= since_utc
    ]
    stalling = project_service.stalled_projects(db)
    notes = db.query(models.Note).filter(models.Note.created_at >= since_utc).all()
    events = (
        db.query(models.CalendarEvent)
        .filter(models.CalendarEvent.date_key >= week_start)
        .filter(models.CalendarEvent.date_key <= today_key)
        .all()
    )

    lines = ["This past week (the last 7 days):"]
    if done:
        titles = ", ".join(f'"{t.title}"' for t in done[:8])
        lines.append(f"- Tasks completed: {len(done)} — {titles}")
    else:
        lines.append("- Tasks completed: 0")
    if advanced:
        lines.append("- Projects moved forward: " + ", ".join(p.name for p in advanced))
    if stalling:
        lines.append(
            "- Projects STALLING (no recent progress): "
            + ", ".join(f"{p.name} ({project_service.days_since_progress(p)}d)" for p in stalling)
        )
    if notes:
        by_cat = {}
        for n in notes:
            by_cat[n.category] = by_cat.get(n.category, 0) + 1
        cats = ", ".join(f"{v} {k}" for k, v in by_cat.items())
        lines.append(f"- Ideas/notes captured: {len(notes)} ({cats})")
    if events:
        lines.append("- Events: " + ", ".join(f'"{e.title}"' for e in events[:8]))

    has_activity = bool(done or advanced or notes or events)
    return "\n".join(lines), has_activity


async def compose_weekly_review(db=None) -> str:
    own = False
    if db is None:
        db = SessionLocal()
        own = True
    try:
        brief, has_activity = _gather(db)

        # Try to phrase it in her voice; fall back to the plain brief.
        try:
            from routers.router_pipeline import (
                grace_llm_client, GRACE_MODEL, genai_types, _build_memory_snapshot,
            )
            if grace_llm_client is not None:
                facts = _build_memory_snapshot(db)
                instruction = (
                    "You are Grace — a warm, sharp AI co-pilot (FRIDAY-style) who "
                    "calls him \"Boss\". Give him his WEEKLY REVIEW out loud, in your "
                    "own voice: 3-5 sentences, warm and honest. Celebrate the real "
                    "win, name what moved, flag the ONE thing that's slipping, and "
                    "end on an encouraging, genuine note. No bullet points, no "
                    "preamble. If the week was quiet, be kind about it, not harsh."
                    + facts
                )
                resp = await grace_llm_client.aio.models.generate_content(
                    model=GRACE_MODEL,
                    contents=[genai_types.Content(role="user", parts=[genai_types.Part(text=brief)])],
                    config=genai_types.GenerateContentConfig(
                        system_instruction=instruction, max_output_tokens=300,
                    ),
                )
                text = (resp.text or "").strip()
                if text:
                    return text
        except Exception as e:
            logger.warning(f"Weekly review phrasing failed, using plain brief: {e}")

        if not has_activity:
            return "Quiet week on the board, Boss — nothing logged. Fresh slate coming up."
        return "Here's your week, Boss:\n" + brief
    finally:
        if own:
            db.close()
