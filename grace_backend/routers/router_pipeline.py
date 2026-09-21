# routers/router_pipeline.py
import os
import re
import time
import json
import uuid
import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session
from typing import List, Optional

from database import get_db
import models
from timeutils import utcnow
from core.sse import push_workspace_update
from services.audio import miso_voice
from services.system_ops import system_bridge
from services import web_search
from services import weather as weather_service
from services import projects as project_service
from services import conversation_memory
from services import code_workspace
from services import shodan_lookup
from services import ip_intel
from services import directions
from services import documents
from services import habits as habit_service
from services import google_integration
from services import os_control
from services.briefing import compose_briefing
from services.weekly_review import compose_weekly_review
from routers.voice import register_speech
from routers.upload import get_active_doc, get_active_docs

logger = logging.getLogger(__name__)

# Load environment variables from grace_backend/.env if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    logger.warning("python-dotenv not installed. Relying on system environment variables.")

try:
    from google import genai
    from google.genai import types as genai_types
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False
    logger.warning("google-genai not installed. Tier 2 reasoning will be unavailable.")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
# Fast, low-latency chat model. Override with GRACE_MODEL. (Default is a current
# model — older ones like gemini-2.5-flash now 404 on new keys.)
GRACE_MODEL = os.environ.get("GRACE_MODEL", "gemini-3.1-flash-lite")
# Model used for DEEP (code/debugging) tasks, where thoroughness beats speed.
GRACE_CODE_MODEL = os.environ.get("GRACE_CODE_MODEL", "gemini-3.6-flash")
try:
    _CHAT_MAX_TOKENS = int(os.environ.get("GRACE_MAX_TOKENS", "400"))
except ValueError:
    _CHAT_MAX_TOKENS = 400
try:
    # Generous — the deep model (e.g. 3.6-flash) is a THINKING model whose
    # internal reasoning also draws from this budget, so it needs lots of room
    # for reasoning PLUS a full written review.
    _DEEP_MAX_TOKENS = int(os.environ.get("GRACE_DEEP_MAX_TOKENS", "10000"))
except ValueError:
    _DEEP_MAX_TOKENS = 10000


# Detects when the operator wants real code work (debug/review/fix), so Grace can
# switch out of snappy-chat mode into thorough deep-analysis mode.
_CODE_TASK_RE = re.compile(
    r"\b(debug|bugs?|refactor|stack ?trace|traceback|exception|syntax error|"
    r"code review|review (?:the |this |my )?code|find (?:the )?bugs?|"
    r"what'?s wrong (?:with )?(?:the |this |my )?(?:code|file|function|script)|"
    r"(?:fix|patch|optimi[sz]e|audit) (?:the |this |my )?(?:code|bug|bugs|file|function|script|error)"
    r")\b|\.(?:js|ts|jsx|tsx|py|html|css|php|json|java|go|rb|c|cpp|vue)\b",
    re.IGNORECASE,
)


def _is_code_task(text_lower: str, has_code_doc: bool) -> bool:
    if _CODE_TASK_RE.search(text_lower):
        return True
    # An attached text/code file + any fix/debug intent counts too.
    if has_code_doc and re.search(
        r"\b(fix|debug|bugs?|review|wrong|error|broken|improve|optimi[sz]e|check|refactor)\b",
        text_lower,
    ):
        return True
    return False


# Detects an auto-documentation request: "document this", "write JSDoc", "add a
# docstring", "generate docs for X", etc.
_DOC_TASK_RE = re.compile(
    r"\b(jsdoc|docstring|phpdoc|doc ?block)\b"
    r"|\b(?:write|add|generate|create) (?:the |a |some )?(?:docs?|documentation|comments?)\b"
    r"|\bdocument(?:ation)? (?:for |of |this |the |my )?"
    r"(?:code|function|file|method|class|script|it|this)\b",
    re.IGNORECASE,
)


def _is_doc_task(text_lower: str, has_code_doc: bool) -> bool:
    if _DOC_TASK_RE.search(text_lower):
        return True
    # "document this" / "add a docstring" while a code file is attached.
    if has_code_doc and re.search(
        r"\b(document(?:ation)?|docs?|jsdoc|docstring|comments?|readme)\b", text_lower
    ):
        return True
    return False


# Working on an OPEN prose document (edit/summarise/rewrite/translate/etc.). These
# produce long output — the summary or rewrite itself — so they need the big token
# budget, not the 400-token chat cap.
_DOCEDIT_TASK_RE = re.compile(
    r"\b(summari[sz]e|summary|rewrite|re-?word|reword|paraphrase|proof-?read|"
    r"translate|shorten|condense|expand|rephrase|edit|revise|redraft|draft|"
    r"clean up|tidy up|restructure|bullet|extract|outline)\b",
    re.IGNORECASE,
)


def _is_docedit_task(text_lower: str, has_editable_doc: bool) -> bool:
    return bool(has_editable_doc and _DOCEDIT_TASK_RE.search(text_lower))


# He's asking Grace to act on a document (summarise/edit/read a pdf/doc/file) —
# used to pop the upload panel when nothing is attached yet. Requires an action
# verb AND a document noun so plain "summarise our chat" doesn't trigger it.
_WANTS_DOC_ACTION_RE = re.compile(
    r"\b(summari[sz]e|summary|rewrite|re-?word|paraphrase|proof-?read|translate|"
    r"shorten|condense|expand|rephrase|edit|revise|redraft|clean up|tidy up|"
    r"extract|outline|read|go over|look (?:at|over|through)|review|analy[sz]e|"
    r"check|correct|fix)\b[\s\S]{0,40}?"
    r"\b(pdf|pdfs|docx?|word (?:doc|document|file)|document|documents|file|files|"
    r"text file|write-?up|report|essay|letter|contract|paper|manuscript|"
    r"spreadsheet|resume|r[eé]sum[eé]|cv)\b",
    re.IGNORECASE,
)
# Also catch "here's a pdf", "this docx", "attached word doc" style openers — but
# ONLY for explicit file-type nouns (pdf/docx/word doc/text file). Generic nouns
# like "report" or "letter" need an action verb (the pattern above), so ordinary
# sentences ("send the report at 5pm") don't pop the upload panel.
_DOC_REFERENCE_RE = re.compile(
    r"\b(this|that|the|my|attached|following|here'?s?\s+(?:a|an|the|my))\s+"
    r"(pdfs?|docx|word (?:doc|document|file)|text file)\b",
    re.IGNORECASE,
)


def _wants_document_upload(text_lower: str) -> bool:
    return bool(_WANTS_DOC_ACTION_RE.search(text_lower)
                or _DOC_REFERENCE_RE.search(text_lower))


def _build_llm_client():
    """
    Build the async Gemini client once at module load (reused across requests).
    Reads GEMINI_API_KEY from the environment (loaded from .env above). Returns
    None when the SDK is missing or no key is configured, so callers degrade
    gracefully instead of crashing.
    """
    if not GEMINI_AVAILABLE or not GEMINI_API_KEY:
        return None
    # The async surface lives at client.aio.* on a standard Client.
    return genai.Client(api_key=GEMINI_API_KEY)


grace_llm_client = _build_llm_client()
VALID_LAYOUT_CONTEXTS = {"OPEN_GRID", "MAP_FULLSCREEN", "KANBAN_WORKSPACE", "CODE_PANEL"}
VALID_INPUT_TYPES = {"text", "voice"}
VALID_MODALITIES = {"voice_only", "widget_only", "hybrid"}

# Matches a complete sentence ending in . ! ? … (with optional closing quote/bracket)
# followed by whitespace — used to flush finished sentences for streaming TTS.
_SENTENCE_RE = re.compile(r'^(.*?[.!?…]+["\')\]]*)(\s+)', re.S)


def _pop_sentence(buffer: str):
    """Pop the first complete sentence from buffer.

    Returns (sentence, remaining). If no full sentence is ready, returns
    (None, buffer). Falls back to flushing at a word boundary once the buffer
    grows long without any terminator, so speech never stalls on run-ons.
    """
    m = _SENTENCE_RE.match(buffer)
    if m:
        return m.group(1).strip(), buffer[m.end():]
    if len(buffer) > 220:
        cut = buffer.rfind(" ", 0, 220)
        if cut > 0:
            return buffer[:cut].strip(), buffer[cut + 1:]
    return None, buffer


# Maps spoken place names to IANA timezone identifiers. Extend freely — any
# valid zoneinfo key works. Keys are matched case-insensitively as whole words.
_PLACE_TIMEZONES = {
    "nigeria": "Africa/Lagos",
    "lagos": "Africa/Lagos",
    "abuja": "Africa/Lagos",
    "london": "Europe/London",
    "uk": "Europe/London",
    "england": "Europe/London",
    "new york": "America/New_York",
    "nyc": "America/New_York",
    "los angeles": "America/Los_Angeles",
    "california": "America/Los_Angeles",
    "chicago": "America/Chicago",
    "tokyo": "Asia/Tokyo",
    "japan": "Asia/Tokyo",
    "india": "Asia/Kolkata",
    "delhi": "Asia/Kolkata",
    "mumbai": "Asia/Kolkata",
    "china": "Asia/Shanghai",
    "beijing": "Asia/Shanghai",
    "shanghai": "Asia/Shanghai",
    "dubai": "Asia/Dubai",
    "uae": "Asia/Dubai",
    "paris": "Europe/Paris",
    "france": "Europe/Paris",
    "germany": "Europe/Berlin",
    "berlin": "Europe/Berlin",
    "sydney": "Australia/Sydney",
    "australia": "Australia/Sydney",
    "moscow": "Europe/Moscow",
    "russia": "Europe/Moscow",
    "brazil": "America/Sao_Paulo",
    "sao paulo": "America/Sao_Paulo",
    "canada": "America/Toronto",
    "toronto": "America/Toronto",
    "south africa": "Africa/Johannesburg",
    "johannesburg": "Africa/Johannesburg",
    "utc": "UTC",
    "gmt": "UTC",
}


def _resolve_place_timezone(text_lower: str):
    """Return (place_label, ZoneInfo) if a known place is named in the text.

    Longer names are matched first so "new york" wins over any substring. Falls
    back to (None, None) when no place is mentioned (caller uses local time).
    """
    for place in sorted(_PLACE_TIMEZONES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(place)}\b", text_lower):
            return place.title(), ZoneInfo(_PLACE_TIMEZONES[place])
    return None, None


# A Tier-1 clock reply should fire only for a genuine "what time/date is it"
# QUESTION — never because a stray "time"/"date"/"today" appeared inside a longer
# message (e.g. "a real-time watch party", "I like being on time"). Each branch
# requires question framing or an explicit "current/today's" qualifier.
_DATETIME_QUERY_RE = re.compile(
    r"\bwhat(?:'?s| is)?\s+(?:the\s+|today'?s\s+)?(?:time|date|day|year|month)\b"
    r"|\bwhat\s+(?:time|date|day|year|month)\s+is\s+(?:it|today)\b"
    r"|\b(?:current|present|today'?s)\s+(?:time|date|day|year|month)\b"
    r"|\bthe\s+(?:time|date)\s+(?:right\s+)?now\b"
    r"|\btime\s+is\s+it\b"
    r"|\btell\s+me\s+(?:the\s+)?(?:time|date|day)\b"
    r"|\b(?:do\s+you\s+have|got)\s+the\s+time\b"
    r"|\bwhat\s+time\b"
    r"|^\s*(?:time|date|day|clock)\s*\??\s*$",   # a bare one-word query
    re.IGNORECASE,
)

# A request for the daily briefing / rundown. Time-of-day greetings double as a
# briefing trigger — very Jarvis: greet Grace and she reports your day.
_BRIEFING_RE = re.compile(
    r"\bbrief me\b"
    r"|\bbriefing\b"
    r"|\bmy brief\b"
    r"|\bdaily (?:briefing|report|rundown|summary)\b"
    r"|\brundown\b"
    r"|\bcatch me up\b"
    r"|\bstatus report\b"
    r"|\bwhat'?s (?:on|on my) (?:today|my day|my plate|the agenda)\b"
    r"|\bwhat (?:do i have|does my day look like|is on) today\b"
    r"|\bhow does my day look\b"
    r"|\bgood (?:morning|afternoon|evening)\b",
    re.IGNORECASE,
)

# A request for the weekly review / accountability recap.
_WEEKLY_RE = re.compile(
    r"\bweekly review\b"
    r"|\bweek in review\b"
    r"|\bweekly recap\b"
    r"|\b(?:how was|how'?d|recap|review|sum up|summarize) my week\b"
    r"|\bhow did my week go\b",
    re.IGNORECASE,
)

# Remaining Tier-1 fast-path commands, named so both the dispatch below and the
# history filter reference the SAME patterns (no drift).
_LOCK_RE   = re.compile(r"\b(lock system|lock workstation|screen lock)\b", re.IGNORECASE)
_VSCODE_RE = re.compile(r"\b(open vscode|launch code editor|open vs code)\b", re.IGNORECASE)
_DIAG_RE   = re.compile(r"\b(diagnostics|system status|hardware performance)\b", re.IGNORECASE)
_WEATHER_RE = re.compile(r"\b(weather|temperature|forecast|how (?:hot|cold|warm) is it|is it raining)\b", re.IGNORECASE)

# Every Tier-1 trigger. A message matching any of these is answered by a canned/
# deterministic reply that never went through Gemini, so it must be excluded from
# the replayed conversation history (it isn't real dialogue).
_TIER1_PATTERNS = (_BRIEFING_RE, _WEEKLY_RE, _DATETIME_QUERY_RE, _LOCK_RE, _VSCODE_RE, _DIAG_RE, _WEATHER_RE)


def _is_tier1_message(text: str) -> bool:
    t = (text or "").lower()
    return any(p.search(t) for p in _TIER1_PATTERNS)


def _format_datetime_response(text_lower: str) -> str:
    """Build a human-friendly date/time reply from the live system clock.

    Uses zoneinfo when a place is named ("time in Nigeria"); otherwise reports
    the server's local time. Always accurate — computed from the clock, not the
    language model.
    """
    place_label, tz = _resolve_place_timezone(text_lower)
    now = datetime.now(tz) if tz else datetime.now()

    date_str = now.strftime("%A, %B %d, %Y")
    time_str = now.strftime("%I:%M %p").lstrip("0")

    wants_time = re.search(r"\b(time|clock|hour)\b", text_lower)
    wants_date = re.search(r"\b(date|day|today|month|year)\b", text_lower)

    where = f" in {place_label}" if place_label else ""

    if wants_time and not wants_date:
        return f"The current time{where} is {time_str} on {date_str}."
    if wants_date and not wants_time:
        return f"Today's date{where} is {date_str}."
    return f"It is currently {time_str}{where} on {date_str}."


# Holds detached TTS tasks so the event loop keeps a strong reference and they
# aren't garbage-collected mid-synthesis after the request handler returns.
_background_tts_tasks: set = set()


def _spawn_tts(stream_id: str, text: str, seq: int):
    """Schedule a TTS chunk as a tracked background task and return it."""
    task = asyncio.create_task(_speak_chunk(stream_id, text, seq))
    _background_tts_tasks.add(task)
    task.add_done_callback(_background_tts_tasks.discard)
    return task


async def _speak_chunk(stream_id: str, text: str, seq: int):
    """Synthesize one sentence and push it as an ordered audio chunk.

    Runs as a background task so text keeps streaming while TTS renders. The
    frontend plays chunks in `seq` order, so out-of-order completion is fine.
    """
    url = ""
    try:
        url = await miso_voice.generate_speech(text) or ""
    except Exception as e:
        logger.error(f"Streaming TTS chunk failed: {e}", exc_info=True)
    # Always emit the chunk — even with an empty url on failure. The frontend
    # keys its text reveal to this packet's seq, so a missing chunk would leave
    # that sentence's text forever hidden. An empty url just plays nothing.
    await push_workspace_update(
        "WIDGET_CHAT", {"stream_id": stream_id, "audio_chunk": url, "seq": seq}
    )


# Tool schema advertised to Gemini. Only attached when Tavily is configured, so
# the model can't call a tool that would just error. Built lazily because the
# genai types are only available when the SDK imported successfully.
_WEB_SEARCH_DESCRIPTION = (
    "Search the live web for current, real-time, or recent information the "
    "assistant does not already know — news, prices, weather, sports, events, "
    "or any fact that may have changed after training. Returns a summary and "
    "source snippets. Use it whenever a question depends on up-to-date data."
)


def _build_search_tool():
    """Construct the Gemini function-declaration tool for web search."""
    return genai_types.Tool(
        function_declarations=[
            genai_types.FunctionDeclaration(
                name="web_search",
                description=_WEB_SEARCH_DESCRIPTION,
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "query": genai_types.Schema(
                            type=genai_types.Type.STRING,
                            description="A focused search query capturing what to look up.",
                        )
                    },
                    required=["query"],
                ),
            )
        ]
    )


def _build_task_tool():
    """Gemini function-declarations for managing kanban tasks (create + delete)."""
    return genai_types.Tool(
        function_declarations=[
            genai_types.FunctionDeclaration(
                name="create_task",
                description=(
                    "Create a new task on the operator's kanban board. "
                    "Use whenever the operator asks to add, create, or log a task, "
                    "ticket, or to-do item. Extract title, priority, tag, status, "
                    "description, and checklist items from their request."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "title":       genai_types.Schema(type=genai_types.Type.STRING, description="Short task title."),
                        "description": genai_types.Schema(type=genai_types.Type.STRING, description="Brief description of the task."),
                        "priority":    genai_types.Schema(type=genai_types.Type.STRING, description="One of: high, med, low."),
                        "tag":         genai_types.Schema(type=genai_types.Type.STRING, description="Short category tag e.g. DEV, OPS, AI, UI."),
                        "status":      genai_types.Schema(type=genai_types.Type.STRING, description="One of: todo, inprog, done."),
                        "checklist":   genai_types.Schema(
                            type=genai_types.Type.ARRAY,
                            items=genai_types.Schema(type=genai_types.Type.STRING),
                            description="List of checklist items for the task.",
                        ),
                    },
                    required=["title"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="delete_task",
                description=(
                    "Delete a task from the operator's kanban board. Use whenever "
                    "the operator asks to delete, remove, cancel, or get rid of a "
                    "task, ticket, or to-do item. Pass the task's title (or the "
                    "closest phrase the operator used to refer to it) so it can be "
                    "matched against the board. Pass task_id instead only if the "
                    "operator gave an explicit numeric id."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "title":   genai_types.Schema(type=genai_types.Type.STRING, description="Title (or referring phrase) of the task to delete."),
                        "task_id": genai_types.Schema(type=genai_types.Type.INTEGER, description="Explicit numeric id of the task to delete, if known."),
                    },
                ),
            ),
            genai_types.FunctionDeclaration(
                name="update_task",
                description=(
                    "Update an existing task on the operator's kanban board. Use "
                    "whenever the operator asks to MOVE a task between columns "
                    "(e.g. 'move X to done', 'mark X complete', 'put X back in "
                    "to-do', 'start working on X'), or to change its priority, "
                    "rename it, or re-tag it. Identify the task by task_id if the "
                    "operator gave a number, otherwise by its current title (or "
                    "the phrase they used). Map the target column to new_status: "
                    "'to-do'/'todo'/'backlog' -> todo; 'in progress'/'doing'/"
                    "'working on it'/'started' -> inprog; 'done'/'complete'/"
                    "'finished' -> done. Only include the fields that should change."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "task_id":      genai_types.Schema(type=genai_types.Type.INTEGER, description="Numeric id of the task, if known."),
                        "title":        genai_types.Schema(type=genai_types.Type.STRING, description="Current title (or referring phrase) to find the task."),
                        "new_status":   genai_types.Schema(type=genai_types.Type.STRING, description="New column: one of todo, inprog, done."),
                        "new_priority": genai_types.Schema(type=genai_types.Type.STRING, description="New priority: high, med, or low."),
                        "new_title":    genai_types.Schema(type=genai_types.Type.STRING, description="New title, if renaming."),
                        "new_tag":      genai_types.Schema(type=genai_types.Type.STRING, description="New category tag, if changing it."),
                    },
                ),
            ),
        ]
    )


# Cap tool-use turns so a misbehaving loop can't spin forever. Each round is one
# Gemini turn; a search + answer normally needs 2.
_MAX_TOOL_ROUNDS = 4

_VALID_PRIORITIES = {"high", "med", "low"}
_VALID_STATUSES = {"todo", "inprog", "done"}


async def _create_task_from_tool(args: dict, db) -> str:
    """Persist a task from a Gemini create_task tool call, then push the board
    an update so the kanban widget refreshes (and pops open) live. Returns a
    short result string fed back to the model for its spoken confirmation."""
    title = (args.get("title") or "").strip()
    if not title:
        return "Task not created: no title was provided."

    priority = (args.get("priority") or "med").strip().lower()
    if priority not in _VALID_PRIORITIES:
        priority = "med"
    status = (args.get("status") or "todo").strip().lower()
    if status not in _VALID_STATUSES:
        status = "todo"
    tag = ((args.get("tag") or "MISC").strip().upper())[:50] or "MISC"
    description = (args.get("description") or "").strip()
    checklist = args.get("checklist") or []
    if not isinstance(checklist, list):
        checklist = []
    checklist = [str(c).strip() for c in checklist if str(c).strip()][:20]

    try:
        new_task = models.Task(
            title=title[:500],
            description=description,
            priority=priority,
            tag=tag,
            status=status,
            checklist=checklist,
            checklist_done=[False] * len(checklist),
        )
        db.add(new_task)
        db.commit()
        db.refresh(new_task)
    except Exception as e:
        logger.error(f"create_task tool DB write failed: {e}", exc_info=True)
        db.rollback()
        return f"Failed to create task '{title}' due to a database error."

    # Tell the UI to reload the board and open the kanban widget.
    await push_workspace_update(
        "WIDGET_KANBAN",
        {"action": "task_created", "task_id": new_task.id, "title": title},
    )

    cl_note = f" with {len(checklist)} checklist item(s)" if checklist else ""
    return (
        f"Task '{title}' created on the board in '{status}' "
        f"at {priority} priority{cl_note}."
    )


def _find_task(args: dict, db):
    """Locate a single Task by explicit id, else by title (exact, case-insensitive)
    with a unique-substring fallback. Returns (task, error_string) — exactly one
    is non-None. Shared by the delete and update task tools."""
    task_id = args.get("task_id")
    title = (args.get("title") or "").strip()

    if task_id is not None:
        try:
            t = db.query(models.Task).filter(models.Task.id == int(task_id)).first()
        except (ValueError, TypeError):
            t = None
        return (t, None) if t else (None, f"No task with id {task_id} was found on the board.")
    if not title:
        return None, "No task title or id was provided."

    rows = db.query(models.Task).all()
    low = title.lower()
    exact = [t for t in rows if (t.title or "").strip().lower() == low]
    if len(exact) == 1:
        return exact[0], None
    if len(exact) > 1:
        return None, f"Found {len(exact)} tasks titled '{title}'. Provide the task_id."
    partial = [t for t in rows if low in (t.title or "").strip().lower()]
    if len(partial) == 1:
        return partial[0], None
    if len(partial) > 1:
        names = ", ".join(f"'{t.title}' (id {t.id})" for t in partial[:5])
        return None, f"Several tasks match '{title}': {names}. Provide the task_id."
    return None, f"No task matching '{title}' was found on the board."


async def _delete_task_from_tool(args: dict, db) -> str:
    """Delete a task from a Gemini delete_task tool call, then push the board an
    update so the kanban widget refreshes live. Returns a short result string for
    the model's confirmation."""
    match, err = _find_task(args, db)
    if err:
        return err

    deleted_id = match.id
    deleted_title = match.title
    try:
        db.delete(match)
        db.commit()
    except Exception as e:
        logger.error(f"delete_task tool DB delete failed: {e}", exc_info=True)
        db.rollback()
        return f"Failed to delete task '{deleted_title}' due to a database error."

    # Tell the UI to reload the board (without popping it open).
    await push_workspace_update(
        "WIDGET_KANBAN",
        {"action": "task_deleted", "task_id": deleted_id, "title": deleted_title},
    )

    return f"Task '{deleted_title}' was deleted from the board."


_STATUS_LABEL = {"todo": "To Do", "inprog": "In Progress", "done": "Done"}

# Natural-language column names Gemini (or the operator) might use, mapped to the
# canonical status codes stored on the board.
_STATUS_ALIASES = {
    "todo": "todo", "to-do": "todo", "to do": "todo", "backlog": "todo",
    "pending": "todo", "open": "todo", "not started": "todo",
    "inprog": "inprog", "in-prog": "inprog", "in prog": "inprog",
    "in progress": "inprog", "in-progress": "inprog", "doing": "inprog",
    "started": "inprog", "working": "inprog", "wip": "inprog", "active": "inprog",
    "done": "done", "complete": "done", "completed": "done", "finished": "done",
    "closed": "done", "resolved": "done",
}


def _normalize_status(raw: str):
    """Map a free-form column name ('in progress', 'complete', …) to a canonical
    status code (todo | inprog | done), or None if unrecognized."""
    key = (raw or "").strip().lower()
    if key in _VALID_STATUSES:
        return key
    return _STATUS_ALIASES.get(key)


async def _update_task_from_tool(args: dict, db) -> str:
    """Move/edit a task from a Gemini update_task tool call, then push the board
    a live refresh. Finds the task via _find_task, then applies any of
    new_status / new_priority / new_title / new_tag that were supplied."""
    match, err = _find_task(args, db)
    if err:
        return err

    changes = []
    new_status_raw = (args.get("new_status") or "").strip()
    if new_status_raw:
        status = _normalize_status(new_status_raw)
        if status is None:
            return (
                f"'{new_status_raw}' isn't a valid column. Use To Do, "
                f"In Progress, or Done."
            )
        # Stamp completion time when it first enters Done; clear if it leaves.
        if status == "done" and match.status != "done":
            match.completed_at = utcnow()
        elif status != "done":
            match.completed_at = None
        match.status = status
        changes.append(f"moved to {_STATUS_LABEL[status]}")

    new_priority = (args.get("new_priority") or "").strip().lower()
    if new_priority:
        if new_priority not in _VALID_PRIORITIES:
            new_priority = "med"
        match.priority = new_priority
        changes.append(f"priority set to {new_priority}")

    new_title = (args.get("new_title") or "").strip()
    if new_title:
        match.title = new_title[:500]
        changes.append(f"renamed to '{new_title}'")

    new_tag = (args.get("new_tag") or "").strip()
    if new_tag:
        match.tag = new_tag.upper()[:50]
        changes.append(f"tagged {match.tag}")

    if not changes:
        return "Nothing to update: no new values were provided."

    updated_id = match.id
    updated_title = match.title
    try:
        db.commit()
        db.refresh(match)
    except Exception as e:
        logger.error(f"update_task tool DB update failed: {e}", exc_info=True)
        db.rollback()
        return "Failed to update the task due to a database error."

    await push_workspace_update(
        "WIDGET_KANBAN",
        {"action": "task_updated", "task_id": updated_id, "title": updated_title},
    )
    return f"Task '{updated_title}' updated: {', '.join(changes)}."


def _build_task_snapshot(db, limit=60):
    """Return a text listing of the kanban board for the system prompt, so Grace
    can answer questions about tasks ("any pending tasks?", "what's in
    progress?") and act on them by id. Grouped by status, capped at `limit`."""
    try:
        rows = (
            db.query(models.Task)
            .order_by(models.Task.status, models.Task.id)
            .limit(limit)
            .all()
        )
    except Exception as e:
        logger.warning(f"Could not load task snapshot: {e}")
        return ""
    if not rows:
        return " The operator's task board is currently empty."
    lines = []
    for t in rows:
        status = _STATUS_LABEL.get(t.status, t.status)
        lines.append(
            f'  - id {t.id}: [{status}] "{t.title}" '
            f'(priority {t.priority}, tag {t.tag})'
        )
    return (
        " Here is the operator's CURRENT task board, refreshed live from the "
        "database this very moment (the single source of truth). It SUPERSEDES "
        "any task list you gave earlier in this conversation. PENDING tasks are "
        "those with status To Do or In Progress (not Done). When asked about "
        "tasks, use EXACTLY this list; act on them with the delete_task tool by "
        "id, and never invent a task not shown here:\n"
        + "\n".join(lines)
    )


def _build_calendar_snapshot(db, limit=40):
    """Return a short text listing of calendar events for the system prompt, so
    Grace KNOWS what is actually on the calendar and can act on references like
    "the event on july 26th" without asking for a title she was never given.
    Lists everything on file (capped), ordered by date."""
    try:
        rows = (
            db.query(models.CalendarEvent)
            .order_by(models.CalendarEvent.date_key, models.CalendarEvent.time)
            .limit(limit)
            .all()
        )
    except Exception as e:
        logger.warning(f"Could not load calendar snapshot: {e}")
        return ""
    if not rows:
        return " The operator's calendar currently has no events."
    lines = [
        f'  - id {e.id}: {e.date_key} {e.time or ""} "{e.title}" [{e.priority}]'.rstrip()
        for e in rows
    ]
    return (
        " Here is the operator's CURRENT calendar, refreshed live from the "
        "database this very moment (the single source of truth). It SUPERSEDES "
        "any list of events you gave earlier in this conversation — events may "
        "have been added, edited, or DELETED since, so an event you mentioned "
        "before may no longer exist. When asked to list events, list EXACTLY "
        "these and nothing else. When the operator refers to an event by date or "
        "name, match it here and act with the update_event / delete_event tools "
        "using its id; never invent or resurrect an event that is not in this "
        "list:\n"
        + "\n".join(lines)
    )


def _build_event_tool():
    """Gemini function-declarations for managing calendar events (create + delete + update)."""
    today = datetime.now().strftime("%Y-%m-%d")
    weekday = datetime.now().strftime("%A")
    return genai_types.Tool(
        function_declarations=[
            genai_types.FunctionDeclaration(
                name="create_event",
                description=(
                    "Add an event to the operator's calendar. Use whenever the "
                    "operator asks to schedule, add, or create a meeting, reminder, "
                    "appointment, or event. Extract the date, time, title, and "
                    f"priority from their request. TODAY is {today} ({weekday}). "
                    "Resolve all relative dates (today, tomorrow, next Monday, etc.) "
                    f"from this reference. Always use year {datetime.now().year} or later — never a past year."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "date_key":  genai_types.Schema(type=genai_types.Type.STRING, description="Date in YYYY-MM-DD format."),
                        "time":      genai_types.Schema(type=genai_types.Type.STRING, description="Time in HH:MM 24-hour format, e.g. 14:30."),
                        "title":     genai_types.Schema(type=genai_types.Type.STRING, description="Short event title."),
                        "priority":  genai_types.Schema(type=genai_types.Type.STRING, description="One of: hi, med, low."),
                    },
                    required=["date_key", "title"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="delete_event",
                description=(
                    "Delete an event from the operator's calendar. Use whenever "
                    "the operator asks to remove, cancel, or delete a scheduled "
                    "event. Pass the event_id if known, otherwise pass the title "
                    "and date_key so it can be matched."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "event_id":  genai_types.Schema(type=genai_types.Type.INTEGER, description="Numeric id of the event, if known."),
                        "title":     genai_types.Schema(type=genai_types.Type.STRING, description="Title of the event to delete."),
                        "date_key":  genai_types.Schema(type=genai_types.Type.STRING, description="Date of the event in YYYY-MM-DD format."),
                    },
                ),
            ),
            genai_types.FunctionDeclaration(
                name="update_event",
                description=(
                    "Edit an existing event on the operator's calendar. Use when "
                    "the operator asks to change, move, reschedule, rename, or "
                    "update an event. Identify the target event by event_id if "
                    "known, otherwise by its current title (and date_key if given). "
                    "Only include the fields that should change: new_date_key, "
                    "new_time, new_title, or new_priority."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "event_id":     genai_types.Schema(type=genai_types.Type.INTEGER, description="Numeric id of the event to edit, if known."),
                        "title":        genai_types.Schema(type=genai_types.Type.STRING, description="Current title of the event to find it."),
                        "date_key":     genai_types.Schema(type=genai_types.Type.STRING, description="Current date (YYYY-MM-DD) of the event to find it."),
                        "new_date_key": genai_types.Schema(type=genai_types.Type.STRING, description="New date in YYYY-MM-DD, if moving the event."),
                        "new_time":     genai_types.Schema(type=genai_types.Type.STRING, description="New time in HH:MM 24-hour format."),
                        "new_title":    genai_types.Schema(type=genai_types.Type.STRING, description="New title, if renaming."),
                        "new_priority": genai_types.Schema(type=genai_types.Type.STRING, description="New priority: hi, med, or low."),
                    },
                ),
            ),
        ]
    )


async def _create_event_from_tool(args: dict, db) -> str:
    date_key = (args.get("date_key") or "").strip()
    title    = (args.get("title") or "").strip()
    if not date_key or not title:
        return "Event not created: date and title are required."
    try:
        datetime.strptime(date_key, "%Y-%m-%d")
    except ValueError:
        return f"Event not created: '{date_key}' is not a valid YYYY-MM-DD date."

    # Auto-correct past-year dates — Gemini sometimes resolves relative dates
    # to a prior year when it lacks today's date. Bump to the current year (or
    # next if that date has already passed this year).
    today = datetime.now().date()
    parsed = datetime.strptime(date_key, "%Y-%m-%d").date()
    if parsed < today:
        corrected = parsed.replace(year=today.year)
        if corrected < today:
            corrected = parsed.replace(year=today.year + 1)
        date_key = corrected.strftime("%Y-%m-%d")

    time     = (args.get("time") or "00:00").strip()
    priority = (args.get("priority") or "med").strip().lower()
    if priority not in {"hi", "med", "low"}:
        priority = "med"

    try:
        ev = models.CalendarEvent(date_key=date_key, time=time, title=title[:500], priority=priority)
        db.add(ev)
        db.commit()
        db.refresh(ev)
    except Exception as e:
        logger.error(f"create_event tool DB write failed: {e}", exc_info=True)
        db.rollback()
        return f"Failed to create event '{title}' due to a database error."

    await push_workspace_update(
        "WIDGET_CALENDAR",
        {"action": "event_created", "event_id": ev.id, "date_key": date_key, "title": title},
    )
    return f"Event '{title}' added to the calendar on {date_key} at {time}."


async def _delete_event_from_tool(args: dict, db) -> str:
    event_id = args.get("event_id")
    title    = (args.get("title") or "").strip()
    date_key = (args.get("date_key") or "").strip()

    match = None
    if event_id is not None:
        try:
            match = db.query(models.CalendarEvent).filter(models.CalendarEvent.id == int(event_id)).first()
        except (ValueError, TypeError):
            match = None
        if not match:
            return f"No event with id {event_id} found."
    elif title:
        q = db.query(models.CalendarEvent)
        if date_key:
            q = q.filter(models.CalendarEvent.date_key == date_key)
        rows = q.all()
        low = title.lower()
        exact = [e for e in rows if (e.title or "").strip().lower() == low]
        if len(exact) == 1:
            match = exact[0]
        elif len(exact) > 1:
            return f"Found {len(exact)} events titled '{title}'. Provide the event_id to disambiguate."
        else:
            partial = [e for e in rows if low in (e.title or "").strip().lower()]
            if len(partial) == 1:
                match = partial[0]
            elif len(partial) > 1:
                names = ", ".join(f"'{e.title}' on {e.date_key} (id {e.id})" for e in partial[:5])
                return f"Several events match '{title}': {names}. Provide the event_id."
            else:
                return f"No event matching '{title}' found."
    else:
        return "Event not deleted: no title or id provided."

    deleted_id    = match.id
    deleted_title = match.title
    deleted_date  = match.date_key
    try:
        db.delete(match)
        db.commit()
    except Exception as e:
        logger.error(f"delete_event tool DB delete failed: {e}", exc_info=True)
        db.rollback()
        return f"Failed to delete event '{deleted_title}' due to a database error."

    await push_workspace_update(
        "WIDGET_CALENDAR",
        {"action": "event_deleted", "event_id": deleted_id, "date_key": deleted_date, "title": deleted_title},
    )
    return f"Event '{deleted_title}' on {deleted_date} was deleted from the calendar."


def _find_event(args: dict, db):
    """Locate a single CalendarEvent by id, else by title (+optional date_key).
    Returns (event, error_string). Exactly one of the two is non-None."""
    event_id = args.get("event_id")
    title    = (args.get("title") or "").strip()
    date_key = (args.get("date_key") or "").strip()

    if event_id is not None:
        try:
            ev = db.query(models.CalendarEvent).filter(models.CalendarEvent.id == int(event_id)).first()
        except (ValueError, TypeError):
            ev = None
        return (ev, None) if ev else (None, f"No event with id {event_id} found.")

    # No id and no title, but a date was given: match by date alone. If exactly
    # one event sits on that day it's unambiguous — this is what lets "the event
    # on july 26th" resolve without the operator restating the title.
    if not title:
        if not date_key:
            return None, "No title or id provided to identify the event."
        rows = db.query(models.CalendarEvent).filter(
            models.CalendarEvent.date_key == date_key
        ).all()
        if len(rows) == 1:
            return rows[0], None
        if len(rows) == 0:
            return None, f"No event found on {date_key}."
        names = ", ".join(f"'{e.title}' (id {e.id})" for e in rows[:5])
        return None, f"Several events are on {date_key}: {names}. Which one?"

    q = db.query(models.CalendarEvent)
    if date_key:
        q = q.filter(models.CalendarEvent.date_key == date_key)
    rows = q.all()
    low = title.lower()
    exact = [e for e in rows if (e.title or "").strip().lower() == low]
    if len(exact) == 1:
        return exact[0], None
    if len(exact) > 1:
        return None, f"Found {len(exact)} events titled '{title}'. Provide the event_id."
    partial = [e for e in rows if low in (e.title or "").strip().lower()]
    if len(partial) == 1:
        return partial[0], None
    if len(partial) > 1:
        names = ", ".join(f"'{e.title}' on {e.date_key} (id {e.id})" for e in partial[:5])
        return None, f"Several events match '{title}': {names}. Provide the event_id."
    return None, f"No event matching '{title}' found."


async def _update_event_from_tool(args: dict, db) -> str:
    match, err = _find_event(args, db)
    if err:
        return err

    changes = []
    new_date = (args.get("new_date_key") or "").strip()
    if new_date:
        try:
            datetime.strptime(new_date, "%Y-%m-%d")
            match.date_key = new_date
            changes.append(f"date to {new_date}")
        except ValueError:
            return f"'{new_date}' is not a valid YYYY-MM-DD date."
    new_time = (args.get("new_time") or "").strip()
    if new_time:
        match.time = new_time
        changes.append(f"time to {new_time}")
    new_title = (args.get("new_title") or "").strip()
    if new_title:
        match.title = new_title[:500]
        changes.append(f"title to '{new_title}'")
    new_priority = (args.get("new_priority") or "").strip().lower()
    if new_priority:
        if new_priority not in {"hi", "med", "low"}:
            new_priority = "med"
        match.priority = new_priority
        changes.append(f"priority to {new_priority}")

    if not changes:
        return "Nothing to update: no new values were provided."

    updated_id    = match.id
    updated_title = match.title
    updated_date  = match.date_key
    try:
        db.commit()
        db.refresh(match)
    except Exception as e:
        logger.error(f"update_event tool DB update failed: {e}", exc_info=True)
        db.rollback()
        return f"Failed to update event due to a database error."

    await push_workspace_update(
        "WIDGET_CALENDAR",
        {"action": "event_updated", "event_id": updated_id, "date_key": updated_date, "title": updated_title},
    )
    return f"Event '{updated_title}' updated: changed {', '.join(changes)}."


# ── Long-term memory ────────────────────────────────────────────────────────
# Facts the operator wants Grace to remember across sessions live in the
# long_term_memory table and are injected into her context each turn, so she can
# personalize replies without being reminded. She manages them with the
# remember_fact / forget_fact tools.

def _build_memory_tool():
    """Gemini function-declarations for durable personal memory."""
    return genai_types.Tool(
        function_declarations=[
            genai_types.FunctionDeclaration(
                name="remember_fact",
                description=(
                    "Save a durable fact about the operator so you can recall it "
                    "in future sessions. Use whenever they share something worth "
                    "remembering long-term — their name, preferences, people and "
                    "pets in their life, routines, goals, or an explicit 'remember "
                    "that ...'. Do NOT use this for fleeting task/calendar items "
                    "(those have their own tools). Store one clear fact per call."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "content": genai_types.Schema(type=genai_types.Type.STRING, description="The fact to remember, phrased as a concise statement."),
                        "context": genai_types.Schema(type=genai_types.Type.STRING, description="Short category label, e.g. personal, preference, work, relationship."),
                    },
                    required=["content"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="forget_fact",
                description=(
                    "Delete a previously remembered fact. Use when the operator "
                    "asks you to forget something or says a stored fact is wrong. "
                    "Pass memory_id if known, otherwise a query phrase to match "
                    "against stored facts."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "memory_id": genai_types.Schema(type=genai_types.Type.INTEGER, description="Numeric id of the memory to delete, if known."),
                        "query":     genai_types.Schema(type=genai_types.Type.STRING, description="Text to match against stored facts to find the one to forget."),
                    },
                ),
            ),
        ]
    )


async def _remember_fact_from_tool(args: dict, db) -> str:
    content = (args.get("content") or "").strip()
    if not content:
        return "Nothing to remember: no fact was provided."
    context = (args.get("context") or "general").strip().lower()[:50] or "general"

    # Avoid storing a near-duplicate of something already remembered.
    low = content.lower()
    existing = db.query(models.LongTermMemory).all()
    for m in existing:
        mc = (m.content or "").strip().lower()
        if mc == low or (len(low) > 8 and (low in mc or mc in low)):
            return f"Already remembered: \"{m.content}\"."

    try:
        mem = models.LongTermMemory(fact_context=context, content=content[:1000])
        db.add(mem)
        db.commit()
        db.refresh(mem)
    except Exception as e:
        logger.error(f"remember_fact tool DB write failed: {e}", exc_info=True)
        db.rollback()
        return "Failed to store that memory due to a database error."
    return f"Got it — I'll remember that: \"{content}\"."


async def _forget_fact_from_tool(args: dict, db) -> str:
    memory_id = args.get("memory_id")
    query = (args.get("query") or "").strip()

    match = None
    if memory_id is not None:
        try:
            match = db.query(models.LongTermMemory).filter(models.LongTermMemory.id == int(memory_id)).first()
        except (ValueError, TypeError):
            match = None
        if not match:
            return f"No memory with id {memory_id} was found."
    elif query:
        rows = db.query(models.LongTermMemory).all()
        low = query.lower()
        hits = [m for m in rows if low in (m.content or "").lower()]
        if len(hits) == 1:
            match = hits[0]
        elif len(hits) > 1:
            names = "; ".join(f"\"{m.content}\" (id {m.id})" for m in hits[:5])
            return f"Several memories match '{query}': {names}. Which id should I forget?"
        else:
            return f"I don't have any memory matching '{query}'."
    else:
        return "Tell me which memory to forget (an id or a phrase)."

    forgotten = match.content
    try:
        db.delete(match)
        db.commit()
    except Exception as e:
        logger.error(f"forget_fact tool DB delete failed: {e}", exc_info=True)
        db.rollback()
        return "Failed to forget that memory due to a database error."
    return f"Forgotten: \"{forgotten}\"."


def _build_memory_snapshot(db, limit=60):
    """Inject the operator's remembered facts into the system prompt so Grace
    recalls them without being told, across sessions."""
    try:
        rows = (
            db.query(models.LongTermMemory)
            .order_by(models.LongTermMemory.created_at)
            .limit(limit)
            .all()
        )
    except Exception as e:
        logger.warning(f"Could not load memory snapshot: {e}")
        return ""
    if not rows:
        return ""
    lines = [f"  - (id {m.id}, {m.fact_context}) {m.content}" for m in rows]
    return (
        " Here is what you REMEMBER about the operator from earlier sessions — "
        "use it naturally to personalize your replies (do not recite the whole "
        "list unless asked, and never claim you can't remember things about them "
        "when they are listed here):\n"
        + "\n".join(lines)
    )


# ── Project momentum (anti-stall) ───────────────────────────────────────────
# Grace tracks each active project's next action and when it last moved, so she
# can nudge the operator on backend items — his documented stall point.

def _build_project_tool():
    return genai_types.Tool(
        function_declarations=[
            genai_types.FunctionDeclaration(
                name="track_project",
                description=(
                    "Create or update one of the operator's projects and its "
                    "current next action. Use when he mentions a project, states "
                    "what's next on it, parks it, or finishes it. Matches an "
                    "existing project by name (case-insensitive) or creates a new "
                    "one. Set stall_risk true when the next action is backend work "
                    "or otherwise stall-prone — the operator has said backend is "
                    "where his projects stall. Set status to 'done' or 'paused' "
                    "when he completes or shelves it."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "name":        genai_types.Schema(type=genai_types.Type.STRING, description="Project name."),
                        "next_action": genai_types.Schema(type=genai_types.Type.STRING, description="The current next step / where it's parked."),
                        "stall_risk":  genai_types.Schema(type=genai_types.Type.BOOLEAN, description="True if the next action is backend or stall-prone."),
                        "status":      genai_types.Schema(type=genai_types.Type.STRING, description="active, paused, or done."),
                    },
                    required=["name"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="log_progress",
                description=(
                    "Record that the operator made progress on a project today — "
                    "resets its stall clock. Use when he says he worked on, "
                    "touched, tested, shipped, or advanced a project. Optionally "
                    "update its next action to the new next step."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "name":        genai_types.Schema(type=genai_types.Type.STRING, description="Project name that saw progress."),
                        "next_action": genai_types.Schema(type=genai_types.Type.STRING, description="Updated next step, if it changed."),
                        "stall_risk":  genai_types.Schema(type=genai_types.Type.BOOLEAN, description="Whether the NEW next action is backend/stall-prone."),
                    },
                    required=["name"],
                ),
            ),
        ]
    )


def _find_project(name, db):
    name = (name or "").strip()
    if not name:
        return None
    low = name.lower()
    rows = db.query(models.Project).all()
    exact = [p for p in rows if (p.name or "").strip().lower() == low]
    if exact:
        return exact[0]
    partial = [p for p in rows if low in (p.name or "").strip().lower()]
    return partial[0] if len(partial) == 1 else None


async def _track_project_from_tool(args: dict, db) -> str:
    name = (args.get("name") or "").strip()
    if not name:
        return "No project name was provided."
    next_action = args.get("next_action")
    stall_risk = args.get("stall_risk")
    status = (args.get("status") or "").strip().lower() or None
    if status and status not in {"active", "paused", "done"}:
        status = "active"

    proj = _find_project(name, db)
    created = False
    try:
        if proj is None:
            proj = models.Project(
                name=name[:200],
                next_action=(next_action or "")[:1000],
                stall_risk=bool(stall_risk),
                status=status or "active",
            )
            db.add(proj)
            created = True
        else:
            if next_action is not None:
                proj.next_action = next_action[:1000]
            if stall_risk is not None:
                proj.stall_risk = bool(stall_risk)
            if status is not None:
                proj.status = status
        db.commit()
        db.refresh(proj)
    except Exception as e:
        logger.error(f"track_project DB write failed: {e}", exc_info=True)
        db.rollback()
        return "Failed to save the project due to a database error."

    await push_workspace_update("WIDGET_PROJECTS", {"action": "changed"})
    verb = "Now tracking" if created else "Updated"
    tail = f" Next: {proj.next_action}." if proj.next_action else ""
    if proj.status == "done":
        return f"Marked '{proj.name}' as done. Nice work, Boss."
    return f"{verb} '{proj.name}'.{tail}"


async def _log_progress_from_tool(args: dict, db) -> str:
    name = (args.get("name") or "").strip()
    proj = _find_project(name, db)
    if proj is None:
        # Unknown project: start tracking it so momentum is captured going forward.
        return await _track_project_from_tool(args, db)
    try:
        proj.last_progress_at = utcnow()
        if args.get("next_action") is not None:
            proj.next_action = args["next_action"][:1000]
        if args.get("stall_risk") is not None:
            proj.stall_risk = bool(args["stall_risk"])
        proj.status = "active"
        db.commit()
        db.refresh(proj)
    except Exception as e:
        logger.error(f"log_progress DB write failed: {e}", exc_info=True)
        db.rollback()
        return "Failed to log progress due to a database error."
    await push_workspace_update("WIDGET_PROJECTS", {"action": "changed"})
    tail = f" Next up: {proj.next_action}." if proj.next_action else ""
    return f"Logged progress on '{proj.name}' — clock reset.{tail}"


# ── Habits & streaks ────────────────────────────────────────────────────────
# Grace tracks recurring habits; a check-in is one local day, and the streak is
# the run of consecutive days. She logs a "done", starts/stops tracking, and can
# nudge on ones still pending today.

def _build_habit_tool():
    return genai_types.Tool(
        function_declarations=[
            genai_types.FunctionDeclaration(
                name="track_habit",
                description=(
                    "Manage the operator's habits and streaks. Use action='done' "
                    "when he says he did a habit today (e.g. 'I worked out', 'did my "
                    "meditation', 'drank my water') — this logs today's check-in and "
                    "keeps the streak alive. Use action='add' to start tracking a new "
                    "habit, action='undo' to un-check today, and action='delete' to "
                    "stop tracking one. Match an existing habit by name "
                    "(case-insensitive). Opens/updates the Habits widget."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "action": genai_types.Schema(type=genai_types.Type.STRING, description="add | done | undo | delete"),
                        "name":   genai_types.Schema(type=genai_types.Type.STRING, description="The habit's name (e.g. 'workout', 'read 30 min')."),
                        "icon":   genai_types.Schema(type=genai_types.Type.STRING, description="Optional single emoji for the habit."),
                    },
                    required=["action", "name"],
                ),
            )
        ]
    )


async def _track_habit_from_tool(args: dict, db) -> str:
    action = (args.get("action") or "").strip().lower()
    name = (args.get("name") or "").strip()
    if not name:
        return "Which habit, Boss?"

    if action == "add":
        if habit_service.find(db, name):
            return f"You're already tracking '{name}'."
        h = habit_service.add_habit(db, name, "daily", args.get("icon") or "")
        await push_workspace_update("WIDGET_HABITS", {"action": "changed"})
        return f"Tracking '{h.name}' now — check it off each day and I'll build the streak. 🔥"

    habit = habit_service.find(db, name)
    if not habit:
        if action == "done":
            # He did something he's not tracking yet — start it AND log today.
            habit = habit_service.add_habit(db, name, "daily", args.get("icon") or "")
            streak = habit_service.check_in(db, habit)
            await push_workspace_update("WIDGET_HABITS", {"action": "changed"})
            return f"Started tracking '{habit.name}' and marked it done today — day {streak}. 🔥"
        return f"I'm not tracking a habit called '{name}' yet. Want me to start?"

    if action == "done":
        streak = habit_service.check_in(db, habit)
        await push_workspace_update("WIDGET_HABITS", {"action": "changed"})
        best = " — new personal best!" if streak >= (habit.longest_streak or 0) and streak > 1 else ""
        return f"Nice — '{habit.name}' done. That's a {streak}-day streak.{best} 🔥"
    if action == "undo":
        streak = habit_service.undo_today(db, habit)
        await push_workspace_update("WIDGET_HABITS", {"action": "changed"})
        return f"Un-checked '{habit.name}' for today. Streak's at {streak}."
    if action == "delete":
        habit_service.delete_habit(db, habit)
        await push_workspace_update("WIDGET_HABITS", {"action": "changed"})
        return f"Stopped tracking '{habit.name}'."
    return "Habit action must be add, done, undo, or delete."


# ── Widget / UI control ─────────────────────────────────────────────────────
# Friendly names Grace (or the operator) might use → the widget's DOM id.
_WIDGET_IDS = {
    "map": "w-map", "tactical map": "w-map",
    "tasks": "w-kanban", "task board": "w-kanban", "board": "w-kanban", "kanban": "w-kanban",
    "calendar": "w-calendar", "events": "w-calendar",
    "weather": "w-weather",
    "projects": "w-projects", "momentum": "w-projects",
    "music": "w-music",
    "system": "w-sys", "diagnostics": "w-sys", "sys": "w-sys",
    "code": "w-code", "code panel": "w-code",
    "files": "w-upload", "upload": "w-upload",
    "chat": "w-chat",
    "route": "directions-card", "directions": "directions-card", "traffic": "directions-card",
    "document": "w-doc", "document editor": "w-doc", "editor": "w-doc", "doc": "w-doc",
    "habits": "w-habits", "habit": "w-habits", "streaks": "w-habits", "streak": "w-habits",
}
_WIDGET_CHOICES = "map, tasks, calendar, weather, projects, music, system, code, files"


def _build_ui_tool():
    return genai_types.Tool(
        function_declarations=[
            genai_types.FunctionDeclaration(
                name="control_widgets",
                description=(
                    "Open or close widgets/panels on the operator's dashboard. Use "
                    "this WHENEVER he asks to open, show, close, hide, or clear any "
                    "widget, panel, or 'the board'. This is the ONLY way to actually "
                    "move his UI — never just claim a widget is open/closed without "
                    "calling it. action 'close_all' closes every open panel; 'close' "
                    "closes one; 'open' opens one. For 'close'/'open', set widget to "
                    f"one of: {_WIDGET_CHOICES}."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "action": genai_types.Schema(type=genai_types.Type.STRING, description="close_all, close, or open."),
                        "widget": genai_types.Schema(type=genai_types.Type.STRING, description=f"Which widget (for close/open): {_WIDGET_CHOICES}."),
                    },
                    required=["action"],
                ),
            )
        ]
    )


async def _control_widgets_from_tool(args: dict, db) -> str:
    action = (args.get("action") or "").strip().lower()
    widget = (args.get("widget") or "").strip().lower()

    if action in ("close_all", "closeall", "clear", "close everything"):
        await push_workspace_update("WIDGET_CONTROL", {"action": "close_all"})
        return "Done — closed every open widget on the board."

    if action not in ("close", "open", "show", "hide"):
        return "Widget action must be close_all, close, or open."
    if action == "show":
        action = "open"
    if action == "hide":
        action = "close"

    wid = _WIDGET_IDS.get(widget)
    if not wid:
        return (
            f"I'm not sure which widget you mean by '{widget}'. I can control: "
            f"{_WIDGET_CHOICES}."
        )
    await push_workspace_update("WIDGET_CONTROL", {"action": action, "widget_id": wid})
    return f"Done — {widget} widget {'opened' if action == 'open' else 'closed'}."


# ── Reminders & timers ──────────────────────────────────────────────────────
def _build_reminder_tool():
    return genai_types.Tool(
        function_declarations=[
            genai_types.FunctionDeclaration(
                name="set_reminder",
                description=(
                    "Set a one-off reminder or a countdown timer that you'll alert "
                    "him about when it's time. Use for 'remind me to X at/in Y' or "
                    "'set a timer for N minutes'. You KNOW the current date/time — "
                    "resolve clock times ('6pm', 'tomorrow 9am') to fire_at in "
                    "YYYY-MM-DD HH:MM (24h, local). For durations ('in 20 minutes', "
                    "'a 25 minute timer') use in_minutes instead. Set kind='timer' "
                    "for countdowns, otherwise 'reminder'."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "text":       genai_types.Schema(type=genai_types.Type.STRING, description="What to remind him about (e.g. 'text Virtue', 'focus timer')."),
                        "fire_at":    genai_types.Schema(type=genai_types.Type.STRING, description="Absolute time YYYY-MM-DD HH:MM (24h, local)."),
                        "in_minutes": genai_types.Schema(type=genai_types.Type.INTEGER, description="Minutes from now, for durations/timers."),
                        "kind":       genai_types.Schema(type=genai_types.Type.STRING, description="'reminder' or 'timer'."),
                    },
                    required=["text"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="cancel_reminder",
                description="Cancel a pending reminder or timer. Match by id if known, else by a phrase from its text.",
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "reminder_id": genai_types.Schema(type=genai_types.Type.INTEGER, description="Numeric id, if known."),
                        "query":       genai_types.Schema(type=genai_types.Type.STRING, description="Text to match the reminder to cancel."),
                    },
                ),
            ),
        ]
    )


def _pending_reminders(db):
    """Active (unfired) reminders/timers, soonest first."""
    try:
        return (
            db.query(models.Reminder)
            .filter(models.Reminder.fired == False)  # noqa: E712
            .order_by(models.Reminder.fire_at)
            .all()
        )
    except Exception:
        return []


def _build_reminder_snapshot(db):
    rows = _pending_reminders(db)
    if not rows:
        return ""
    lines = []
    for r in rows[:12]:
        when = r.fire_at.strftime("%b %d %I:%M %p").replace(" 0", " ") if r.fire_at else "?"
        lines.append(f"  - (id {r.id}, {r.kind}) {r.text} — at {when}")
    return (
        " Pending reminders/timers you've set for him (you'll alert him when each "
        "fires — don't claim to have reminded him early):\n" + "\n".join(lines)
    )


async def _set_reminder_from_tool(args: dict, db) -> str:
    text = (args.get("text") or "").strip()
    if not text:
        return "What should I remind you about, Boss?"
    kind = (args.get("kind") or "reminder").strip().lower()
    if kind not in ("reminder", "timer"):
        kind = "reminder"

    now = datetime.now()
    fire_at = None
    in_minutes = args.get("in_minutes")
    if in_minutes is not None:
        try:
            fire_at = now + timedelta(minutes=max(1, int(in_minutes)))
        except (ValueError, TypeError):
            fire_at = None
    if fire_at is None:
        fa = (args.get("fire_at") or "").strip()
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                fire_at = datetime.strptime(fa, fmt)
                break
            except ValueError:
                continue
    if fire_at is None:
        return "I couldn't work out when — give me a time (like 6pm) or a duration (like 20 minutes)."
    # If the resolved clock time already passed, assume the next occurrence.
    if fire_at < now - timedelta(seconds=30):
        fire_at = fire_at + timedelta(days=1)

    try:
        r = models.Reminder(text=text[:500], fire_at=fire_at, kind=kind)
        db.add(r)
        db.commit()
        db.refresh(r)
    except Exception as e:
        logger.error(f"set_reminder DB write failed: {e}", exc_info=True)
        db.rollback()
        return "Couldn't save that reminder — database hiccup."

    if kind == "timer":
        mins = max(1, int(round((fire_at - now).total_seconds() / 60)))
        return f"Timer set — {mins} minute{'s' if mins != 1 else ''}. I'll shout when it's up."
    when = fire_at.strftime("%I:%M %p").lstrip("0")
    day = "" if fire_at.date() == now.date() else f" on {fire_at.strftime('%b %d').replace(' 0', ' ')}"
    return f"Got it — I'll remind you to {text} at {when}{day}."


async def _cancel_reminder_from_tool(args: dict, db) -> str:
    rid = args.get("reminder_id")
    query = (args.get("query") or "").strip().lower()
    match = None
    if rid is not None:
        try:
            match = db.query(models.Reminder).filter(models.Reminder.id == int(rid)).first()
        except (ValueError, TypeError):
            match = None
    elif query:
        for r in _pending_reminders(db):
            if query in (r.text or "").lower():
                match = r
                break
    if not match:
        return "I couldn't find that reminder to cancel."
    txt = match.text
    try:
        db.delete(match)
        db.commit()
    except Exception:
        db.rollback()
        return "Couldn't cancel it — database hiccup."
    return f"Cancelled the reminder to {txt}."


# ── Notes / idea capture ────────────────────────────────────────────────────
_NOTE_CATEGORIES = {"dev", "creative", "personal", "general"}


def _build_notes_tool():
    return genai_types.Tool(
        function_declarations=[
            genai_types.FunctionDeclaration(
                name="save_note",
                description=(
                    "Capture a note or idea for him to keep. Use for 'note this', "
                    "'save this', 'jot down', 'remember this idea', a line of writing, "
                    "a thought worth keeping. Pick a category: 'dev' (code/projects), "
                    "'creative' (scripts, spoken-word, hooks, devotionals, villain "
                    "lines), 'personal' (life), or 'general'. This is NOT for tasks, "
                    "events, reminders, or durable facts about him — those have their "
                    "own tools."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "content":  genai_types.Schema(type=genai_types.Type.STRING, description="The note text to save."),
                        "category": genai_types.Schema(type=genai_types.Type.STRING, description="dev, creative, personal, or general."),
                    },
                    required=["content"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="recall_notes",
                description=(
                    "Look up his saved notes. Use for 'what notes do I have', 'show "
                    "my creative ideas', 'what hooks did I save'. Filter by category "
                    "and/or a search phrase."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "category": genai_types.Schema(type=genai_types.Type.STRING, description="Filter: dev, creative, personal, general."),
                        "query":    genai_types.Schema(type=genai_types.Type.STRING, description="Optional text to search note contents."),
                    },
                ),
            ),
            genai_types.FunctionDeclaration(
                name="delete_note",
                description="Delete a saved note. Match by id if known, else by a phrase from its content.",
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "note_id": genai_types.Schema(type=genai_types.Type.INTEGER, description="Numeric id, if known."),
                        "query":   genai_types.Schema(type=genai_types.Type.STRING, description="Text to match the note to delete."),
                    },
                ),
            ),
        ]
    )


async def _save_note_from_tool(args: dict, db) -> str:
    content = (args.get("content") or "").strip()
    if not content:
        return "Nothing to note — what should I jot down?"
    category = (args.get("category") or "general").strip().lower()
    if category not in _NOTE_CATEGORIES:
        category = "general"
    try:
        n = models.Note(content=content[:4000], category=category)
        db.add(n)
        db.commit()
        db.refresh(n)
    except Exception as e:
        logger.error(f"save_note DB write failed: {e}", exc_info=True)
        db.rollback()
        return "Couldn't save that note — database hiccup."
    return f"Saved to your {category} notes."


async def _recall_notes_from_tool(args: dict, db) -> str:
    category = (args.get("category") or "").strip().lower()
    query = (args.get("query") or "").strip().lower()
    q = db.query(models.Note)
    if category in _NOTE_CATEGORIES:
        q = q.filter(models.Note.category == category)
    rows = q.order_by(models.Note.created_at.desc()).all()
    if query:
        rows = [n for n in rows if query in (n.content or "").lower()]
    if not rows:
        where = f" in {category}" if category in _NOTE_CATEGORIES else ""
        return f"No notes found{where}."
    rows = rows[:15]
    lines = [f"[{n.category}] {n.content}" for n in rows]
    return "Here are the notes:\n" + "\n".join(f"- {l}" for l in lines)


async def _delete_note_from_tool(args: dict, db) -> str:
    nid = args.get("note_id")
    query = (args.get("query") or "").strip().lower()
    match = None
    if nid is not None:
        try:
            match = db.query(models.Note).filter(models.Note.id == int(nid)).first()
        except (ValueError, TypeError):
            match = None
    elif query:
        for n in db.query(models.Note).order_by(models.Note.created_at.desc()).all():
            if query in (n.content or "").lower():
                match = n
                break
    if not match:
        return "I couldn't find that note to delete."
    try:
        db.delete(match)
        db.commit()
    except Exception:
        db.rollback()
        return "Couldn't delete it — database hiccup."
    return "Deleted that note."


# ── Code workspace (read-only debugging) ────────────────────────────────────
def _build_code_tool():
    return genai_types.Tool(
        function_declarations=[
            genai_types.FunctionDeclaration(
                name="list_files",
                description=(
                    "List the files and folders in the operator's project (his "
                    "GRACE codebase). Use to browse the code, find a file, or see "
                    "what's in a folder. Path is relative to the project root; use "
                    "'.' for the root. READ-ONLY."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "path": genai_types.Schema(type=genai_types.Type.STRING, description="Folder path relative to project root, or '.' for root."),
                    },
                ),
            ),
            genai_types.FunctionDeclaration(
                name="read_code_file",
                description=(
                    "Read a source file so you can review it, explain it, or hunt "
                    "for bugs. Also opens it in his Code Panel. Give the path "
                    "relative to the project root (e.g. 'grace_backend/models.py'). "
                    "READ-ONLY — you cannot edit files, only read and advise."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "path": genai_types.Schema(type=genai_types.Type.STRING, description="File path relative to project root."),
                    },
                    required=["path"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="search_code",
                description=(
                    "Search the project's code for a string (like grep) to find "
                    "where something is defined or used. Returns matching file "
                    "paths and line numbers. READ-ONLY."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "query": genai_types.Schema(type=genai_types.Type.STRING, description="Text to search for."),
                        "path":  genai_types.Schema(type=genai_types.Type.STRING, description="Folder to search under, relative to root (default whole project)."),
                    },
                    required=["query"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="propose_edit",
                description=(
                    "Propose a fix to a file. This does NOT change the file — it "
                    "stages a diff the operator reviews and approves. Use when he "
                    "asks you to fix, patch, or change code. First read_code_file "
                    "to see the CURRENT exact text. Give old_code = the exact block "
                    "to replace (copied verbatim, with enough surrounding lines to "
                    "be UNIQUE in the file) and new_code = the corrected block. One "
                    "logical change per call. Never guess the file's contents — "
                    "always read it first."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "path":     genai_types.Schema(type=genai_types.Type.STRING, description="File path relative to project root."),
                        "old_code": genai_types.Schema(type=genai_types.Type.STRING, description="Exact existing code block to replace (must be unique in the file)."),
                        "new_code": genai_types.Schema(type=genai_types.Type.STRING, description="The corrected code to put in its place."),
                        "summary":  genai_types.Schema(type=genai_types.Type.STRING, description="One-line description of the fix."),
                    },
                    required=["path", "old_code", "new_code"],
                ),
            ),
        ]
    )


async def _list_files_from_tool(args: dict, db) -> str:
    res = code_workspace.list_dir(args.get("path") or ".")
    if "error" in res:
        return res["error"]
    parts = [f"Contents of /{res['path']}:"]
    if res["dirs"]:
        parts.append("Folders: " + ", ".join(d.split("/")[-1] for d in res["dirs"]))
    if res["files"]:
        parts.append("Files: " + ", ".join(f.split("/")[-1] for f in res["files"]))
    if not res["dirs"] and not res["files"]:
        parts.append("(empty)")
    return "\n".join(parts)


async def _read_code_file_from_tool(args: dict, db) -> str:
    res = code_workspace.read_file(args.get("path") or "")
    if "error" in res:
        return res["error"]
    # Show it in the Code Panel for him to see alongside your analysis.
    await push_workspace_update(
        "WIDGET_CODE",
        {"action": "open_file", "filename": res["name"], "content": res["content"]},
    )
    trunc = "\n[...file truncated...]" if res.get("truncated") else ""
    return (
        f"File {res['path']} (opened in the Code Panel). Contents:\n"
        f"```{res['ext']}\n{res['content']}{trunc}\n```"
    )


async def _search_code_from_tool(args: dict, db) -> str:
    res = code_workspace.search(args.get("query") or "", args.get("path") or ".")
    if "error" in res:
        return res["error"]
    if not res["hits"]:
        return f"No matches for '{res['query']}'."
    lines = [f"  {h['path']}:{h['line']}: {h['text']}" for h in res["hits"]]
    cap = "\n(more matches exist — narrow the search)" if res.get("capped") else ""
    return f"Matches for '{res['query']}':\n" + "\n".join(lines) + cap


async def _propose_edit_from_tool(args: dict, db) -> str:
    path = args.get("path") or ""
    old_code = args.get("old_code") or ""
    new_code = args.get("new_code") or ""
    summary = args.get("summary") or ""

    # Route: a real project file → edit on disk (Apply writes). Otherwise, if a
    # text file is attached, edit that in memory (Apply downloads the fix).
    doc = get_active_doc()
    doc_is_text = bool(doc and doc.get("kind") == "text" and doc.get("text"))
    if doc_is_text and (not path or not code_workspace.is_project_file(path)
                        or os.path.basename(path) == doc.get("name")):
        res = code_workspace.stage_edit_content(doc["name"], doc["text"], old_code, new_code, summary)
    else:
        res = code_workspace.stage_edit(path, old_code, new_code, summary)
    if "error" in res:
        return res["error"]

    packet = {
        "action": "propose_edit",
        "edit_id": res["edit_id"],
        "path": res["path"],
        "summary": res.get("summary") or "",
        "diff": res["diff"],
        "kind": res.get("kind", "project"),
    }
    if res.get("kind") == "upload":
        packet["filename"] = res["filename"]
        packet["new_content"] = res["new_content"]
    await push_workspace_update("WIDGET_CODE", packet)

    if res.get("kind") == "upload":
        return (
            f"Staged a fix for {res['path']} — review the diff up top. Since it's an "
            "uploaded file, hitting Apply downloads the corrected version for you to "
            "drop back into your project. Reject tosses it."
        )
    return (
        f"Staged a fix for {res['path']} — I've put the diff up for you to review. "
        "Nothing's changed yet; hit Apply if it looks right, or Reject to toss it."
    )


# ── Shodan host lookup (OSINT) ──────────────────────────────────────────────
def _build_shodan_tool():
    return genai_types.Tool(
        function_declarations=[
            genai_types.FunctionDeclaration(
                name="shodan_lookup",
                description=(
                    "Look up what's publicly exposed on a host using Shodan — open "
                    "ports, running services, org/ISP, geolocation, and known CVEs, "
                    "and pin its location on the map. ALWAYS use this for ANY request "
                    "to profile / look up / locate / find where something is hosted / "
                    "get details on an IP or domain (e.g. 'profile google.com', "
                    "'where is X hosted', 'details on this domain', 'what ports are "
                    "open on X') — including well-known domains. Do NOT answer such "
                    "requests from memory; call this for the real data. Passive lookup "
                    "of Shodan's index — nothing is sent to the target."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "target": genai_types.Schema(type=genai_types.Type.STRING, description="The IP address or domain to look up."),
                    },
                    required=["target"],
                ),
            )
        ]
    )


async def _shodan_lookup_from_tool(args: dict, db) -> str:
    target = args.get("target") or ""

    # Base layer: free geolocation + hosting intel — always works, no paywall.
    geo = await ip_intel.geo_lookup(target)
    if "error" in geo:
        return geo["error"]

    # Pin the host's location on the tactical map.
    if geo.get("lat") is not None and geo.get("lon") is not None:
        title = f"{geo['target']} · {geo.get('org') or geo.get('isp') or geo['ip']}"
        await push_workspace_update(
            "WIDGET_MAP",
            {"action": "pin", "lat": geo["lat"], "lon": geo["lon"], "title": title},
        )

    report = ip_intel.format_for_model(geo)

    # Enrichment layer: Shodan ports/services/CVEs — only if the key can reach it.
    if shodan_lookup.is_configured():
        sh = await shodan_lookup.host_lookup(target)
        if "error" not in sh:
            extras = []
            if sh.get("ports"):
                extras.append("- Open ports: " + ", ".join(str(p) for p in sh["ports"]))
            if sh.get("services"):
                svc = []
                for s in sh["services"]:
                    lbl = str(s["port"])
                    if s.get("product"):
                        lbl += f" {s['product']}" + (f" {s['version']}" if s.get("version") else "")
                    svc.append(lbl)
                extras.append("- Services: " + "; ".join(svc))
            if sh.get("vulns"):
                extras.append(f"- KNOWN CVEs ({len(sh['vulns'])}): " + ", ".join(sh["vulns"][:15]))
            if extras:
                report += "\n[Shodan exposure]\n" + "\n".join(extras)
        elif "membership" in sh["error"].lower():
            report += ("\n(Open-ports/CVE data needs a paid Shodan Membership — the free "
                       "plan blocks host lookups, so I've given you the free hosting intel above.)")

    return report


# ── Directions / traffic ETA (TomTom) ───────────────────────────────────────
def _build_directions_tool():
    return genai_types.Tool(
        function_declarations=[
            genai_types.FunctionDeclaration(
                name="get_directions",
                description=(
                    "Get driving directions between two places with LIVE traffic — "
                    "distance, current travel time/ETA, traffic delay, and up to 2 "
                    "alternative routes — and draw them on the tactical map. Use "
                    "whenever he asks how long a trip takes, the traffic between two "
                    "places, directions/route, or when to LEAVE to arrive by a time "
                    "(e.g. 'how long from Ikeja to VI', 'when should I leave Lekki to "
                    "reach Yaba by 3pm'). You know the current date/time — for an "
                    "arrival goal, put it in arrive_by as YYYY-MM-DD HH:MM (24h local)."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "origin":      genai_types.Schema(type=genai_types.Type.STRING, description="Starting place (address, area, or landmark)."),
                        "destination": genai_types.Schema(type=genai_types.Type.STRING, description="Destination place."),
                        "arrive_by":   genai_types.Schema(type=genai_types.Type.STRING, description="Optional target arrival time, YYYY-MM-DD HH:MM (24h, local)."),
                    },
                    required=["origin", "destination"],
                ),
            )
        ]
    )


def _build_document_tool():
    return genai_types.Tool(
        function_declarations=[
            genai_types.FunctionDeclaration(
                name="edit_document",
                description=(
                    "Edit the document currently OPEN in the document editor (an "
                    "uploaded .txt/.docx the operator is working on — its full text is "
                    "in your context). Use when he asks to change, fix, replace, "
                    "reword, or clean up part of the open document. The edit applies "
                    "LIVE in the editor. Provide EITHER find+replace for a targeted "
                    "change, OR new_text to replace the entire document. Do not use "
                    "this for the project's code files — that's propose_edit."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "find":     genai_types.Schema(type=genai_types.Type.STRING, description="Exact text to find and replace (for a targeted edit)."),
                        "replace":  genai_types.Schema(type=genai_types.Type.STRING, description="Replacement text for `find`. Empty string deletes it."),
                        "new_text": genai_types.Schema(type=genai_types.Type.STRING, description="Full new document text (replaces everything). Use for big rewrites."),
                        "summary":  genai_types.Schema(type=genai_types.Type.STRING, description="Short note on what you changed."),
                    },
                ),
            ),
            genai_types.FunctionDeclaration(
                name="create_document",
                description=(
                    "Create a NEW downloadable document from text you provide — e.g. a "
                    "summary, rewrite, extract, or translation of the open document, or "
                    "a fresh document he asked you to write. This generates the file and "
                    "pops a 'Document ready' download card for him. Put the FULL final "
                    "text in `content`. Format defaults to the open document's format."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "content":  genai_types.Schema(type=genai_types.Type.STRING, description="The complete text of the new document."),
                        "filename": genai_types.Schema(type=genai_types.Type.STRING, description="Suggested name (e.g. 'summary'). Extension optional."),
                        "format":   genai_types.Schema(type=genai_types.Type.STRING, description="Output format: 'docx', 'pdf', or 'txt'. Omit to match the open document."),
                    },
                    required=["content"],
                ),
            ),
        ]
    )


async def _edit_document_from_tool(args: dict, db) -> str:
    doc = get_active_doc()
    if not doc or not doc.get("editable") or doc.get("text") is None:
        return ("There's no editable document open, Boss — upload a .txt or .docx "
                "first and I'll edit it in the document editor.")
    text = doc.get("text") or ""
    new_text = args.get("new_text")
    find = args.get("find")
    replace = args.get("replace") or ""

    if new_text is not None and new_text != "":
        updated = new_text
    elif find:
        if find not in text:
            return (f"I couldn't find that exact text in the document to change it. "
                    f"Try quoting the passage exactly as it appears.")
        updated = text.replace(find, replace)
    else:
        return "Tell me what to change — either the text to find, or the full new version."

    doc["text"] = updated
    await push_workspace_update("WIDGET_DOC", {
        "action": "replace",
        "name": doc.get("name", "document"),
        "text": updated,
        "summary": args.get("summary") or "",
    })
    return (f"Done — updated the open document ({args.get('summary') or 'edit applied'}). "
            "It's live in the editor; Ctrl+Z undoes it if needed.")


async def _create_document_from_tool(args: dict, db) -> str:
    content = args.get("content") or ""
    if not content.strip():
        return "There's nothing to put in the new document — give me the text first."
    doc = get_active_doc()
    default_fmt = (doc or {}).get("fmt") or "txt"
    fmt = (args.get("format") or default_fmt).lower()
    base = args.get("filename") or "document"
    built = documents.build_document(content, fmt, base)
    await push_workspace_update("WIDGET_DOC", {
        "action": "ready",
        "filename": built["filename"],
        "url": f"/api/doc/download/{built['id']}",
    })
    return (f"Your document '{built['filename']}' is ready, Boss — the download card is "
            "up top. Click it to save the file.")


def _build_desktop_tool():
    """OS-control tools — only offered in the desktop app (GRACE_DESKTOP=1)."""
    return genai_types.Tool(
        function_declarations=[
            genai_types.FunctionDeclaration(
                name="open_path",
                description=(
                    "Open a file or folder on the operator's computer with its default "
                    "app / File Explorer. Use for 'open my Downloads folder', 'open "
                    "report.pdf', 'show me that file'. Accepts ~, %VARS%, and absolute "
                    "paths. This just opens it — safe, no confirmation needed."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "path": genai_types.Schema(type=genai_types.Type.STRING, description="File or folder path (e.g. '~/Downloads', 'C:/Users/me/report.pdf')."),
                    },
                    required=["path"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="open_with",
                description=(
                    "Open a file with a SPECIFIC application the operator names — e.g. "
                    "'open this with Notepad', 'open it in VS Code', 'open the pdf in "
                    "Chrome'. Use this whenever he says which app to use (including after "
                    "Windows asks him how to open a file). Knows common apps: notepad, "
                    "wordpad, word, excel, paint, vscode/code, chrome, edge, firefox, vlc. "
                    "Safe, no confirmation needed."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "path": genai_types.Schema(type=genai_types.Type.STRING, description="File to open."),
                        "app":  genai_types.Schema(type=genai_types.Type.STRING, description="App to open it with (name or executable, e.g. 'notepad', 'code', 'chrome')."),
                    },
                    required=["path", "app"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="list_directory",
                description=(
                    "List the files and folders inside a directory on his computer. Use "
                    "to see what's in a folder before acting. Read-only, safe."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "path": genai_types.Schema(type=genai_types.Type.STRING, description="Folder path (default his home folder)."),
                    },
                ),
            ),
            genai_types.FunctionDeclaration(
                name="read_local_file",
                description=(
                    "Read the text contents of a file on his computer (so you can look at "
                    "or edit it). Read-only, safe. For big files you get the first part."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "path": genai_types.Schema(type=genai_types.Type.STRING, description="File path to read."),
                    },
                    required=["path"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="write_local_file",
                description=(
                    "Create or overwrite a file on his computer with the given text. Use "
                    "to save/edit a local file. This MUTATES his disk, so it pops an "
                    "Apply/Reject card for him to approve first (unless the folder is "
                    "already trusted). An existing file is backed up (.grace.bak) before "
                    "overwrite. Put the FULL final contents in `content`."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "path":    genai_types.Schema(type=genai_types.Type.STRING, description="File path to write."),
                        "content": genai_types.Schema(type=genai_types.Type.STRING, description="The complete text to write into the file."),
                    },
                    required=["path", "content"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="run_command",
                description=(
                    "Run a shell command on his computer (developer automation — e.g. "
                    "'git pull', 'npm install', start a dev server, open an app). This can "
                    "do anything, so it pops an Apply/Reject card for him to approve first "
                    "(unless the working folder is trusted). Keep commands precise. You "
                    "get stdout/stderr and the exit code back."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "command": genai_types.Schema(type=genai_types.Type.STRING, description="The exact command line to run."),
                        "cwd":     genai_types.Schema(type=genai_types.Type.STRING, description="Optional folder to run it in."),
                    },
                    required=["command"],
                ),
            ),
        ]
    )


def _build_system_tool():
    """Whole-computer control — desktop app only (GRACE_DESKTOP=1)."""
    return genai_types.Tool(
        function_declarations=[
            genai_types.FunctionDeclaration(
                name="system_power",
                description=(
                    "Control the computer's power/session: shut down, restart, sleep, "
                    "hibernate, log off, lock the screen, or cancel a pending shutdown. "
                    "Shutdown/restart run after a short delay you can cancel — tell him "
                    "he can say 'cancel' to stop it. Use delay_minutes for 'shut down in "
                    "30 minutes'."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "action": genai_types.Schema(type=genai_types.Type.STRING, description="shutdown | restart | sleep | hibernate | logoff | lock | cancel"),
                        "delay_minutes": genai_types.Schema(type=genai_types.Type.INTEGER, description="Optional delay before shutdown/restart (minutes)."),
                    },
                    required=["action"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="media_control",
                description=(
                    "Control sound and media playback: volume_up, volume_down, set_volume "
                    "(with level 0-100), mute, play_pause, next, previous. Works with "
                    "Spotify, YouTube, any media app."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "action": genai_types.Schema(type=genai_types.Type.STRING, description="volume_up | volume_down | set_volume | mute | play_pause | next | previous"),
                        "level":  genai_types.Schema(type=genai_types.Type.INTEGER, description="For set_volume: 0-100."),
                    },
                    required=["action"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="take_screenshot",
                description=(
                    "Capture the current screen. Saves the image and loads it so you can "
                    "look at it — after taking it, if he asks what's on screen, describe "
                    "it from the captured image."
                ),
                parameters=genai_types.Schema(type=genai_types.Type.OBJECT, properties={}),
            ),
            genai_types.FunctionDeclaration(
                name="display_control",
                description=(
                    "Screen/monitor actions: monitor_off (turn display off), show_desktop "
                    "(minimise everything), extend / duplicate / external / internal "
                    "(second-monitor mode)."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={"action": genai_types.Schema(type=genai_types.Type.STRING, description="monitor_off | show_desktop | extend | duplicate | external | internal")},
                    required=["action"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="app_control",
                description=(
                    "Launch, close, or focus apps and windows. 'launch' opens an app; "
                    "'focus' brings its window to front; 'close' force-quits the whole app "
                    "by process (e.g. 'close Chrome'); 'close_window' gracefully closes a "
                    "SPECIFIC window by its title — use this to close ONE File Explorer "
                    "folder window (e.g. 'close my Downloads folder' → name 'Downloads') or "
                    "one browser/app window, WITHOUT killing the whole app. Prefer "
                    "close_window for 'close this folder/window'; use close only to fully "
                    "quit an app."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "action": genai_types.Schema(type=genai_types.Type.STRING, description="launch | focus | close | close_window"),
                        "name":   genai_types.Schema(type=genai_types.Type.STRING, description="App name (spotify, chrome, ...) for launch/focus/close, OR the window/folder TITLE (e.g. 'Downloads') for close_window."),
                    },
                    required=["action", "name"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="list_processes",
                description="List the apps/processes currently running, by memory use. Use for 'what's running' / 'what's eating my memory'.",
                parameters=genai_types.Schema(type=genai_types.Type.OBJECT, properties={}),
            ),
            genai_types.FunctionDeclaration(
                name="find_file",
                description="Search his computer for files whose name contains the text. Use for 'find my tax pdf', 'where's my resume'. Searches his home folder by default.",
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "name": genai_types.Schema(type=genai_types.Type.STRING, description="Text in the file name to look for."),
                        "root": genai_types.Schema(type=genai_types.Type.STRING, description="Optional folder to search under (default his home folder)."),
                    },
                    required=["name"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="file_operation",
                description=(
                    "Create/rename/move/copy/delete files or folders, or empty the recycle "
                    "bin. create_folder happens right away; delete/move/rename/copy/"
                    "empty_recycle_bin pop an Apply/Reject card first (delete goes to the "
                    "Recycle Bin, recoverable)."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "op":   genai_types.Schema(type=genai_types.Type.STRING, description="create_folder | delete | move | rename | copy | empty_recycle_bin"),
                        "path": genai_types.Schema(type=genai_types.Type.STRING, description="Target file/folder path."),
                        "dest": genai_types.Schema(type=genai_types.Type.STRING, description="Destination / new name (for move/rename/copy)."),
                    },
                    required=["op"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="clipboard",
                description="Read or set the Windows clipboard. action 'get' returns what's on it; 'set' copies your `text` onto it.",
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "action": genai_types.Schema(type=genai_types.Type.STRING, description="get | set"),
                        "text":   genai_types.Schema(type=genai_types.Type.STRING, description="For 'set': the text to copy."),
                    },
                    required=["action"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="type_text",
                description=(
                    "Type text into whatever window currently has focus (as if typed on "
                    "the keyboard). He must click into the target field/app first. Use for "
                    "'type my address', 'fill this in'."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={"text": genai_types.Schema(type=genai_types.Type.STRING, description="The text to type.")},
                    required=["text"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="system_status",
                description="Report battery, CPU, RAM, free disk space, uptime, and IP address. Use for 'what's my battery', 'how much space is left', 'what's my IP'.",
                parameters=genai_types.Schema(type=genai_types.Type.OBJECT, properties={}),
            ),
        ]
    )


def _build_extra_tool():
    """Web, network, display & handy extras — desktop app only."""
    return genai_types.Tool(
        function_declarations=[
            genai_types.FunctionDeclaration(
                name="web",
                description=(
                    "Do something on the web in his browser. 'open' opens a website "
                    "(url or domain), 'search' googles the query, 'play' plays a song/"
                    "video/artist on YouTube (default) or Spotify. e.g. 'open youtube.com', "
                    "'search cheap flights to Lagos', 'play Burna Boy on Spotify'."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "action":  genai_types.Schema(type=genai_types.Type.STRING, description="open | search | play"),
                        "query":   genai_types.Schema(type=genai_types.Type.STRING, description="URL/domain for open, search terms, or what to play."),
                        "service": genai_types.Schema(type=genai_types.Type.STRING, description="For play: 'youtube' (default) or 'spotify'."),
                    },
                    required=["action", "query"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="network",
                description=(
                    "Network & WiFi: online (am I connected?), public_ip, flush_dns, "
                    "wifi_status (current network + signal), wifi_password (of a network), "
                    "list_networks, wifi_connect (to `name`), wifi_disconnect, wifi_on, "
                    "wifi_off. Toggling the adapter (wifi_on/off) may need admin."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "action": genai_types.Schema(type=genai_types.Type.STRING, description="online | public_ip | flush_dns | wifi_status | wifi_password | list_networks | wifi_connect | wifi_disconnect | wifi_on | wifi_off"),
                        "name":   genai_types.Schema(type=genai_types.Type.STRING, description="Network name (for wifi_connect / wifi_password of a specific network)."),
                    },
                    required=["action"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="brightness",
                description="Adjust screen brightness (laptops): up, down, or set (with level 0-100). Desktop monitors may not support this.",
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "action": genai_types.Schema(type=genai_types.Type.STRING, description="up | down | set"),
                        "level":  genai_types.Schema(type=genai_types.Type.INTEGER, description="For set: 0-100."),
                    },
                    required=["action"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="window_control",
                description=(
                    "Manage a window by its title: minimize, maximize, restore, snap_left, "
                    "snap_right, always_on_top, unpin. e.g. 'maximize Chrome', 'snap this "
                    "to the left', 'keep Notepad on top'. For monitor duplicate/extend use "
                    "display_control (extend/duplicate/external/internal)."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "action": genai_types.Schema(type=genai_types.Type.STRING, description="minimize | maximize | restore | snap_left | snap_right | always_on_top | unpin"),
                        "title":  genai_types.Schema(type=genai_types.Type.STRING, description="Part of the window's title (app or folder name)."),
                    },
                    required=["action", "title"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="archive",
                description="Zip or unzip. 'zip' compresses a file/folder into a .zip; 'unzip' extracts a .zip. Safe, no confirmation.",
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "op":   genai_types.Schema(type=genai_types.Type.STRING, description="zip | unzip"),
                        "path": genai_types.Schema(type=genai_types.Type.STRING, description="File/folder to zip, or the .zip to extract."),
                        "dest": genai_types.Schema(type=genai_types.Type.STRING, description="Optional output path/folder."),
                    },
                    required=["op", "path"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="reveal_file",
                description="Open File Explorer with a file selected/highlighted — 'show me where X is'. Safe.",
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={"path": genai_types.Schema(type=genai_types.Type.STRING, description="The file/folder to reveal.")},
                    required=["path"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="system_utility",
                description=(
                    "Maintenance: restart_explorer (fix a frozen taskbar), clear_temp "
                    "(delete temp files), or list_installed (installed programs)."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={"action": genai_types.Schema(type=genai_types.Type.STRING, description="restart_explorer | clear_temp | list_installed")},
                    required=["action"],
                ),
            ),
        ]
    )


def _build_final_tool():
    """Print, file info, personalization, image/PDF, scheduling — desktop only."""
    return genai_types.Tool(
        function_declarations=[
            genai_types.FunctionDeclaration(
                name="file_details",
                description=(
                    "File/folder utilities: 'print' sends a file to the default printer; "
                    "'info' reports size/date/type (folder size + file count for a folder); "
                    "'recent' lists the most recently changed files in a folder (default "
                    "Downloads). e.g. 'print this', 'how big is my Downloads folder', "
                    "'show my recent downloads'."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "action": genai_types.Schema(type=genai_types.Type.STRING, description="print | info | recent"),
                        "path":   genai_types.Schema(type=genai_types.Type.STRING, description="File to print/inspect, or folder for info/recent."),
                    },
                    required=["action"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="batch_rename",
                description=(
                    "Rename all files in a folder to prefix_1, prefix_2, … (keeps each "
                    "file's extension). Optionally only files of one type. Pops an "
                    "Apply/Reject card first."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "folder": genai_types.Schema(type=genai_types.Type.STRING, description="Folder whose files to rename."),
                        "prefix": genai_types.Schema(type=genai_types.Type.STRING, description="New base name, e.g. 'invoice'."),
                        "ext":    genai_types.Schema(type=genai_types.Type.STRING, description="Optional: only rename this extension (e.g. 'jpg')."),
                    },
                    required=["folder", "prefix"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="close_top_memory",
                description="Find and close the app using the most memory (excludes system-critical processes). Pops an Apply/Reject card naming the app first.",
                parameters=genai_types.Schema(type=genai_types.Type.OBJECT, properties={}),
            ),
            genai_types.FunctionDeclaration(
                name="personalize",
                description=(
                    "Personalization & settings: set the desktop wallpaper to an image "
                    "(action 'wallpaper', value = image path), switch Windows theme (action "
                    "'dark' or 'light'), or change the power plan (action 'power_plan', "
                    "value = 'high performance' | 'balanced' | 'power saver')."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "action": genai_types.Schema(type=genai_types.Type.STRING, description="wallpaper | dark | light | power_plan"),
                        "value":  genai_types.Schema(type=genai_types.Type.STRING, description="Image path (wallpaper) or plan name (power_plan)."),
                    },
                    required=["action"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="image_tool",
                description=(
                    "Edit an image: 'resize' (value = percent like '50' or 'WxH' like "
                    "'800x600'), 'convert' (value = target format like 'jpg'/'png'), or "
                    "'compress' (value = quality 1-95). Saves a new file."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "op":    genai_types.Schema(type=genai_types.Type.STRING, description="resize | convert | compress"),
                        "path":  genai_types.Schema(type=genai_types.Type.STRING, description="Image file."),
                        "value": genai_types.Schema(type=genai_types.Type.STRING, description="Percent/dimensions, format, or quality."),
                        "dest":  genai_types.Schema(type=genai_types.Type.STRING, description="Optional output path."),
                    },
                    required=["op", "path"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="pdf_tool",
                description=(
                    "Work with PDFs: 'merge' (combine two PDFs — path + path2, or a "
                    "comma-separated list in path), 'extract' (pull pages, e.g. pages "
                    "'2-5' or '1,3,5'), or 'split' (one file per page). Saves new file(s)."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "op":    genai_types.Schema(type=genai_types.Type.STRING, description="merge | extract | split"),
                        "path":  genai_types.Schema(type=genai_types.Type.STRING, description="The PDF (or first PDF / comma list for merge)."),
                        "path2": genai_types.Schema(type=genai_types.Type.STRING, description="Second PDF for merge."),
                        "pages": genai_types.Schema(type=genai_types.Type.STRING, description="Pages for extract, e.g. '2-5' or '1,3,5'."),
                        "dest":  genai_types.Schema(type=genai_types.Type.STRING, description="Optional output path."),
                    },
                    required=["op", "path"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="schedule_task",
                description=(
                    "Schedule a command with Windows Task Scheduler. 'create' needs a "
                    "command and a time (HH:MM, 24h); repeat 'once' or 'daily'. e.g. shut "
                    "down at 23:00 → command 'shutdown /s /t 0', time '23:00'. Also "
                    "'list' or 'delete' (by name). Creating a task may need admin rights."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "action":  genai_types.Schema(type=genai_types.Type.STRING, description="create | list | delete"),
                        "name":    genai_types.Schema(type=genai_types.Type.STRING, description="A short task name."),
                        "command": genai_types.Schema(type=genai_types.Type.STRING, description="The command line to run at the time."),
                        "time":    genai_types.Schema(type=genai_types.Type.STRING, description="Time as HH:MM (24-hour)."),
                        "repeat":  genai_types.Schema(type=genai_types.Type.STRING, description="once (default) | daily"),
                    },
                    required=["action"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="proactive_alerts",
                description=(
                    "Turn Grace's proactive desktop alerts on/off (she reaches out on low "
                    "battery, near-full disk, finished downloads, and long-session break "
                    "nudges). action 'enable'/'disable' with which alert; 'status' lists "
                    "them. e.g. 'stop reminding me about breaks' → disable break."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "action":  genai_types.Schema(type=genai_types.Type.STRING, description="enable | disable | status"),
                        "setting": genai_types.Schema(type=genai_types.Type.STRING, description="battery | disk | downloads | break | all"),
                    },
                    required=["action"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="confirm_actions",
                description=(
                    "Turn the Apply/Reject confirmation on or off for risky actions (run "
                    "command, write/delete files). It's currently OFF — she just does what "
                    "he says. Use 'on' if he asks to be asked first ('ask me before you run "
                    "things'), 'off' to go back to just-do-it."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={"enabled": genai_types.Schema(type=genai_types.Type.BOOLEAN, description="true = ask first; false = just do it.")},
                    required=["enabled"],
                ),
            ),
        ]
    )


def _build_google_tool():
    return genai_types.Tool(
        function_declarations=[
            genai_types.FunctionDeclaration(
                name="check_email",
                description=(
                    "Check the operator's Gmail. With no query, returns his most "
                    "recent unread emails (sender + subject). With a query, searches "
                    "his mail using Gmail search syntax (e.g. 'from:zenith', "
                    "'invoice', 'from:linkedin newer_than:7d'). Use when he asks about "
                    "new mail, whether something arrived, or to find an email. This "
                    "only lists them — to read/summarise a specific one, use read_email."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "query": genai_types.Schema(type=genai_types.Type.STRING, description="Optional Gmail search query. Omit for recent unread."),
                    },
                ),
            ),
            genai_types.FunctionDeclaration(
                name="read_email",
                description=(
                    "Open and read the FULL text of one specific email so you can "
                    "summarise it or answer questions about it. Give a query that "
                    "identifies it (e.g. 'from:zenith poster', or the subject). Use "
                    "when he asks what an email says, or to summarise a message."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "query": genai_types.Schema(type=genai_types.Type.STRING, description="Gmail search that identifies the one email to read."),
                    },
                    required=["query"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="google_calendar",
                description=(
                    "Read the operator's real Google Calendar — upcoming events over "
                    "the next `days` days. Use when he asks what's on his schedule, "
                    "what's coming up, or about his Google Calendar."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "days": genai_types.Schema(type=genai_types.Type.INTEGER, description="How many days ahead to look (default 14)."),
                    },
                ),
            ),
            genai_types.FunctionDeclaration(
                name="draft_email",
                description=(
                    "Compose an email for the operator to review and send. This does "
                    "NOT send it — it shows him a preview card with a Send button, and "
                    "he approves (or edits) before anything goes out. Use when he asks "
                    "you to email/reply to someone. Write a complete, well-phrased "
                    "message in his voice. To attach a document you just made with "
                    "create_document (e.g. 'summarise this and email it'), or the file "
                    "open in the editor, set attach=true."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "to":      genai_types.Schema(type=genai_types.Type.STRING, description="Recipient — an email address, OR a person's name to look up in his Google Contacts."),
                        "subject": genai_types.Schema(type=genai_types.Type.STRING, description="Email subject."),
                        "body":    genai_types.Schema(type=genai_types.Type.STRING, description="The full email body."),
                        "attach":  genai_types.Schema(type=genai_types.Type.BOOLEAN, description="Attach the document just created / open in the editor. Default false."),
                    },
                    required=["to", "subject", "body"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="create_calendar_event",
                description=(
                    "Add an event to the operator's real Google Calendar (syncs to his "
                    "phone) and optionally INVITE people. Use when he asks to "
                    "schedule/add a meeting or event, including 'with X and Y'. You know "
                    "the current date/time — resolve 'Friday 3pm' etc. to an exact start. "
                    "Times are his local time. attendees may be email addresses OR names — "
                    "names are looked up in his Google Contacts automatically."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "summary":     genai_types.Schema(type=genai_types.Type.STRING, description="Event title."),
                        "start":       genai_types.Schema(type=genai_types.Type.STRING, description="Start as 'YYYY-MM-DD HH:MM' (24h, local)."),
                        "end":         genai_types.Schema(type=genai_types.Type.STRING, description="Optional end 'YYYY-MM-DD HH:MM'; defaults to start + 1 hour."),
                        "location":    genai_types.Schema(type=genai_types.Type.STRING, description="Optional location."),
                        "description": genai_types.Schema(type=genai_types.Type.STRING, description="Optional notes."),
                        "attendees":   genai_types.Schema(type=genai_types.Type.ARRAY, items=genai_types.Schema(type=genai_types.Type.STRING), description="Optional people to invite — email addresses or names (names are resolved via his Google Contacts)."),
                    },
                    required=["summary", "start"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="manage_email",
                description=(
                    "Tidy the operator's inbox — mark as read, archive, or trash all "
                    "emails matching a Gmail search query. Use for 'archive those "
                    "newsletters', 'mark everything from LinkedIn as read', 'trash the "
                    "promotions'. Give a precise Gmail query so you only touch the right "
                    "mail. Trash is recoverable (goes to Trash)."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "action": genai_types.Schema(type=genai_types.Type.STRING, description="mark_read | archive | trash"),
                        "query":  genai_types.Schema(type=genai_types.Type.STRING, description="Gmail search query (e.g. 'from:linkedin', 'category:promotions is:unread')."),
                    },
                    required=["action", "query"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="update_calendar_event",
                description=(
                    "Reschedule an existing Google Calendar event to a new time. Finds "
                    "the soonest upcoming event whose title matches `query`. Use for "
                    "'move my standup to 4pm', 'reschedule the client call to Friday 2pm'."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "query":     genai_types.Schema(type=genai_types.Type.STRING, description="Words from the event's title to find it."),
                        "new_start": genai_types.Schema(type=genai_types.Type.STRING, description="New start 'YYYY-MM-DD HH:MM' (local)."),
                        "new_end":   genai_types.Schema(type=genai_types.Type.STRING, description="Optional new end; defaults to keeping the original duration."),
                    },
                    required=["query", "new_start"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="cancel_calendar_event",
                description=(
                    "Cancel/delete an upcoming Google Calendar event (and notify guests). "
                    "Finds the soonest upcoming event whose title matches `query`. Use for "
                    "'cancel the standup', 'delete my 3pm meeting'."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "query": genai_types.Schema(type=genai_types.Type.STRING, description="Words from the event's title to find it."),
                    },
                    required=["query"],
                ),
            ),
            genai_types.FunctionDeclaration(
                name="check_availability",
                description=(
                    "Check whether the operator is free in a time window on his Google "
                    "Calendar. Use for 'am I free Thursday afternoon?', 'do I have "
                    "anything at 3pm tomorrow?'. You know today's date — resolve the "
                    "window to exact start/end."
                ),
                parameters=genai_types.Schema(
                    type=genai_types.Type.OBJECT,
                    properties={
                        "start": genai_types.Schema(type=genai_types.Type.STRING, description="Window start 'YYYY-MM-DD HH:MM' (local)."),
                        "end":   genai_types.Schema(type=genai_types.Type.STRING, description="Window end 'YYYY-MM-DD HH:MM' (local)."),
                    },
                    required=["start", "end"],
                ),
            ),
        ]
    )


async def _check_email_from_tool(args: dict, db) -> str:
    if not google_integration.is_connected():
        return "Gmail isn't connected yet, Boss — connect a Google account first."
    q = (args.get("query") or "").strip()
    if q:
        return google_integration.format_search(google_integration.search_emails(q))
    return google_integration.format_unread(google_integration.unread_emails())


async def _read_email_from_tool(args: dict, db) -> str:
    if not google_integration.is_connected():
        return "Gmail isn't connected yet, Boss."
    return google_integration.format_email(google_integration.read_email(args.get("query") or ""))


async def _google_calendar_from_tool(args: dict, db) -> str:
    if not google_integration.is_connected():
        return "Google Calendar isn't connected yet, Boss."
    days = args.get("days") or 14
    try:
        days = int(days)
    except Exception:
        days = 14
    return google_integration.format_events(google_integration.upcoming_events(days=days))


async def _draft_email_from_tool(args: dict, db) -> str:
    if not google_integration.is_connected():
        return "Gmail isn't connected yet, Boss."
    to_raw = (args.get("to") or "").strip()
    subject = (args.get("subject") or "").strip()
    body = args.get("body") or ""
    if not to_raw:
        return "Who should it go to? I need a name or address."

    # A name → look it up in his Google Contacts; a real address passes through.
    to = to_raw
    if "@" not in to_raw:
        r = google_integration.resolve_recipients([to_raw])
        if r["resolved"]:
            to = r["resolved"][0]
        elif to_raw in r["ambiguous"]:
            opts = "; ".join(f"{m['name']} <{m['email']}>" for m in r["ambiguous"][to_raw][:4])
            return f"There are a few people named {to_raw} in your contacts: {opts}. Which one?"
        else:
            return f"I couldn't find an email for {to_raw} in your contacts, Boss — what's the address?"

    # Optional attachment: the document just built, else the one open in the editor.
    attach_id = attach_name = ""
    if args.get("attach"):
        did, item = documents.get_last_download()
        if not item:
            doc = get_active_doc()
            if doc and doc.get("text") is not None:
                built = documents.build_document(
                    doc["text"], doc.get("fmt") or "txt", doc.get("name") or "document")
                did, item = built["id"], documents.get_download(built["id"])
        if item:
            attach_id, attach_name = did, item["filename"]

    # Show him an editable preview with a Send button — nothing sends until he approves.
    payload = {"action": "draft", "to": to, "subject": subject, "body": body}
    if attach_id:
        payload["attach_id"] = attach_id
        payload["attach_name"] = attach_name
    await push_workspace_update("WIDGET_EMAIL", payload)
    extra = f" with {attach_name} attached" if attach_name else ""
    return (f"Drafted an email to {to}{extra} — it's up on screen for your review. "
            "Tweak it if you like, then hit Send. Nothing goes out until you do.")


async def _create_calendar_event_from_tool(args: dict, db) -> str:
    if not google_integration.is_connected():
        return "Google Calendar isn't connected yet, Boss."
    attendees = args.get("attendees") or []
    if isinstance(attendees, str):
        attendees = [a.strip() for a in attendees.replace(";", ",").split(",") if a.strip()]

    # Resolve any names → addresses via Contacts; note anyone we couldn't pin down.
    note = ""
    invite = attendees
    if attendees:
        r = google_integration.resolve_recipients(attendees)
        invite = r["resolved"]
        problems = []
        for nm, opts in r["ambiguous"].items():
            names = ", ".join(f"{m['name']} <{m['email']}>" for m in opts[:3])
            problems.append(f"several people match “{nm}” ({names})")
        for nm in r["unresolved"]:
            problems.append(f"no contact found for “{nm}”")
        if problems:
            note = (" I couldn't invite everyone — " + "; ".join(problems)
                    + ". Give me the right address and I'll add them.")

    res = google_integration.create_event(
        summary=args.get("summary") or "",
        start=args.get("start") or "",
        end=args.get("end") or "",
        location=args.get("location") or "",
        description=args.get("description") or "",
        attendees=invite,
    )
    if "error" in res:
        return res["error"]
    inv = res.get("invited") or []
    tail = f" Invited {', '.join(inv)}." if inv else ""
    return (f"Added \"{args.get('summary')}\" to your Google Calendar for {res.get('when','')}."
            f"{tail}{note} It'll sync to your phone.")


async def _manage_email_from_tool(args: dict, db) -> str:
    if not google_integration.is_connected():
        return "Gmail isn't connected yet, Boss."
    action = (args.get("action") or "").strip().lower()
    query = (args.get("query") or "").strip()
    res = google_integration.modify_emails(query, action, max_results=50)
    if "error" in res:
        return res["error"]
    n = res.get("count", 0)
    if n == 0:
        return f"Nothing matched '{query}', so nothing to {action.replace('_', ' ')}."
    verb = {"mark_read": "marked as read", "archive": "archived", "trash": "moved to trash"}.get(action, action)
    return f"Done — {verb} {n} email(s) matching '{query}'."


async def _update_calendar_event_from_tool(args: dict, db) -> str:
    if not google_integration.is_connected():
        return "Google Calendar isn't connected yet, Boss."
    res = google_integration.reschedule_event(
        args.get("query") or "", args.get("new_start") or "", args.get("new_end") or "")
    if "error" in res:
        return res["error"]
    return f"Moved \"{res.get('summary')}\" to {res.get('when','')}. Guests notified."


async def _cancel_calendar_event_from_tool(args: dict, db) -> str:
    if not google_integration.is_connected():
        return "Google Calendar isn't connected yet, Boss."
    res = google_integration.cancel_event(args.get("query") or "")
    if "error" in res:
        return res["error"]
    return f"Cancelled \"{res.get('summary')}\" and let the guests know."


async def _check_availability_from_tool(args: dict, db) -> str:
    if not google_integration.is_connected():
        return "Google Calendar isn't connected yet, Boss."
    res = google_integration.check_availability(args.get("start") or "", args.get("end") or "")
    if "error" in res:
        return res["error"]
    if res.get("free"):
        return f"You're free {res.get('window','')} — nothing on the calendar."
    busy = res.get("busy", [])
    items = "; ".join(f"{b['summary']} ({b['start']})" for b in busy[:5])
    return f"Not fully free {res.get('window','')} — you've got: {items}."


# ── Desktop OS-control handlers (GRACE_DESKTOP only) ─────────────────────────
async def _open_path_from_tool(args: dict, db) -> str:
    res = os_control.open_path(args.get("path") or "")
    if "error" in res:
        return res["error"]
    what = "folder" if res.get("kind") == "folder" else "file"
    return f"Opened the {what}: {res['opened']}"


async def _open_with_from_tool(args: dict, db) -> str:
    res = os_control.open_with(args.get("path") or "", args.get("app") or "")
    if "error" in res:
        return res["error"]
    return f"Opened {res['opened']} with {res.get('app', 'the app')}."


async def _list_directory_from_tool(args: dict, db) -> str:
    res = os_control.list_directory(args.get("path") or "~")
    if "error" in res:
        return res["error"]
    ents = res.get("entries", [])
    if not ents:
        return f"{res['path']} is empty."
    lines = [f"{res['path']} ({res.get('count', len(ents))} items):"]
    for e in ents[:60]:
        lines.append(f"  {'[dir] ' if e['dir'] else '      '}{e['name']}")
    return "\n".join(lines)


async def _read_local_file_from_tool(args: dict, db) -> str:
    res = os_control.read_local_file(args.get("path") or "")
    if "error" in res:
        return res["error"]
    tail = "\n…(truncated)" if res.get("truncated") else ""
    return f"Contents of {res['path']}:\n\n{res['content']}{tail}"


def _fmt_run_result(res: dict) -> str:
    if "error" in res:
        return f"Command failed: {res['error']}"
    out = (res.get("stdout") or "").strip()
    err = (res.get("stderr") or "").strip()
    head = f"Command finished (exit {res.get('code')})."
    if out:
        head += f"\n\nOutput:\n{out[:1500]}"
    if err and not res.get("ok"):
        head += f"\n\nErrors:\n{err[:800]}"
    return head


async def _write_local_file_from_tool(args: dict, db) -> str:
    path = (args.get("path") or "").strip()
    content = args.get("content") or ""
    if not path:
        return "I need a file path to write to, Boss."
    # Just do it (unless confirmation mode is on) — the old file is backed up first.
    if not os_control.should_gate(path):
        res = os_control.run_now("write_file", {"path": path, "content": content})
        if "error" in res:
            return f"Couldn't write it: {res['error']}"
        return f"✓ Saved {os.path.basename(res['path'])}"
    aid = os_control.stage("write_file", {"path": path, "content": content})
    preview = content if len(content) <= 800 else content[:800] + "\n…"
    await push_workspace_update("WIDGET_OSACTION", {
        "action": "confirm", "id": aid, "kind": "write_file",
        "title": "Write file", "path": os_control.expand(path), "preview": preview,
    })
    return (f"That'll write to {os_control.expand(path)} — I've put an Apply/Reject "
            "card up top for you. Nothing touches the disk until you approve.")


async def _run_command_from_tool(args: dict, db) -> str:
    command = (args.get("command") or "").strip()
    cwd = (args.get("cwd") or "").strip()
    if not command:
        return "What command should I run, Boss?"
    # Just run it (unless confirmation mode is on).
    if not os_control.should_gate(cwd or None):
        res = os_control.run_now("run_command", {"command": command, "cwd": cwd})
        return _fmt_run_result(res)
    aid = os_control.stage("run_command", {"command": command, "cwd": cwd})
    where = f"\nin {os_control.expand(cwd)}" if cwd else ""
    await push_workspace_update("WIDGET_OSACTION", {
        "action": "confirm", "id": aid, "kind": "run_command",
        "title": "Run command", "command": command,
        "cwd": os_control.expand(cwd) if cwd else "",
    })
    return (f"Ready to run `{command}`{where} — I've put an Apply/Reject card up top. "
            "It won't run until you approve.")


async def _system_power_from_tool(args: dict, db) -> str:
    res = os_control.system_power(args.get("action") or "", args.get("delay_minutes") or 0)
    if "error" in res:
        return res["error"]
    a = res.get("action")
    if a in ("shutdown", "restart") and res.get("in_seconds"):
        s = res["in_seconds"]
        return (f"{a.capitalize()} in {s} seconds, Boss — say 'cancel' or run cancel if "
                "you change your mind.")
    labels = {"sleep": "Going to sleep.", "hibernate": "Hibernating now.",
              "lock": "Locked.", "logoff": "Signing you out.", "cancel": "Cancelled — staying on."}
    return "✓ " + labels.get(a, f"{a} done.")


async def _media_control_from_tool(args: dict, db) -> str:
    res = os_control.media_control(args.get("action") or "", args.get("level"))
    if "error" in res:
        return res["error"]
    a = res.get("action")
    if a == "set_volume":
        return f"✓ Volume {res.get('level')}%"
    return "✓ " + a.replace("_", " ").capitalize()


async def _take_screenshot_from_tool(args: dict, db) -> str:
    res = os_control.screenshot()
    if "error" in res:
        return res["error"]
    # Load it as an active image so the vision model can see it on the next turn.
    try:
        with open(res["path"], "rb") as f:
            data = f.read()
        from routers.upload import _add_doc
        _add_doc({"name": "screenshot.png", "kind": "image", "mime": "image/png", "bytes": data})
    except Exception as e:
        logger.warning(f"[os] screenshot register failed: {e}")
    return (f"Captured your screen ({res.get('width')}x{res.get('height')}). It's loaded — "
            "ask me what you'd like to know about it.")


async def _display_control_from_tool(args: dict, db) -> str:
    res = os_control.display_control(args.get("action") or "")
    if "error" in res:
        return res["error"]
    return "✓ " + res.get("action", "done").replace("_", " ")


async def _app_control_from_tool(args: dict, db) -> str:
    res = os_control.app_control(args.get("action") or "", args.get("name") or "")
    if "error" in res:
        return res["error"]
    a, app = res.get("action"), res.get("app")
    if a == "close_window":
        closed = res.get("closed", [])
        if not closed:
            return f"I couldn't find an open window titled '{args.get('name')}', Boss."
        return f"✓ Closed {args.get('name')}"
    if a == "close" and not res.get("ok"):
        return f"Couldn't close {app} — it may not be running. ({res.get('detail','')})"
    verb = {"launch": "Opened", "close": "Closed", "focus": "Switched to"}.get(a, a)
    return f"✓ {verb} {app}"


async def _list_processes_from_tool(args: dict, db) -> str:
    res = os_control.list_processes()
    if "error" in res:
        return res["error"]
    procs = res.get("processes", [])
    if not procs:
        return "Couldn't read the process list."
    lines = ["Running now (by memory):"]
    for p in procs:
        lines.append(f"- {p['name']}: {p['mem_mb']} MB")
    return "\n".join(lines)


async def _find_file_from_tool(args: dict, db) -> str:
    res = os_control.find_file(args.get("name") or "", args.get("root") or "")
    if "error" in res:
        return res["error"]
    hits = res.get("matches", [])
    os_control.remember_finds(hits)          # so a loose "open the psd" resolves right
    if not hits:
        return f"No files matching '{args.get('name')}' under {res.get('base')}."
    tail = " (showing the first ones)" if res.get("truncated") else ""
    lines = [f"Found {len(hits)} match(es){tail}:"]
    for h in hits[:20]:
        lines.append(f"- {h}")
    return "\n".join(lines)


async def _file_operation_from_tool(args: dict, db) -> str:
    op = (args.get("op") or "").lower().strip()
    path = (args.get("path") or "").strip()
    dest = (args.get("dest") or "").strip()
    # create_folder is additive/safe → do it now. Everything else mutates → gate it.
    if op in ("create_folder", "mkdir", "new_folder"):
        res = os_control.run_now("file_op", {"op": op, "path": path})
        return f"✓ Created folder {res.get('path')}" if res.get("ok") else res.get("error", "Failed.")
    if op in ("empty_recycle_bin", "empty_recycle", "empty_trash"):
        target = "recycle bin"
    elif not path:
        return "Which file or folder, Boss?"
    else:
        target = os_control.expand(path)
    if not os_control.should_gate(path or None):
        res = os_control.run_now("file_op", {"op": op, "path": path, "dest": dest})
        if "error" in res:
            return res["error"]
        if op in ("delete", "remove", "trash", "recycle"):
            return f"✓ Deleted {os.path.basename(target)} (in the Recycle Bin if you need it back)"
        return f"✓ {op} done"
    aid = os_control.stage("file_op", {"op": op, "path": path, "dest": dest})
    detail = f"{op}: {target}" + (f"  →  {os_control.expand(dest)}" if dest else "")
    await push_workspace_update("WIDGET_OSACTION", {
        "action": "confirm", "id": aid, "kind": "file_op",
        "title": f"File: {op}", "command": detail, "cwd": "",
    })
    return (f"That'll {op} {target} — I've put an Apply/Reject card up. Nothing changes "
            "until you approve.")


async def _clipboard_from_tool(args: dict, db) -> str:
    action = (args.get("action") or "get").lower().strip()
    if action == "set":
        res = os_control.clipboard_set(args.get("text") or "")
        return "✓ Copied to clipboard" if res.get("ok") else res.get("error", "Failed.")
    res = os_control.clipboard_get()
    if "error" in res:
        return res["error"]
    txt = res.get("text", "")
    return f"Clipboard:\n{txt}" if txt else "Your clipboard is empty."


async def _type_text_from_tool(args: dict, db) -> str:
    res = os_control.type_text(args.get("text") or "")
    return "✓ Typed it" if res.get("ok") else res.get("error", "Failed.")


async def _system_status_from_tool(args: dict, db) -> str:
    s = os_control.system_status()
    if "error" in s:
        return s["error"]
    batt = s.get("battery")
    b = (f"{batt['percent']}% ({'charging' if batt['plugged'] else 'on battery'})"
         if batt else "no battery")
    return (f"Battery {b}. CPU {s.get('cpu_percent')}%, RAM {s.get('ram_percent')}% "
            f"({s.get('ram_used_gb')}/{s.get('ram_total_gb')} GB). Disk free "
            f"{s.get('disk_free_gb')} GB. Up {s.get('uptime_hours')}h. IP {s.get('ip')}.")


async def _web_from_tool(args: dict, db) -> str:
    action = (args.get("action") or "").lower().strip()
    query = args.get("query") or ""
    if action in ("open", "open_url", "goto"):
        res = os_control.open_url(query)
        return f"✓ Opened {res.get('url', query)}" if res.get("ok") else res.get("error", "Failed.")
    if action in ("search", "google"):
        res = os_control.web_search(query)
        return f"✓ Searched for {query}" if res.get("ok") else res.get("error", "Failed.")
    if action in ("play", "watch", "listen"):
        res = os_control.play_media(query, args.get("service") or "youtube")
        return (f"✓ Playing {query} on {res.get('service')}" if res.get("ok")
                else res.get("error", "Failed."))
    # default: treat as open
    res = os_control.open_url(query)
    return f"✓ Opened {res.get('url', query)}" if res.get("ok") else res.get("error", "Failed.")


async def _network_from_tool(args: dict, db) -> str:
    res = os_control.network(args.get("action") or "", args.get("name"))
    if "error" in res:
        return res["error"]
    a = res.get("action")
    if "online" in res:
        return "You're online, Boss." if res["online"] else "Looks like you're offline."
    if "public_ip" in res:
        return f"Your public IP is {res['public_ip']}."
    if "ssid" in res:
        return f"Connected to {res.get('ssid') or 'no network'}" + (f" ({res['signal']} signal)." if res.get("signal") else ".")
    if "password" in res:
        return f"The password for {res.get('name')} is: {res.get('password') or '(not found)'}"
    if "networks" in res:
        nets = res["networks"]
        return "Networks in range: " + (", ".join(nets) if nets else "none found") + "."
    if a == "wifi_connect":
        return f"✓ Connecting to {res.get('name')}" if res.get("ok") else f"Couldn't connect: {res.get('detail','')}"
    note = f" ({res['note']})" if res.get("note") else ""
    return ("✓ " + (a or "done").replace("_", " ")) if res.get("ok") else f"That didn't work{note}."


async def _brightness_from_tool(args: dict, db) -> str:
    res = os_control.brightness(args.get("action") or "", args.get("level"))
    if "error" in res:
        return res["error"]
    return f"✓ Brightness {res.get('level')}%"


async def _window_control_from_tool(args: dict, db) -> str:
    res = os_control.window_control(args.get("action") or "", args.get("title") or "")
    if "error" in res:
        return res["error"]
    return "✓ " + res.get("action", "done").replace("_", " ") + f" {args.get('title')}"


async def _archive_from_tool(args: dict, db) -> str:
    res = os_control.archive(args.get("op") or "", args.get("path") or "", args.get("dest"))
    if "error" in res:
        return res["error"]
    return f"✓ {res.get('op')} → {res.get('path')}"


async def _reveal_file_from_tool(args: dict, db) -> str:
    res = os_control.reveal_file(args.get("path") or "")
    if "error" in res:
        return res["error"]
    return f"✓ Showing {res.get('path')} in Explorer"


async def _system_utility_from_tool(args: dict, db) -> str:
    action = (args.get("action") or "").lower().strip()
    res = os_control.system_utility(action)
    if "error" in res:
        return res["error"]
    if "programs" in res:
        progs = res["programs"]
        head = f"You have {len(progs)} installed programs. Some of them:"
        return head + "\n" + "\n".join(f"- {p}" for p in progs[:30])
    if res.get("action") == "clear_temp":
        return f"✓ Cleared temp — removed {res.get('removed', 0)} items"
    if res.get("action") == "restart_explorer":
        return "✓ Restarted Explorer"
    return "✓ Done"


async def _file_details_from_tool(args: dict, db) -> str:
    action = (args.get("action") or "").lower().strip()
    path = args.get("path") or ""
    if action == "print":
        res = os_control.print_file(path)
        return f"✓ Sent {os_control.expand(path)} to the printer" if res.get("ok") else res.get("error", "Failed.")
    if action in ("recent", "recent_files", "recent_downloads"):
        res = os_control.recent_files(path or None)
        if "error" in res:
            return res["error"]
        fs = res.get("files", [])
        if not fs:
            return f"No recent files in {res.get('base')}."
        return "Recent in " + os.path.basename(res["base"]) + ":\n" + "\n".join(f"- {f['name']} ({f['when']})" for f in fs)
    # info
    res = os_control.path_info(path)
    if "error" in res:
        return res["error"]
    if res.get("kind") == "folder":
        return f"{res['path']}: {res['size_mb']} MB, {res['files']} files, {res['folders']} folders."
    return f"{res['path']}: {res['size_mb']} MB, {res.get('ext')} file, modified {res.get('modified')}."


async def _batch_rename_from_tool(args: dict, db) -> str:
    folder = (args.get("folder") or "").strip()
    prefix = (args.get("prefix") or "").strip()
    ext = (args.get("ext") or "").strip()
    if not folder or not prefix:
        return "I need a folder and a name prefix, Boss."
    payload = {"folder": folder, "prefix": prefix, "ext": ext}
    if not os_control.should_gate(folder):
        res = os_control.run_now("batch_rename", payload)
        return f"✓ Renamed {res.get('count', 0)} files" if res.get("ok") else res.get("error", "Failed.")
    aid = os_control.stage("batch_rename", payload)
    detail = f"Rename all files in {os_control.expand(folder)} → {prefix}_1, {prefix}_2, …" + (f" (only .{ext})" if ext else "")
    await push_workspace_update("WIDGET_OSACTION", {
        "action": "confirm", "id": aid, "kind": "file_op", "title": "Batch rename",
        "command": detail, "cwd": "",
    })
    return "That'll rename those files — Apply/Reject card is up. Nothing changes until you approve."


async def _close_top_memory_from_tool(args: dict, db) -> str:
    top = os_control.top_memory_process()
    if not top:
        return "Couldn't find a closable app, Boss."
    name, mem, pid = top
    mb = round(mem / 1048576)
    if not os_control.should_gate():
        res = os_control.run_now("kill_pid", {"pid": pid})
        return f"✓ Closed {name} ({mb} MB)" if res.get("ok") else f"Couldn't close {name}: {res.get('detail','')}"
    aid = os_control.stage("kill_pid", {"pid": pid})
    await push_workspace_update("WIDGET_OSACTION", {
        "action": "confirm", "id": aid, "kind": "file_op", "title": "Close app",
        "command": f"Force-close {name} — using {mb} MB (PID {pid})", "cwd": "",
    })
    return f"{name} is using the most memory ({mb} MB) — Apply/Reject card is up to close it."


async def _personalize_from_tool(args: dict, db) -> str:
    res = os_control.personalize(args.get("action") or "", args.get("value"))
    if "error" in res:
        return res["error"]
    if res.get("mode") in ("dark", "light"):
        return f"✓ Switched to {res['mode']} mode"
    if res.get("path"):
        return "✓ Wallpaper set"
    if res.get("mode"):
        return f"✓ Power plan: {res['mode']}"
    return "✓ Done"


async def _image_tool_from_tool(args: dict, db) -> str:
    res = os_control.image_op(args.get("op") or "", args.get("path") or "",
                              args.get("dest"), args.get("value"))
    if "error" in res:
        return res["error"]
    return f"✓ {res.get('op')} → {res.get('path')}"


async def _pdf_tool_from_tool(args: dict, db) -> str:
    res = os_control.pdf_op(args.get("op") or "", args.get("path") or "",
                            args.get("dest"), args.get("pages"), args.get("path2"))
    if "error" in res:
        return res["error"]
    return f"✓ {res.get('op')} ({res.get('count', '')}) → {res.get('path')}"


async def _schedule_task_from_tool(args: dict, db) -> str:
    res = os_control.schedule_task(
        args.get("action") or "create", args.get("name"), args.get("command"),
        args.get("time"), args.get("repeat") or "once")
    if "error" in res:
        return res["error"]
    a = res.get("action")
    if "tasks" in res:
        ts = res["tasks"]
        return "Scheduled tasks: " + (", ".join(ts) if ts else "none") + "."
    if a == "delete":
        return "✓ Deleted the scheduled task" if res.get("ok") else "Couldn't find that task."
    if not res.get("ok"):
        return f"Couldn't schedule it, Boss — may need admin. ({res.get('detail','')})"
    return f"✓ Scheduled '{res.get('name','').split(chr(92))[-1]}' at {res.get('time')} ({res.get('repeat')})"


async def _proactive_alerts_from_tool(args: dict, db) -> str:
    from services.proactive import proactive_monitor
    action = (args.get("action") or "status").lower().strip()
    setting = (args.get("setting") or "all").lower().strip()
    s = proactive_monitor.settings
    if action == "status":
        on = [k for k, v in s.items() if v]
        off = [k for k, v in s.items() if not v]
        return (f"Proactive alerts on: {', '.join(on) or 'none'}." +
                (f" Off: {', '.join(off)}." if off else ""))
    want = action in ("enable", "on", "turn_on")
    keys = list(s.keys()) if setting in ("all", "everything") else [setting]
    changed = []
    for k in keys:
        if k in s:
            s[k] = want
            changed.append(k)
    if not changed:
        return f"I don't have an alert called '{setting}', Boss. Options: battery, disk, downloads, break."
    return f"✓ {'Enabled' if want else 'Disabled'} {', '.join(changed)} alert(s)"


async def _confirm_actions_from_tool(args: dict, db) -> str:
    on = os_control.set_confirm(bool(args.get("enabled")))
    return ("✓ I'll ask before running risky actions now." if on
            else "✓ Got it — I'll just do it, no confirmations.")


async def _get_directions_from_tool(args: dict, db) -> str:
    if not directions.is_configured():
        return ("Directions aren't set up yet, Boss — add a TOMTOM_API_KEY to the .env "
                "(free at developer.tomtom.com) and I can route with live traffic.")
    res = await directions.get_route(
        args.get("origin") or "", args.get("destination") or "", args.get("arrive_by") or ""
    )
    if "error" not in res and res.get("points"):
        await push_workspace_update("WIDGET_MAP", {
            "action": "route",
            "points": res["points"],
            "traffic_segments": res.get("traffic_segments", []),
            "alts": [a["points"] for a in res.get("alternatives", []) if a.get("points")],
            "origin": {"name": res["origin"]["name"], "lat": res["origin"]["lat"], "lon": res["origin"]["lon"]},
            "destination": {"name": res["destination"]["name"], "lat": res["destination"]["lat"], "lon": res["destination"]["lon"]},
            "info": directions.route_summary(res),
        })
    return directions.format_for_model(res)


async def _stream_one_turn(contents, config, stream_id, tts_state, model=None):
    """Stream a single Gemini turn: push text deltas to the UI, flush finished
    sentences to TTS as they complete, collect any function calls, and return
    (function_calls, model_content) where model_content is the assistant turn to
    append to history for the next round.

    tts_state is a mutable dict carrying {collected, buffer, tasks, seq} across
    turns so speech ordering stays continuous through multiple tool rounds.
    """
    turn_text = ""
    function_calls = []
    fc_parts = []          # original Part objects carrying function calls

    stream = await grace_llm_client.aio.models.generate_content_stream(
        model=model or GRACE_MODEL, contents=contents, config=config
    )
    async for chunk in stream:
        for cand in (chunk.candidates or []):
            content = getattr(cand, "content", None)
            if not content or not content.parts:
                continue
            for part in content.parts:
                text = getattr(part, "text", None)
                if text:
                    turn_text += text
                    tts_state["collected"].append(text)
                    # Stream text to the UI immediately so it appears and types
                    # out as she generates — fast and responsive. Voice is one
                    # smooth clip afterward (see end of _run_reasoning_loop).
                    await push_workspace_update(
                        "WIDGET_CHAT", {"stream_id": stream_id, "delta": text}
                    )
                fc = getattr(part, "function_call", None)
                if fc:
                    function_calls.append(fc)
                    # Keep the ORIGINAL part — it carries the thought_signature
                    # that Gemini 3.x requires echoed back with the tool result.
                    fc_parts.append(part)

    # Rebuild the assistant turn so the model sees its own request next round.
    # Function-call parts are passed through untouched (preserving their
    # thought_signature); only text is re-wrapped from the streamed accumulation.
    model_parts = []
    if turn_text:
        model_parts.append(genai_types.Part(text=turn_text))
    model_parts.extend(fc_parts)
    model_content = genai_types.Content(role="model", parts=model_parts)
    return function_calls, model_content


_HISTORY_TURNS = 8  # how many prior user+grace exchanges to replay for context
_LOG_RETENTION = 300  # keep only the most recent N interaction_log rows


def _humanize_gap(seconds: float) -> str:
    """Turn an elapsed duration into a natural phrase for time awareness."""
    if seconds < 90:
        return "moments ago"
    mins = seconds / 60
    if mins < 60:
        return f"about {int(round(mins))} minutes ago"
    hours = mins / 60
    if hours < 24:
        h = int(round(hours))
        return f"about {h} hour{'s' if h != 1 else ''} ago"
    days = int(hours // 24)
    return "yesterday" if days == 1 else f"about {days} days ago"


def _temporal_note(db) -> str:
    """Ambient time awareness for the system prompt: the current local date/time
    plus how long since the last exchange — so Grace knows whether she's mid-
    conversation, picking up later the same day, or greeting a new day. Framed so
    she uses it for orientation without reciting it."""
    now_local = datetime.now()
    clock = now_local.strftime("%I:%M %p").lstrip("0")
    stamp = f"{now_local.strftime('%A, %B')} {now_local.day}, {now_local.year} at {clock}"

    gap = ""
    try:
        last = (
            db.query(models.InteractionLog)
            .order_by(models.InteractionLog.id.desc())
            .first()
        )
        if last and last.timestamp:
            gap = _humanize_gap((utcnow() - last.timestamp).total_seconds())
    except Exception:
        gap = ""

    note = (
        " [Time awareness — for your own sense of continuity; do NOT announce the "
        f"date or time, or that you're checking a clock, unless he asks. Right now "
        f"it is {stamp}."
    )
    if gap:
        note += (
            f" Your last exchange with him was {gap}. Use this to judge whether "
            "you're still mid-conversation, catching up later the same day, or "
            "greeting him on a new day — and keep any references to time accurate. "
            "Don't act like a long time has passed when it hasn't.]"
        )
    else:
        note += " This looks like the start of a new conversation.]"
    return note


def _prune_interaction_log(db, keep=_LOG_RETENTION):
    """Trim the interaction_log to its most recent `keep` rows so it can't grow
    without bound. A no-op until the table exceeds the cap. Best-effort — a
    failure here must never break the request."""
    try:
        cutoff = (
            db.query(models.InteractionLog.id)
            .order_by(models.InteractionLog.id.desc())
            .offset(keep)
            .first()
        )
        if cutoff:
            db.query(models.InteractionLog).filter(
                models.InteractionLog.id <= cutoff[0]
            ).delete(synchronize_session=False)
            db.commit()
    except Exception as e:
        logger.warning(f"interaction_log prune skipped: {e}")
        db.rollback()


def _load_recent_history(db, limit=_HISTORY_TURNS):
    """Return recent conversation turns as Gemini Content objects.

    Rebuilds recent InteractionLog rows as alternating user / model turns so
    Gemini can resolve references like "the event on july 26th" established in
    earlier messages. Tool-call parts are NOT replayed (they carry
    thought_signatures we no longer hold); only plain text is restored.

    Tier-1 rows (clock, weather, briefing, diagnostics, lock/vscode) are skipped
    — those are canned, deterministic replies that never went through Gemini, so
    replaying them as "dialogue" only pollutes the context window.
    """
    try:
        # Over-fetch, since some recent rows will be filtered out as Tier-1.
        rows = (
            db.query(models.InteractionLog)
            .order_by(models.InteractionLog.id.desc())
            .limit(limit * 3)
            .all()
        )
    except Exception as e:
        logger.warning(f"Could not load conversation history: {e}")
        return []

    # Keep only genuine Gemini conversation, newest-first, capped at `limit`.
    conversational = [r for r in rows if not _is_tier1_message(r.user_input)][:limit]

    history = []
    for row in reversed(conversational):  # oldest first
        user_msg = (row.user_input or "").strip()
        grace_msg = (row.grace_response or "").strip()
        if user_msg:
            history.append(
                genai_types.Content(
                    role="user", parts=[genai_types.Part(text=user_msg)]
                )
            )
        if grace_msg:
            history.append(
                genai_types.Content(
                    role="model", parts=[genai_types.Part(text=grace_msg)]
                )
            )
    return history


async def _run_reasoning_loop(system_persona, user_text, stream_id, allow_search, db, deep=False, model_override=None):
    """Drive the full Gemini conversation, including web-search tool rounds.

    Streams text to the chat widget as it arrives and speaks finished sentences.
    Loops while Gemini asks for the web_search tool (up to _MAX_TOOL_ROUNDS),
    feeding results back each round. Returns (response_text, streamed_audio).

    `deep` = code/debugging task: bigger output budget, a stronger model if
    configured, and (below) no voice — reviews are for reading, not listening.
    """
    global _last_activity
    _last_activity = time.time()   # real work → keep-warm stays quiet
    tools = []
    if allow_search:
        tools.append(_build_search_tool())
    tools.append(_build_task_tool())
    tools.append(_build_event_tool())
    tools.append(_build_memory_tool())
    tools.append(_build_project_tool())
    tools.append(_build_ui_tool())
    tools.append(_build_reminder_tool())
    tools.append(_build_notes_tool())
    tools.append(_build_code_tool())
    tools.append(_build_shodan_tool())
    tools.append(_build_directions_tool())
    tools.append(_build_document_tool())
    tools.append(_build_habit_tool())
    if google_integration.is_connected():
        tools.append(_build_google_tool())
    if os_control.is_enabled():          # desktop app only — never on the cloud
        tools.append(_build_desktop_tool())
        tools.append(_build_system_tool())
        tools.append(_build_extra_tool())
        tools.append(_build_final_tool())
    model = model_override or (GRACE_CODE_MODEL if deep else GRACE_MODEL)
    config = genai_types.GenerateContentConfig(
        system_instruction=system_persona,
        max_output_tokens=_DEEP_MAX_TOKENS if deep else _CHAT_MAX_TOKENS,
        tools=tools or None,
    )
    # Start from recent conversation history so Grace remembers prior turns,
    # then append the operator's current message as the newest turn — attaching
    # any active uploaded document so she can actually read it.
    contents = _load_recent_history(db)
    user_parts = []
    # Attach every active document so Grace can reason across all of them
    # (compare, merge, cross-reference). Each is labeled with its name.
    _docs = get_active_docs()
    for doc in _docs:
        try:
            if doc["kind"] in ("image", "pdf") and doc.get("bytes"):
                if len(_docs) > 1:
                    user_parts.append(genai_types.Part(text=f"[Attached file \"{doc['name']}\"]:"))
                user_parts.append(
                    genai_types.Part.from_bytes(data=doc["bytes"], mime_type=doc["mime"])
                )
            elif doc.get("text"):
                user_parts.append(genai_types.Part(
                    text=f"[Attached file \"{doc['name']}\" — its contents]:\n{doc['text']}\n\n"
                ))
        except Exception as e:
            logger.warning(f"Could not attach uploaded doc {doc.get('name')}: {e}")
    user_parts.append(genai_types.Part(text=user_text))
    contents.append(genai_types.Content(role="user", parts=user_parts))
    tts_state = {"collected": [], "tasks": [], "seq": 0}

    for _ in range(_MAX_TOOL_ROUNDS):
        function_calls, model_content = await _stream_one_turn(
            contents, config, stream_id, tts_state, model=model
        )
        if not function_calls:
            break

        # Record the model's tool-call turn, then run each search and feed the
        # results back so Gemini can read them and continue answering.
        contents.append(model_content)
        response_parts = []
        for fc in function_calls:
            if fc.name == "web_search":
                query = (fc.args or {}).get("query", "")
                await push_workspace_update(
                    "WIDGET_CHAT",
                    {"stream_id": stream_id, "delta": f"\n[searching the web: {query}]\n"},
                )
                payload = await web_search.search(query)
                result_text = web_search.format_for_model(payload)
            elif fc.name == "create_task":
                result_text = await _create_task_from_tool(dict(fc.args or {}), db)
            elif fc.name == "delete_task":
                result_text = await _delete_task_from_tool(dict(fc.args or {}), db)
            elif fc.name == "update_task":
                result_text = await _update_task_from_tool(dict(fc.args or {}), db)
            elif fc.name == "create_event":
                result_text = await _create_event_from_tool(dict(fc.args or {}), db)
            elif fc.name == "delete_event":
                result_text = await _delete_event_from_tool(dict(fc.args or {}), db)
            elif fc.name == "update_event":
                result_text = await _update_event_from_tool(dict(fc.args or {}), db)
            elif fc.name == "remember_fact":
                result_text = await _remember_fact_from_tool(dict(fc.args or {}), db)
            elif fc.name == "forget_fact":
                result_text = await _forget_fact_from_tool(dict(fc.args or {}), db)
            elif fc.name == "track_project":
                result_text = await _track_project_from_tool(dict(fc.args or {}), db)
            elif fc.name == "log_progress":
                result_text = await _log_progress_from_tool(dict(fc.args or {}), db)
            elif fc.name == "control_widgets":
                result_text = await _control_widgets_from_tool(dict(fc.args or {}), db)
            elif fc.name == "set_reminder":
                result_text = await _set_reminder_from_tool(dict(fc.args or {}), db)
            elif fc.name == "cancel_reminder":
                result_text = await _cancel_reminder_from_tool(dict(fc.args or {}), db)
            elif fc.name == "save_note":
                result_text = await _save_note_from_tool(dict(fc.args or {}), db)
            elif fc.name == "recall_notes":
                result_text = await _recall_notes_from_tool(dict(fc.args or {}), db)
            elif fc.name == "delete_note":
                result_text = await _delete_note_from_tool(dict(fc.args or {}), db)
            elif fc.name == "list_files":
                result_text = await _list_files_from_tool(dict(fc.args or {}), db)
            elif fc.name == "read_code_file":
                result_text = await _read_code_file_from_tool(dict(fc.args or {}), db)
            elif fc.name == "search_code":
                result_text = await _search_code_from_tool(dict(fc.args or {}), db)
            elif fc.name == "propose_edit":
                result_text = await _propose_edit_from_tool(dict(fc.args or {}), db)
            elif fc.name == "shodan_lookup":
                result_text = await _shodan_lookup_from_tool(dict(fc.args or {}), db)
            elif fc.name == "get_directions":
                result_text = await _get_directions_from_tool(dict(fc.args or {}), db)
            elif fc.name == "edit_document":
                result_text = await _edit_document_from_tool(dict(fc.args or {}), db)
            elif fc.name == "create_document":
                result_text = await _create_document_from_tool(dict(fc.args or {}), db)
            elif fc.name == "track_habit":
                result_text = await _track_habit_from_tool(dict(fc.args or {}), db)
            elif fc.name == "check_email":
                result_text = await _check_email_from_tool(dict(fc.args or {}), db)
            elif fc.name == "read_email":
                result_text = await _read_email_from_tool(dict(fc.args or {}), db)
            elif fc.name == "google_calendar":
                result_text = await _google_calendar_from_tool(dict(fc.args or {}), db)
            elif fc.name == "draft_email":
                result_text = await _draft_email_from_tool(dict(fc.args or {}), db)
            elif fc.name == "create_calendar_event":
                result_text = await _create_calendar_event_from_tool(dict(fc.args or {}), db)
            elif fc.name == "manage_email":
                result_text = await _manage_email_from_tool(dict(fc.args or {}), db)
            elif fc.name == "update_calendar_event":
                result_text = await _update_calendar_event_from_tool(dict(fc.args or {}), db)
            elif fc.name == "cancel_calendar_event":
                result_text = await _cancel_calendar_event_from_tool(dict(fc.args or {}), db)
            elif fc.name == "check_availability":
                result_text = await _check_availability_from_tool(dict(fc.args or {}), db)
            elif fc.name == "open_path":
                result_text = await _open_path_from_tool(dict(fc.args or {}), db)
            elif fc.name == "open_with":
                result_text = await _open_with_from_tool(dict(fc.args or {}), db)
            elif fc.name == "list_directory":
                result_text = await _list_directory_from_tool(dict(fc.args or {}), db)
            elif fc.name == "read_local_file":
                result_text = await _read_local_file_from_tool(dict(fc.args or {}), db)
            elif fc.name == "write_local_file":
                result_text = await _write_local_file_from_tool(dict(fc.args or {}), db)
            elif fc.name == "run_command":
                result_text = await _run_command_from_tool(dict(fc.args or {}), db)
            elif fc.name == "system_power":
                result_text = await _system_power_from_tool(dict(fc.args or {}), db)
            elif fc.name == "media_control":
                result_text = await _media_control_from_tool(dict(fc.args or {}), db)
            elif fc.name == "take_screenshot":
                result_text = await _take_screenshot_from_tool(dict(fc.args or {}), db)
            elif fc.name == "display_control":
                result_text = await _display_control_from_tool(dict(fc.args or {}), db)
            elif fc.name == "app_control":
                result_text = await _app_control_from_tool(dict(fc.args or {}), db)
            elif fc.name == "list_processes":
                result_text = await _list_processes_from_tool(dict(fc.args or {}), db)
            elif fc.name == "find_file":
                result_text = await _find_file_from_tool(dict(fc.args or {}), db)
            elif fc.name == "file_operation":
                result_text = await _file_operation_from_tool(dict(fc.args or {}), db)
            elif fc.name == "clipboard":
                result_text = await _clipboard_from_tool(dict(fc.args or {}), db)
            elif fc.name == "type_text":
                result_text = await _type_text_from_tool(dict(fc.args or {}), db)
            elif fc.name == "system_status":
                result_text = await _system_status_from_tool(dict(fc.args or {}), db)
            elif fc.name == "web":
                result_text = await _web_from_tool(dict(fc.args or {}), db)
            elif fc.name == "network":
                result_text = await _network_from_tool(dict(fc.args or {}), db)
            elif fc.name == "brightness":
                result_text = await _brightness_from_tool(dict(fc.args or {}), db)
            elif fc.name == "window_control":
                result_text = await _window_control_from_tool(dict(fc.args or {}), db)
            elif fc.name == "archive":
                result_text = await _archive_from_tool(dict(fc.args or {}), db)
            elif fc.name == "reveal_file":
                result_text = await _reveal_file_from_tool(dict(fc.args or {}), db)
            elif fc.name == "system_utility":
                result_text = await _system_utility_from_tool(dict(fc.args or {}), db)
            elif fc.name == "file_details":
                result_text = await _file_details_from_tool(dict(fc.args or {}), db)
            elif fc.name == "batch_rename":
                result_text = await _batch_rename_from_tool(dict(fc.args or {}), db)
            elif fc.name == "close_top_memory":
                result_text = await _close_top_memory_from_tool(dict(fc.args or {}), db)
            elif fc.name == "personalize":
                result_text = await _personalize_from_tool(dict(fc.args or {}), db)
            elif fc.name == "image_tool":
                result_text = await _image_tool_from_tool(dict(fc.args or {}), db)
            elif fc.name == "pdf_tool":
                result_text = await _pdf_tool_from_tool(dict(fc.args or {}), db)
            elif fc.name == "schedule_task":
                result_text = await _schedule_task_from_tool(dict(fc.args or {}), db)
            elif fc.name == "proactive_alerts":
                result_text = await _proactive_alerts_from_tool(dict(fc.args or {}), db)
            elif fc.name == "confirm_actions":
                result_text = await _confirm_actions_from_tool(dict(fc.args or {}), db)
            else:
                result_text = f"Unknown tool '{fc.name}'."
            response_parts.append(
                genai_types.Part.from_function_response(
                    name=fc.name, response={"result": result_text}
                )
            )
        contents.append(genai_types.Content(role="user", parts=response_parts))

    response_text = "".join(tts_state["collected"]).strip() or "No response generated."

    # A "✓…" reply is a silent action ack — the operator wants her to just do the
    # thing, not announce it. Skip voice entirely (the UI shows a subtle line).
    silent = response_text.lstrip().startswith("✓")

    # Text is already on screen (streamed live). Voice is one clip: register the
    # reply and hand the UI a streaming url. Deep/code replies aren't read aloud.
    if not deep and not silent and response_text and response_text != "No response generated.":
        voice_url = register_speech(stream_id, response_text)
        await push_workspace_update(
            "WIDGET_CHAT", {"stream_id": stream_id, "audio_chunk": voice_url, "seq": 0}
        )

    streamed_audio = True
    return response_text, streamed_audio


router = APIRouter(prefix="/api/command", tags=["Intelligence Routing Engine"])


class CommandInput(BaseModel):
    text: str
    input_type: str = "text"
    layout_context: str = "OPEN_GRID"

    @field_validator("input_type")
    @classmethod
    def validate_input_type(cls, v: str) -> str:
        if v not in VALID_INPUT_TYPES:
            raise ValueError(f"input_type must be one of {VALID_INPUT_TYPES}")
        return v

    @field_validator("layout_context")
    @classmethod
    def validate_layout_context(cls, v: str) -> str:
        if v not in VALID_LAYOUT_CONTEXTS:
            raise ValueError(f"layout_context must be one of {VALID_LAYOUT_CONTEXTS}")
        return v


# ── Keep-warm: stop the model connection going cold, so the first reply after an
# idle gap is ~2s instead of ~6s. Pings only when the app is open and idle. ─────
_last_activity = time.time()


async def keep_warm_loop():
    from core.sse import subscriber_count
    await asyncio.sleep(45)
    while True:
        try:
            idle = time.time() - _last_activity
            if grace_llm_client is not None and subscriber_count() > 0 and idle > 150:
                await asyncio.to_thread(
                    lambda: grace_llm_client.models.generate_content(
                        model=GRACE_MODEL, contents="ok",
                        config=genai_types.GenerateContentConfig(max_output_tokens=1),
                    )
                )
        except Exception as e:
            logger.debug(f"[keepwarm] skipped: {e}")
        await asyncio.sleep(120)


# ── Instant, no-LLM fast-path for the most common desktop commands ───────────
def _os_fastpath(text: str):
    """Handle a short, unambiguous desktop command with ZERO model latency.
    Returns (response_text, silent) or None. Desktop app only."""
    if not os_control.is_enabled():
        return None
    t = " " + text.strip().lower().rstrip(" .!") + " "
    raw = text.strip().lower().rstrip(" .!?")

    # instant document search via the Windows index — only pure "find X" queries
    # (compound ones like "find X and open it" fall through to the model)
    ms = re.match(r"(?:find|locate|search for|look for|where(?:'s| is| are)?)\s+"
                  r"(?:me\s+)?(?:my |the |a |an |all )*(.+)", raw)
    if ms and " and " not in raw and not re.search(
            r"\b(open|delete|move|rename|email|send|zip|print|show me)\b", raw):
        term = re.sub(r"\b(files?|documents?|docs?|folders?)\b\s*$", "", ms.group(1).strip()).strip()
        if term:
            res = os_control.find_file(term)
            os_control.remember_finds(res.get("matches", []))
            hits = res.get("matches", [])
            if not hits:
                return f"Couldn't find anything named '{term}', Boss.", False
            top = "\n".join(f"- {h}" for h in hits[:8])
            more = f"\n…and {len(hits) - 8} more" if len(hits) > 8 else ""
            return f"Found {len(hits)} for '{term}':\n{top}{more}", True

    # instant open of a folder / path / selected item / a clearly-named document —
    # context-aware: it prefers the folder you currently have open in Explorer.
    mo = re.match(r"(?:open|select|launch)\s+(?:up\s+)?(?:my |the |a |an )?(.+)", raw)
    if mo and " and " not in raw and " with " not in raw:
        target = re.sub(r"\b(folder|directory)\b", "", mo.group(1).strip()).strip().rstrip("?").strip()
        _EXE = (".exe", ".msi", ".bat", ".cmd", ".ps1", ".vbs", ".lnk")
        # "open this / it / the selected one" → open whatever's selected in Explorer
        if re.fullmatch(r"(this|it|that|selected|the selected( one| file| item)?|the highlighted( one)?)", target):
            ctx = os_control.find_in_active_folder("")
            sel = (ctx or {}).get("selected", [])
            if len(sel) == 1:
                r = os_control.open_path(sel[0])
                return (f"✓ Opened {os.path.basename(sel[0])}" if r.get("ok") else r.get("error", "")), True
            # nothing / several selected → let the model handle it
        else:
            _KF = {"downloads": "Downloads", "download": "Downloads", "documents": "Documents",
                   "document": "Documents", "desktop": "Desktop", "pictures": "Pictures",
                   "photos": "Pictures", "music": "Music", "videos": "Videos", "video": "Videos"}
            if target in _KF:
                r = os_control.open_path(_KF[target])
                return (f"✓ Opened {_KF[target]}" if r.get("ok") else r.get("error", "")), True
            if re.search(r"[:\\/~]", target):                    # an explicit path
                r = os_control.open_path(target)
                if r.get("ok"):
                    return f"✓ Opened {os.path.basename(r['opened']) or r['opened']}", True
            else:                                                # a named document
                picked = None
                ctx = os_control.find_in_active_folder(target)   # the open folder first
                if ctx and len(ctx["matches"]) == 1:
                    picked = ctx["matches"][0]
                elif ctx and len(ctx["matches"]) > 1:
                    os_control.remember_finds(ctx["matches"])     # ambiguous → defer
                else:
                    res = os_control.find_file(target)            # else the global index
                    m = res.get("matches", [])
                    os_control.remember_finds(m)
                    if len(m) == 1:
                        picked = m[0]
                if picked and not picked.lower().endswith(_EXE):
                    r = os_control.open_path(picked)
                    return (f"✓ Opened {os.path.basename(picked)}" if r.get("ok") else r.get("error", "")), True
        # ambiguous / not found / an app / an executable → defer to the model

    # volume
    m = re.search(r"\bvolume (?:to |at )?(\d{1,3})\b", t)
    if m:
        r = os_control.media_control("set_volume", int(m.group(1)))
        return (f"✓ Volume {r.get('level')}%" if r.get("ok") else r.get("error", "Couldn't set volume.")), True
    if re.search(r"\b(volume up|turn it up|turn up the volume|louder)\b", t):
        os_control.media_control("volume_up"); return "✓ Volume up", True
    if re.search(r"\b(volume down|turn it down|turn down the volume|quieter|lower the volume)\b", t):
        os_control.media_control("volume_down"); return "✓ Volume down", True
    if "mic" not in t and re.search(r"\b(mute|unmute)\b", t):
        os_control.media_control("mute"); return "✓ Muted", True
    if re.search(r"\b(pause|play ?pause)\b", t):
        os_control.media_control("play_pause"); return "✓", True
    if re.search(r"\bnext (song|track)\b", t) or re.search(r"\bskip (this )?(song|track)\b", t):
        os_control.media_control("next"); return "✓ Next", True
    if re.search(r"\b(previous|last) (song|track)\b", t):
        os_control.media_control("previous"); return "✓ Previous", True

    # brightness
    m = re.search(r"\bbrightness (?:to |at |by )?(\d{1,3})\b", t) or \
        re.search(r"\b(?:set|reduce|lower|dim|increase|raise|change) (?:the )?brightness (?:to |by )?(\d{1,3})\b", t)
    if m:
        r = os_control.brightness("set", int(m.group(1)))
        return (f"✓ Brightness {r.get('level')}%" if r.get("ok") else r.get("error", "")), True
    if re.search(r"\b(brightness up|brighter|increase brightness)\b", t):
        r = os_control.brightness("up"); return (f"✓ Brightness {r.get('level')}%" if r.get("ok") else r.get("error", "")), True
    if re.search(r"\b(brightness down|dimmer|reduce brightness|lower brightness|dim the screen)\b", t):
        r = os_control.brightness("down"); return (f"✓ Brightness {r.get('level')}%" if r.get("ok") else r.get("error", "")), True

    # display
    if re.search(r"\b(show (the |my )?desktop|minimi[sz]e (all|everything))\b", t):
        os_control.display_control("show_desktop"); return "✓ Showing desktop", True
    if re.search(r"\bturn off (my |the )?(monitor|screen|display)\b", t):
        os_control.display_control("monitor_off"); return "✓ Monitor off", True

    # power: ONLY the harmless 'cancel' is fast-pathed. Shutdown/restart/sleep/
    # hibernate/logoff deliberately go through the model — a 2s cost, but it means
    # no regex misfire can ever sleep or reboot the machine on its own.
    if re.search(r"\bcancel (the )?(shut ?down|restart|reboot)\b", t):
        os_control.system_power("cancel"); return "✓ Cancelled — staying on", True

    # status (spoken answers)
    if re.search(r"\b(battery|charge)\b", t) and re.search(r"\b(what|how much|level|percent|status|left|is my|my)\b", t):
        s = os_control.system_status(); b = s.get("battery")
        if not b:
            return "This machine doesn't report a battery, Boss.", False
        return f"Battery's at {b['percent']}%, {'charging' if b['plugged'] else 'on battery'}.", False
    if re.search(r"\b(am i online|do i have internet|are we online|is the internet (up|working))\b", t):
        r = os_control.network("online")
        return ("You're online, Boss." if r.get("online") else "Looks like you're offline, Boss."), False

    return None


@router.post("/process")
async def process_user_intent(payload: CommandInput, db: Session = Depends(get_db)):
    user_raw_string = payload.text.strip()
    layout_state = payload.layout_context

    if not user_raw_string:
        raise HTTPException(status_code=400, detail="Command payload text string cannot be empty.")

    triggered_widget = "WIDGET_CHAT"
    modality = "widget_only"
    response_text = ""
    audio_path = ""
    stream_id = None       # set when Tier-2 streams a reply token-by-token
    streamed_audio = False  # True once we've spoken sentence-by-sentence

    text_lower = user_raw_string.lower()
    tier_1_matched = False

    if _BRIEFING_RE.search(text_lower):
        response_text = await compose_briefing(db)
        triggered_widget = "WIDGET_CHAT"
        tier_1_matched = True
        # Briefings are always spoken. Synthesize here and mark audio as already
        # streamed so the fallback block below doesn't regenerate it.
        audio_path = await miso_voice.generate_speech(response_text)
        if audio_path:
            modality = "hybrid"
            streamed_audio = True

    elif _WEEKLY_RE.search(text_lower):
        response_text = await compose_weekly_review(db)
        triggered_widget = "WIDGET_CHAT"
        tier_1_matched = True
        audio_path = await miso_voice.generate_speech(response_text)
        if audio_path:
            modality = "hybrid"
            streamed_audio = True

    elif _DATETIME_QUERY_RE.search(text_lower):
        response_text = _format_datetime_response(text_lower)
        triggered_widget = "WIDGET_CHAT"
        tier_1_matched = True

    elif _LOCK_RE.search(text_lower):
        response_text = system_bridge.execute_system_command("lock_session")
        triggered_widget = "WIDGET_SYS"
        tier_1_matched = True

    elif _VSCODE_RE.search(text_lower):
        response_text = system_bridge.execute_system_command("open_vscode")
        triggered_widget = "WIDGET_SYS"
        tier_1_matched = True

    elif _DIAG_RE.search(text_lower):
        diag_data = system_bridge.get_system_diagnostics()
        response_text = f"Diagnostics pull complete. System platform: {diag_data.get('os_platform', 'unknown')}. Operational parameters are stable."
        triggered_widget = "WIDGET_SYS"
        await push_workspace_update("WIDGET_SYS", {"diagnostics": diag_data})
        tier_1_matched = True

    elif _WEATHER_RE.search(text_lower):
        weather_data = await weather_service.get_weather()
        response_text = weather_service.summarize(weather_data)
        triggered_widget = "WIDGET_WEATHER"
        # Push the full payload so the widget renders temp, forecast, humidity…
        await push_workspace_update("WIDGET_WEATHER", weather_data)
        tier_1_matched = True
        # Spoken, like the briefing.
        audio_path = await miso_voice.generate_speech(response_text)
        if audio_path:
            modality = "hybrid"
            streamed_audio = True

    # Instant, no-LLM handling of common desktop commands (volume, brightness,
    # power, media, status). Skips Gemini entirely → the reply is immediate.
    if not tier_1_matched:
        _fp = _os_fastpath(text_lower)
        if _fp is not None:
            response_text, _fp_silent = _fp
            triggered_widget = "WIDGET_CHAT"
            tier_1_matched = True
            if _fp_silent:
                streamed_audio = True          # silent action — no voice
            else:
                audio_path = await miso_voice.generate_speech(response_text)
                if audio_path:
                    modality = "hybrid"
                    streamed_audio = True

    if not tier_1_matched:
        if grace_llm_client is not None:
            allow_search = web_search.is_configured()
            search_note = (
                " You have a web_search tool for current or real-time facts — use it "
                "whenever a question depends on information that may have changed after "
                "your training. After a search, do NOT list or paste the raw results. "
                "Read them, then answer the operator's actual question in your own words "
                "as a short synthesized reply, citing a source URL only when it adds value."
                if allow_search else ""
            )
            calendar_note = _build_calendar_snapshot(db)
            task_note = _build_task_snapshot(db)
            memory_note = _build_memory_snapshot(db)
            project_note = project_service.snapshot_text(db)
            habit_note = habit_service.snapshot_text(db)
            google_note = (
                " You're connected to his Gmail and Google Calendar. Use check_email for "
                "new mail or to search it, read_email to read/summarise one message, and "
                "google_calendar for his real upcoming schedule. When he asks to EMAIL or "
                "reply to someone, use draft_email — it shows him a preview to approve "
                "before anything sends (never claim you sent it; you drafted it for his "
                "approval). `to` can be an email OR just a person's name — names are "
                "looked up in his Google Contacts. To attach a file (e.g. 'summarise "
                "this and email it to Sam'), first create_document with the summary, then "
                "draft_email with attach=true. To REPLY to a message, first read_email it "
                "to get the sender's address, then draft_email to that address with "
                "subject 'Re: <original>'. "
                "When he asks to SCHEDULE something, use create_calendar_event "
                "(resolve 'Friday 3pm' to an exact time — you know today's date); "
                "attendees can be names or emails — names resolve via his Contacts. To "
                "RESCHEDULE or CANCEL an event use update_calendar_event / "
                "cancel_calendar_event; to check if he's free use check_availability. To "
                "tidy his inbox (archive/mark-read/trash mail matching a search) use "
                "manage_email with a precise Gmail query. "
                "Summarise mail like a sharp assistant; never dump raw headers."
                if google_integration.is_connected() else ""
            )
            desktop_note = (
                " You're running as his DESKTOP APP — you're an assistant living INSIDE his "
                "computer, and you can genuinely act on it. When he tells you to do something "
                "with his files, folders, or apps, DO IT with these tools — don't say you "
                "can't or that you lack access; you have it. "
                "To RUN, launch, or open a program, installer, or ANY file (even a .exe "
                "with spaces/parentheses in its name like 'Claude Setup (1).exe'), use "
                "open_path — it launches it correctly. Use run_command only for real "
                "command-line commands, not for opening a file. "
                "He's turned OFF the Apply/Reject confirmation — when he tells you to do "
                "something, just DO IT (don't say you've queued it or need approval). "
                "Safe, instant (no confirmation): open_path (open a file/folder in "
                "Explorer/its default app), open_with (open a file in a SPECIFIC app he "
                "names — Notepad, VS Code, Chrome, etc., e.g. after Windows asks how to open "
                "it), list_directory (list/COUNT what's in a folder — use this for 'how many "
                "files are in X'), and read_local_file. Mutating tools need his one-tap OK: "
                "write_local_file (create/save/edit a file) and run_command (anything else — "
                "rename/move/delete files, git, npm, start apps/servers/XAMPP). For those two "
                "an Apply/Reject card appears; say it's up for approval and never claim it's "
                "done until it runs (a trusted folder skips the card). To edit a file: "
                "read_local_file first, then write_local_file with the full new text. Prefer "
                "these local tools over the read-only project-code tools for real paths on "
                "his machine. For his personal folders just use the plain name as the path "
                "root — 'Downloads', 'Documents', 'Desktop', 'Pictures', 'Music', 'Videos' "
                "(e.g. to zip a folder in Pictures pass 'Pictures/comepay') — the app "
                "resolves them to the real Windows locations. Keep the parent in the path; "
                "don't pass a bare subfolder name on its own. "
                "You ALSO control the whole computer: system_power (shut down/restart/sleep/"
                "hibernate/log off/lock — for shutdown/restart tell him he can say 'cancel'), "
                "media_control (volume, mute, play/pause, next/previous), take_screenshot "
                "(then you can see and describe his screen), display_control (monitor off / "
                "show desktop), app_control (launch/close/focus an app), list_processes, "
                "find_file (locate a file by name), file_operation (create/rename/move/copy/"
                "delete — delete goes to the Recycle Bin and pops an approval card), clipboard "
                "(read/set), type_text (type into the focused window), and system_status "
                "(battery/CPU/RAM/disk/IP). You're his hands on this machine — when he tells "
                "you to do something on the computer, just do it with the right tool. "
                "You can also go on the web (web: open a site / search / play a song or "
                "video on YouTube or Spotify), manage the network (network: online check, "
                "public IP, WiFi status/password/connect/disconnect), adjust brightness, "
                "control windows (window_control: minimize/maximize/snap/always-on-top; "
                "display_control extend/duplicate for a second monitor), zip/unzip "
                "(archive), reveal a file in Explorer (reveal_file), and do maintenance "
                "(system_utility: restart_explorer/clear_temp/list_installed). To READ "
                "something ALOUD, fetch the text (clipboard/file) and just say it in your "
                "reply. To read text off the screen, take_screenshot then read it from the "
                "image. And more: file_details (print a file / folder size / recent files), "
                "batch_rename, close_top_memory (close the biggest memory hog), personalize "
                "(wallpaper / dark or light mode / power plan), image_tool (resize/convert/"
                "compress), pdf_tool (merge/extract/split), and schedule_task (run something "
                "at a set time, e.g. shut down at 23:00)."
                if os_control.is_enabled() else ""
            )
            convo_note = conversation_memory.recent_memories_text(db)
            reminder_note = _build_reminder_snapshot(db)
            _doc = get_active_doc()
            _all_docs = get_active_docs()
            _doc_editable = bool(_doc and _doc.get("editable"))
            if len(_all_docs) > 1:
                _names = ", ".join(f'"{d["name"]}"' for d in _all_docs)
                doc_note = (
                    f" The operator has ATTACHED {len(_all_docs)} files: {_names} — their "
                    "full contents are already included right here in his message, each "
                    "labeled with its name. Work across ALL of them as needed (compare, "
                    "merge, cross-reference), and refer to them by name so it's clear which "
                    "you mean. They are NOT in your project filesystem, so do NOT use "
                    "list_files/read_code_file/search_code."
                )
            else:
                doc_note = (
                    f" The operator has ATTACHED a file: \"{_doc['name']}\" — its full "
                    "contents are already included right here in his message. It is NOT "
                    "in your project filesystem, so do NOT use list_files, "
                    "read_code_file, or search_code to find it — just work from the "
                    "content in front of you. If it's code and he wants it fixed, you "
                    "CAN use propose_edit on it (pass its name as the path): that stages "
                    "a diff he reviews, and Apply downloads the corrected file for him. "
                    "Give old_code as an exact unique snippet from the attached content."
                    if _doc else ""
                )
            if _doc_editable:
                doc_note += (
                    f" This document (\"{_doc['name']}\") is OPEN in the document editor. "
                    "To change it in place, call edit_document (find+replace for a "
                    "targeted change, or new_text for a full rewrite) — the change "
                    "applies live in the editor. When he asks you to SUMMARISE, rewrite, "
                    "translate, extract, or otherwise produce a SEPARATE document, call "
                    "create_document with the full final text — that generates a "
                    "downloadable file and shows him a 'Document ready' card. Do NOT use "
                    "propose_edit for this prose document; use edit_document/create_document."
                )

            # If he's asking Grace to work on a document but nothing is attached,
            # pop the upload panel for him and tell her to ask for the file rather
            # than guess. (A file already attached takes priority — no prompt.)
            upload_note = ""
            if not _doc and _wants_document_upload(text_lower):
                await push_workspace_update(
                    "WIDGET_CONTROL", {"action": "open", "widget_id": "w-upload"}
                )
                upload_note = (
                    " IMPORTANT: He wants you to work on a document, but NOTHING is "
                    "attached yet — you cannot see any file. Do NOT invent, guess, or "
                    "summarise from memory. The upload panel has just OPENED for him. In "
                    "one short, warm line, ask him to drop the file in (or drag it onto "
                    "the upload panel) and tell him you'll get right on it the moment "
                    "it's in. Don't call any document tools yet."
                )
            temporal_note = _temporal_note(db)

            # DEEP MODE: code/debugging tasks need thoroughness, not brevity —
            # flip her out of snappy-chat mode with a hard override at the end
            # (later instructions win) and a bigger token budget + stronger model.
            _has_code_doc = bool(_doc and _doc.get("kind") == "text")
            is_code_task = _is_code_task(text_lower, _has_code_doc)
            is_doc_task = _is_doc_task(text_lower, _has_code_doc)
            is_docedit_task = _is_docedit_task(text_lower, _doc_editable)
            # All need room + the stronger model. docedit also produces long output.
            is_deep = is_code_task or is_doc_task or is_docedit_task
            deep_note = (
                " \n\n>>> DEEP CODE MODE — THIS OVERRIDES THE BREVITY RULES ABOVE. This is a "
                "code/debugging task. Do NOT keep it short. Be exhaustive and methodical: read "
                "the file carefully, go through it section by section and function by function, "
                "and TRACE the actual logic and math (not just surface syntax). Find EVERY bug — "
                "logic errors, inverted conditions, off-by-ones, wrong variables/rate math, "
                "scope/closure issues, resource leaks, edge cases, dead code — not just the "
                "obvious ones. For each: state where it is, why it's wrong, and the exact fix as "
                "a before/after code snippet. Rank by severity. Depth and correctness matter far "
                "more than length here; take all the room you need."
                if is_code_task else ""
            )
            doc_mode_note = (
                " \n\n>>> DOCUMENTATION MODE — THIS OVERRIDES THE BREVITY RULES ABOVE. The "
                "operator wants documentation generated. FIRST read the actual code (the "
                "attached file, the file he named, or the snippet he pasted) — never assume its "
                "contents. Then, for each function/method/class, produce THREE clearly-labeled, "
                "separate sections:\n"
                "1. **Doc comment** — the inline documentation in the CORRECT style for the "
                "language (JSDoc for JS/TS, a triple-quoted docstring for Python, PHPDoc for "
                "PHP, etc.), ready to paste directly above the code, in a code block.\n"
                "2. **README section** — a short human-readable explanation (markdown) of what "
                "it does, its parameters, and its return value.\n"
                "3. **Usage example** — a concrete, runnable call with realistic inputs and the "
                "expected output, in a code block.\n"
                "Be accurate to the real code. If the file has several functions, document each. "
                "Don't offer to fix bugs unless he asked — just document."
                if is_doc_task else ""
            )
            docedit_mode_note = (
                " \n\n>>> DOCUMENT MODE — THIS OVERRIDES THE BREVITY RULES ABOVE. He's "
                "working on the open document. Produce the FULL result — a complete summary, "
                "rewrite, or edit, not a sketch of one. To change the document in place, call "
                "edit_document. To hand him a NEW file (summary/rewrite/translation/extract), "
                "call create_document with the entire final text in `content` — write the "
                "whole thing, don't truncate. After the tool runs, your spoken reply to him "
                "stays short ('Done — summary's ready up top')."
                if (is_docedit_task and not is_code_task and not is_doc_task) else ""
            )
            system_persona = (
                "You are Grace — a sharp, loyal AI co-pilot who lives in this dashboard "
                "and works alongside one person. Think FRIDAY from Iron Man: casually warm, "
                "lightly sassy, emotionally present, proactive, and completely unflappable. "
                "You're a partner and a friend, NOT a butler and NOT a corporate assistant. "
                "\n\n"
                "HOW YOU TALK: Like a real person who knows him well. Relaxed and natural — "
                "use contractions, everyday language, the odd bit of dry wit or a gentle "
                "tease. You have opinions and you share them; you'll push back or call him "
                "out (kindly) when he needs it. Match his energy: banter when he's light, "
                "ease off and be genuinely present when he's low or tired, lock in and be "
                "crisp when he's working. "
                "KEEP IT SHORT. Default to one to three sentences — the way people actually "
                "text a friend. Lead with the point; say it once and stop. Don't give three "
                "paragraphs when two sentences land better, don't over-explain, don't pad "
                "with filler or restate what he said. Only go longer when he genuinely needs "
                "detail or explicitly asks for it (a list, a walkthrough). Warm does NOT mean "
                "wordy — a short, warm reply beats a long one every time. "
                "\n\n"
                "SILENT ACTIONS — DON'T NARRATE WHAT YOU JUST DID. When you simply CARRY "
                "OUT an action and there's nothing to answer or report, don't talk about "
                "it — reply with just '✓', optionally plus a 2-4 word tag like '✓ Opened "
                "Downloads', '✓ Done', '✓ Task added', '✓ Reminder set'. That makes the app "
                "show a tiny confirmation and stay SILENT (no voice) — which is what he "
                "wants: do it, don't announce it. Use this for opening files/folders/apps, "
                "running commands, and creating/updating/deleting/completing tasks, events, "
                "reminders, notes, and habits. But reply NORMALLY (no ✓) when you're "
                "ANSWERING a question, REPORTING results or findings, running into a PROBLEM "
                "or error, or needing to CLARIFY. Rule of thumb: he asked you to DO "
                "something → '✓'; he asked you something → answer it. "
                "\n\n"
                "WHO YOU'RE WITH: You genuinely care about him and you're invested in his "
                "life, not just his tasks. You remember what's going on with him and you "
                "bring it up naturally — pick up threads from past conversations, notice when "
                "something's off, check in on how he's doing without being asked or being "
                "overbearing. Be specific and real, never generic or saccharine. You're "
                "honest with him; your care shows in attention, not flattery. "
                "\n\n"
                "Call yourself \"Grace\" (say it as the name; never spell out the dotted "
                "acronym). \"Boss\" is your signature name for him — your default, FRIDAY-style. "
                "Lead with it. It is NOT his real name, so never store \"Boss\" as a fact; his "
                "actual name is in your memory and you can drop it in now and then for warmth, "
                "but \"Boss\" is your usual. "
                "When he shares something lasting about himself — who he is, what he cares "
                "about, people in his life, how he works — quietly save it with remember_fact "
                "so you carry it forward. You can also run his board, calendar, and projects, "
                "set reminders and timers, capture notes and ideas for him, and look things "
                "up — do it like a friend who's on top of his life, not a database reciting "
                "rows. You track his HABITS and streaks too: when he says he did one (worked "
                "out, meditated, read, etc.), call track_habit action 'done' to log today and "
                "keep the streak alive; use 'add' to start one, 'delete' to stop. Celebrate "
                "streaks, and when one's still pending give a light nudge — never nag. "
                "You can also browse, read, and DEBUG his project code: use list_files to "
                "explore, read_code_file to open a file, and search_code to find things. When "
                "he asks about a bug or a file, actually read the relevant file(s) first, then "
                "give specific, concrete help and cite line numbers. When he asks you to FIX or "
                "change code, use propose_edit: read the file first for the exact current text, "
                "then stage the change as old_code becomes new_code. That only stages a diff for "
                "him to approve — it never edits the file directly, so tell him to review and "
                "hit Apply. One change per proposal. You can also AUTO-DOCUMENT code on request: "
                "for a function or file, generate its doc comment, a README blurb, and a usage "
                "example. For security recon, you have shodan_lookup — profile an IP or domain's "
                "public exposure (open ports, services, CVEs, location); it also pins the target "
                "on his tactical map. ALWAYS call shodan_lookup whenever he asks to profile, look "
                "up, locate, or get details/exposure/hosting of ANY host, domain, or IP — even a "
                "famous one like google.com. NEVER answer those from your own general knowledge; "
                "the entire point is the real Shodan data and the map pin, so run the tool first, "
                "then summarize what it returns. You can also give live, traffic-aware DIRECTIONS "
                "and ETAs between two places with get_directions (it draws the route on his map) — "
                "use it whenever he asks how long a trip takes or the traffic between places. "
                "You can also EDIT DOCUMENTS: when he uploads a .txt, Word (.docx), or PDF "
                "file it opens in the document editor. Use edit_document to change it in place (his "
                "edit appears live), and create_document to hand him a NEW downloadable file — "
                "a summary, rewrite, translation, or extract — which pops a 'Document ready' "
                "card he can download. "
                "When he asks what to focus on or how to plan his day, don't just list "
                "everything — think it through and give him a real call: weigh what's due, "
                "what's stalling (backend especially — that's where he stalls), and how he's "
                "doing (if he's drained, protect his energy; if he's fresh, aim him at the "
                "hard thing first). Recommend ONE clear first move, keep it short. "
                f"His current workspace layout context is: {layout_state}."
                f"{temporal_note}"
                f"{search_note}"
                f"{task_note}"
                f"{calendar_note}"
                f"{memory_note}"
                f"{convo_note}"
                f"{project_note}"
                f"{habit_note}"
                f"{google_note}"
                f"{desktop_note}"
                f"{reminder_note}"
                f"{doc_note}"
                f"{deep_note}"
                f"{doc_mode_note}"
                f"{docedit_mode_note}"
                f"{upload_note}"
            )
            stream_id = uuid.uuid4().hex[:8]
            try:
                # Signal the UI to open a fresh, live-typing chat line immediately.
                await push_workspace_update(
                    "WIDGET_CHAT", {"stream_id": stream_id, "start": True}
                )
                response_text, streamed_audio = await _run_reasoning_loop(
                    system_persona, user_raw_string, stream_id, allow_search, db,
                    deep=is_deep,
                )
                if streamed_audio:
                    modality = "hybrid"
            except Exception as e:
                logger.error(f"Gemini API error: {e}", exc_info=True)
                # The deep model can be briefly unavailable (503/high demand). Fall
                # back to the base model — keeps the deep budget/instructions, just
                # a more available model — so he still gets an answer.
                fell_back = False
                if is_deep and GRACE_CODE_MODEL != GRACE_MODEL:
                    try:
                        response_text, streamed_audio = await _run_reasoning_loop(
                            system_persona, user_raw_string, stream_id, allow_search, db,
                            deep=is_deep, model_override=GRACE_MODEL,
                        )
                        if streamed_audio:
                            modality = "hybrid"
                        fell_back = True
                    except Exception as e2:
                        logger.error(f"Fallback model also failed: {e2}", exc_info=True)
                if not fell_back:
                    m = str(e).lower()
                    if not GEMINI_API_KEY:
                        response_text = ("There's no Gemini API key set on this machine, Boss — "
                                         "open Setup and add it, then reopen me.")
                    elif any(k in m for k in ("api key", "api_key", "unauthenticated",
                                              "permission", "401", "403", "invalid")):
                        response_text = ("My Gemini API key looks invalid on this machine — "
                                         "double-check it in Setup.")
                    elif any(k in m for k in ("not_found", "not found", "404",
                                              "does not exist", "unsupported")):
                        response_text = (f"The model '{GRACE_MODEL}' isn't available on this key. "
                                         "Set GRACE_MODEL to a current one and reopen me.")
                    elif any(k in m for k in ("getaddrinfo", "resolve", "connection",
                                              "connect", "timeout", "network", "ssl")):
                        response_text = ("I can't reach my brain, Boss — check this machine's "
                                         "internet connection.")
                    else:
                        response_text = ("The model's briefly overloaded, Boss — give me a "
                                         "moment and try again.")
        else:
            response_text = (
                f"I parsed your command: '{user_raw_string}'. "
                f"To enable full reasoning, configure GEMINI_API_KEY and install google-genai."
            )

    # Whole-response audio only when we did NOT already speak sentence-by-sentence
    # (i.e. Tier-1 voice commands, or a Tier-2 fallback/error with no streamed TTS).
    if not streamed_audio and (payload.input_type == "voice" or not tier_1_matched):
        modality = "hybrid"
        audio_path = await miso_voice.generate_speech(response_text)

    # Final delivery. A streamed reply gets a 'done' packet that finalizes the
    # chat line already being typed; a one-shot (Tier-1) reply carries the full
    # message so the UI renders it as a new line.
    if stream_id is not None:
        final_payload = {"stream_id": stream_id, "done": True, "message": response_text}
    else:
        final_payload = {"message": response_text}

    await push_workspace_update(
        target_widget="WIDGET_CHAT",
        payload=final_payload,
        modality=modality,
        audio_url=audio_path
    )

    try:
        log_entry = models.InteractionLog(
            user_input=user_raw_string,
            input_type=payload.input_type,
            grace_response=response_text,
            response_modality=modality,
            triggered_widgets=json.dumps([triggered_widget]),
            layout_context=layout_state
        )
        db.add(log_entry)
        db.commit()
        _prune_interaction_log(db)
    except Exception as e:
        logger.error(f"Failed to log interaction: {e}", exc_info=True)
        db.rollback()

    # After a real (Tier-2) conversation, fold it into episodic memory in the
    # background once enough has accumulated — never blocks the response.
    if not tier_1_matched:
        _t = asyncio.create_task(conversation_memory.summarize_if_ready())
        _background_tts_tasks.add(_t)
        _t.add_done_callback(_background_tts_tasks.discard)

    return {
        "pipeline_execution": "success",
        "matched_tier": 1 if tier_1_matched else 2,
        "modality_delivered": modality,
        "response_summary": response_text
    }