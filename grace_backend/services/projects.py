# services/projects.py
"""Project momentum helpers — the anti-stall engine's read/analysis side.

Grace tracks each active project's next action and when progress last happened.
This module answers "how long has it been stalled?" and "which backend item is
parked the longest?", shared by the context snapshot (router_pipeline) and the
proactive nudge (reminders).
"""
import os

import models
from timeutils import utcnow

# A stall-prone (backend) item with no progress for this many days is "stalling".
try:
    STALL_DAYS = int(os.environ.get("GRACE_STALL_DAYS", "3"))
except ValueError:
    STALL_DAYS = 3


def days_since_progress(p) -> int:
    ref = p.last_progress_at or p.created_at or utcnow()
    return max(0, (utcnow() - ref).days)


def is_stalled(p, threshold=STALL_DAYS) -> bool:
    """True for an active, stall-prone project with no recent progress."""
    return (
        p.status == "active"
        and bool(p.stall_risk)
        and days_since_progress(p) >= threshold
    )


def stalled_projects(db, threshold=STALL_DAYS):
    """Active stall-prone projects past the threshold, stalest first."""
    active = db.query(models.Project).filter(models.Project.status == "active").all()
    stalled = [p for p in active if is_stalled(p, threshold)]
    stalled.sort(key=days_since_progress, reverse=True)
    return stalled


def snapshot_text(db) -> str:
    """A context block listing active projects and their momentum, so Grace can
    answer 'what's stalling?' and act on projects by id."""
    active = (
        db.query(models.Project)
        .filter(models.Project.status == "active")
        .order_by(models.Project.name)
        .all()
    )
    if not active:
        return ""
    lines = []
    for p in active:
        d = days_since_progress(p)
        na = f" — next: {p.next_action}" if p.next_action else ""
        risk = " [backend/stall-prone]" if p.stall_risk else ""
        flag = " ** STALLING **" if is_stalled(p) else ""
        lines.append(f"  - {p.name} (id {p.id}, {d}d since progress){na}{risk}{flag}")
    return (
        " Here are the operator's ACTIVE projects and their momentum (days since "
        "they last made progress). The operator has told you backend work is where "
        "his projects stall, so items flagged STALLING deserve a gentle push. When "
        "he says he worked on something, call log_progress to reset its clock:\n"
        + "\n".join(lines)
    )
