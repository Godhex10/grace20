# services/reminders.py
"""Proactive reminder engine.

Runs as a background asyncio loop for the life of the app. Every CHECK_INTERVAL
seconds it scans the calendar (and any tasks with a due date) and, when
something is coming up soon, pushes a spoken heads-up to the chat widget — so
Grace speaks up on her own instead of only answering when asked.

State is kept in-memory: a set of keys for reminders already fired, so a given
event is announced once, not every tick. This resets when the server restarts,
which is fine for a local single-user dashboard.
"""
import asyncio
import logging
from datetime import datetime, timedelta

import os

from database import SessionLocal
import models
from core.sse import push_workspace_update, subscriber_count
from services.audio import miso_voice
from services.briefing import compose_briefing
from services.weekly_review import compose_weekly_review
from services import projects as project_service
from services import conversation_memory

logger = logging.getLogger(__name__)

# How often to scan for upcoming items.
CHECK_INTERVAL_SECONDS = 60
# Announce a timed item once it's within this many minutes of starting.
LEAD_MINUTES = 15
# Hour of day (0-23, local) at/after which the once-daily briefing is delivered
# the first time the operator is connected. Override with GRACE_BRIEFING_HOUR.
try:
    BRIEFING_HOUR = int(os.environ.get("GRACE_BRIEFING_HOUR", "9"))
except ValueError:
    BRIEFING_HOUR = 9

# Hour at/after which the once-daily anti-stall nudge fires (separate from the
# briefing so it lands as its own mid-day momentum check). GRACE_STALL_NUDGE_HOUR.
try:
    STALL_NUDGE_HOUR = int(os.environ.get("GRACE_STALL_NUDGE_HOUR", "14"))
except ValueError:
    STALL_NUDGE_HOUR = 14

# Hour at/after which Grace's warm, unprompted daily check-in fires. Evening by
# default (he's usually winding down). GRACE_CHECKIN_HOUR to override.
try:
    CHECKIN_HOUR = int(os.environ.get("GRACE_CHECKIN_HOUR", "18"))
except ValueError:
    CHECKIN_HOUR = 18

# Weekly review: weekday (0=Mon … 6=Sun) and hour it fires. Sunday evening.
try:
    WEEKLY_REVIEW_DAY = int(os.environ.get("GRACE_WEEKLY_REVIEW_DAY", "6"))
except ValueError:
    WEEKLY_REVIEW_DAY = 6
try:
    WEEKLY_REVIEW_HOUR = int(os.environ.get("GRACE_WEEKLY_REVIEW_HOUR", "18"))
except ValueError:
    WEEKLY_REVIEW_HOUR = 18
# Events stored with time "00:00" are treated as all-day — no precise start, so
# they get a single "on your calendar today" heads-up instead of a countdown.
ALL_DAY_TIME = "00:00"


class ReminderService:
    def __init__(self):
        self._task = None
        # Keys of reminders already delivered this process, e.g. "event:4:soon"
        # or "event:4:allday:2026-07-26". Prevents repeat announcements.
        self._fired = set()
        # Date (date object) the daily briefing was last delivered, so it fires
        # at most once per calendar day.
        self._last_briefing_date = None
        # Same idea for the anti-stall nudge.
        self._last_nudge_date = None
        # …and the daily warm check-in.
        self._last_checkin_date = None
        # …and the weekly review (fires once on its configured day).
        self._last_weekly_date = None

    async def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._run())
            logger.info("[Reminders] Proactive reminder loop started.")

    async def stop(self):
        if self._task:
            self._task.cancel()
            self._task = None

    async def _run(self):
        # Small initial delay so startup work (and the first client connecting)
        # settles before the first scan.
        await asyncio.sleep(10)
        while True:
            try:
                await self._scan_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[Reminders] Scan failed: {e}", exc_info=True)
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

    async def _scan_once(self):
        # No point announcing to an empty room — nobody is connected to hear it,
        # and we'd burn the one-shot 'fired' flag before the user is even back.
        if subscriber_count() == 0:
            return

        now = datetime.now()
        today = now.date()

        # One DB session for the whole tick — the briefing, the stall nudge, and
        # the event/task sweep all share it instead of each opening their own.
        db = SessionLocal()
        try:
            # Once-daily briefing: the first scan on/after BRIEFING_HOUR each day.
            if now.hour >= BRIEFING_HOUR and self._last_briefing_date != today:
                self._last_briefing_date = today
                try:
                    await self._announce(await compose_briefing(db))
                except Exception as e:
                    logger.error(f"[Reminders] Briefing failed: {e}", exc_info=True)

            # Once-daily anti-stall nudge: the stalest backend item gets a push,
            # so a parked project doesn't quietly rot (his known failure mode).
            if now.hour >= STALL_NUDGE_HOUR and self._last_nudge_date != today:
                self._last_nudge_date = today
                try:
                    msg = self._stall_nudge_message(db)
                    if msg:
                        await self._announce(msg)
                except Exception as e:
                    logger.error(f"[Reminders] Stall nudge failed: {e}", exc_info=True)

            # Once-daily warm check-in — Grace reaching out first, like a friend.
            if now.hour >= CHECKIN_HOUR and self._last_checkin_date != today:
                self._last_checkin_date = today
                try:
                    msg = await conversation_memory.generate_checkin(db)
                    if msg:
                        await self._announce(msg)
                except Exception as e:
                    logger.error(f"[Reminders] Check-in failed: {e}", exc_info=True)

            # Weekly review — the accountability bookend, once on its day.
            if (now.weekday() == WEEKLY_REVIEW_DAY and now.hour >= WEEKLY_REVIEW_HOUR
                    and self._last_weekly_date != today):
                self._last_weekly_date = today
                try:
                    await self._announce(await compose_weekly_review(db))
                except Exception as e:
                    logger.error(f"[Reminders] Weekly review failed: {e}", exc_info=True)

            # User-set reminders & timers that have come due. One-shot: marked
            # fired in the DB, so no in-memory dedup needed.
            fired_messages = []
            now_due = (
                db.query(models.Reminder)
                .filter(models.Reminder.fired == False)  # noqa: E712
                .filter(models.Reminder.fire_at <= now)
                .all()
            )
            for r in now_due:
                r.fired = True
                if r.kind == "timer":
                    lbl = (r.text or "").strip().lower()
                    fired_messages.append(
                        "Boss — your timer's up."
                        if lbl in ("", "timer", "focus timer")
                        else f"Boss — your {r.text} is up."
                    )
                else:
                    fired_messages.append(f"Boss, quick reminder: {r.text}.")
            if now_due:
                db.commit()

            # Timed event / due-task reminders.
            due = []  # (key, message) to announce this tick
            for ev in db.query(models.CalendarEvent).all():
                item = self._event_reminder(ev, now)
                if item:
                    due.append(item)
            for t in db.query(models.Task).filter(models.Task.due_date.isnot(None)).all():
                item = self._task_reminder(t, now)
                if item:
                    due.append(item)
        finally:
            db.close()

        for message in fired_messages:
            await self._announce(message)
        for key, message in due:
            self._fired.add(key)
            await self._announce(message)

    def _stall_nudge_message(self, db):
        """Return a nudge about the stalest at-risk project, or None if nothing
        is stalling. Uses the caller's DB session."""
        stalled = project_service.stalled_projects(db)
        if not stalled:
            return None
        p = stalled[0]
        days = project_service.days_since_progress(p)
        action = f" The next step is {p.next_action}." if p.next_action else ""
        return (
            f"Boss, a momentum check: \"{p.name}\" hasn't moved in {days} days, "
            f"and it's a backend item — usually where things stall for you."
            f"{action} Want to make it today's one backend win?"
        )

    def _event_reminder(self, ev, now):
        """Return (key, message) if this event should be announced now, else None."""
        time_str = (ev.time or ALL_DAY_TIME).strip() or ALL_DAY_TIME

        # All-day event: one heads-up on the morning of the day it lands.
        if time_str == ALL_DAY_TIME:
            if ev.date_key == now.strftime("%Y-%m-%d"):
                key = f"event:{ev.id}:allday:{ev.date_key}"
                if key not in self._fired:
                    return key, f"Heads up — you have \"{ev.title}\" on your calendar today."
            return None

        start = self._parse_dt(ev.date_key, time_str)
        if start is None:
            return None
        return self._countdown_reminder(
            entity="event", ident=ev.id, title=ev.title, start=start, now=now
        )

    def _task_reminder(self, t, now):
        """Return (key, message) if this task's due_date is imminent, else None."""
        if not t.due_date:
            return None
        return self._countdown_reminder(
            entity="task", ident=t.id, title=t.title, start=t.due_date, now=now,
            noun="is due",
        )

    def _countdown_reminder(self, entity, ident, title, start, now, noun="starts"):
        """Shared logic: fire once when `start` is within the lead window."""
        minutes_until = (start - now).total_seconds() / 60.0
        # Only within [now, now + LEAD]; a tiny negative slack (-1) covers an
        # item that ticked just past its start between scans.
        if -1 <= minutes_until <= LEAD_MINUTES:
            key = f"{entity}:{ident}:soon"
            if key in self._fired:
                return None
            if minutes_until <= 1:
                message = f"Heads up — \"{title}\" {noun} now."
            else:
                mins = int(round(minutes_until))
                message = f"Heads up — \"{title}\" {noun} in {mins} minutes."
            return key, message
        return None

    @staticmethod
    def _parse_dt(date_key, time_str):
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(f"{date_key} {time_str}", fmt)
            except ValueError:
                continue
        return None

    async def _announce(self, message):
        """Push a proactive spoken message to the chat widget."""
        logger.info(f"[Reminders] Announcing: {message}")
        audio_url = ""
        try:
            audio_url = await miso_voice.generate_speech(message)
        except Exception as e:
            logger.error(f"[Reminders] TTS failed: {e}", exc_info=True)
        await push_workspace_update(
            target_widget="WIDGET_CHAT",
            payload={"message": message},
            modality="hybrid" if audio_url else "widget_only",
            audio_url=audio_url,
        )


reminder_service = ReminderService()
