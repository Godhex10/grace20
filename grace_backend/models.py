import datetime
from sqlalchemy import Column, Integer, Text, DateTime, String, CheckConstraint, JSON, Boolean
from database import Base
from timeutils import utcnow

class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(Text, nullable=False)
    description = Column(Text, nullable=True)
    priority = Column(String, default="med")
    tag = Column(String, default="MISC")
    status = Column(String, CheckConstraint("status IN ('todo', 'inprog', 'done')"), default="todo")
    checklist = Column(JSON, default=list)
    checklist_done = Column(JSON, default=list)  # parallel array of booleans
    created_at = Column(DateTime, default=utcnow)
    due_date = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)  # set when it moves to Done

class CalendarEvent(Base):
    __tablename__ = "calendar_events"

    id = Column(Integer, primary_key=True, index=True)
    date_key = Column(String, nullable=False, index=True)  # "YYYY-MM-DD"
    time = Column(String, default="00:00")                 # "HH:MM"
    title = Column(Text, nullable=False)
    priority = Column(String, default="med")               # hi | med | low
    created_at = Column(DateTime, default=utcnow)

class InteractionLog(Base):
    __tablename__ = "interaction_log"

    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, default=utcnow)
    user_input = Column(Text, nullable=False)
    input_type = Column(String, CheckConstraint("input_type IN ('text', 'voice')"), nullable=False)
    grace_response = Column(Text, nullable=True)
    response_modality = Column(String, CheckConstraint("response_modality IN ('voice_only', 'widget_only', 'hybrid')"), nullable=False)
    triggered_widgets = Column(Text, nullable=True)  # JSON-string representation: e.g., '["WIDGET_WEATHER"]'
    layout_context = Column(Text, nullable=False)    # Operational focus layout context state

class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, index=True)
    next_action = Column(Text, default="")        # current next step / where it's parked
    stall_risk = Column(Boolean, default=False)   # is the next action backend/stall-prone?
    status = Column(String, default="active")     # active | paused | done
    last_progress_at = Column(DateTime, default=utcnow)
    created_at = Column(DateTime, default=utcnow)


class LongTermMemory(Base):
    __tablename__ = "long_term_memory"

    id = Column(Integer, primary_key=True, index=True)
    fact_context = Column(Text, nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=utcnow)


class Reminder(Base):
    """A one-off reminder or timer Grace fires at a set time (stored in LOCAL
    naive time, since the reminder loop compares against local now())."""
    __tablename__ = "reminders"

    id = Column(Integer, primary_key=True, index=True)
    text = Column(Text, nullable=False)
    fire_at = Column(DateTime, nullable=False, index=True)
    kind = Column(String, default="reminder")   # reminder | timer
    fired = Column(Boolean, default=False)
    created_at = Column(DateTime, default=utcnow)


class Note(Base):
    """A quick captured note / idea, filed by lane."""
    __tablename__ = "notes"

    id = Column(Integer, primary_key=True, index=True)
    content = Column(Text, nullable=False)
    category = Column(String, default="general", index=True)  # dev | creative | personal | general
    created_at = Column(DateTime, default=utcnow)


class Habit(Base):
    """A recurring habit the operator wants to keep — check-ins live in
    HabitCheckin, and the streak is computed from them. longest_streak is cached
    for cheap display."""
    __tablename__ = "habits"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, index=True)
    cadence = Column(String, default="daily")     # daily (weekly reserved for later)
    icon = Column(String, default="")             # optional emoji
    longest_streak = Column(Integer, default=0)
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utcnow)


class HabitCheckin(Base):
    """One check-off of a habit on a given LOCAL day. done_on is a 'YYYY-MM-DD'
    key (local date) so streaks don't drift with timezone/UTC."""
    __tablename__ = "habit_checkins"

    id = Column(Integer, primary_key=True, index=True)
    habit_id = Column(Integer, index=True, nullable=False)
    done_on = Column(String, nullable=False, index=True)   # "YYYY-MM-DD" (local)
    created_at = Column(DateTime, default=utcnow)


class ConversationMemory(Base):
    """Episodic memory — the gist and emotional texture of a past conversation,
    so Grace can pick up threads across sessions the way a friend would."""
    __tablename__ = "conversation_memory"

    id = Column(Integer, primary_key=True, index=True)
    summary = Column(Text, nullable=False)              # "We talked about … he seemed …"
    tone = Column(String, default="")                  # short mood tag, e.g. "tired, hopeful"
    covers_until_log_id = Column(Integer, default=0)    # highest interaction_log id summarized
    created_at = Column(DateTime, default=utcnow)