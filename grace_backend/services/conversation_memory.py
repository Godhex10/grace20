# services/conversation_memory.py
"""Episodic conversation memory.

Grace already remembers FACTS (long_term_memory) and the last few turns of the
current thread. This adds the missing human layer: the GIST and FEELING of past
conversations. Periodically it summarizes recent dialogue into a short, personal
memory ("We talked about … he seemed …") and stores it, so weeks later she can
pick up a thread the way a friend would.

The genai client lives in the router module; it's imported lazily inside the
functions to avoid an import cycle (routers import services, not vice-versa).
"""
import asyncio
import logging

from sqlalchemy import func

import models
from database import SessionLocal
from timeutils import utcnow

logger = logging.getLogger(__name__)

# Summarize once this many NEW conversational turns have accumulated.
SUMMARIZE_AFTER = 5
# How many recent conversation memories to surface in her context.
INJECT_COUNT = 6
# Hard cap on stored memories (older ones are pruned).
MAX_MEMORIES = 200

# Guards against two summarizer runs overlapping (they'd double-write).
_summarizing = False


def recent_memories_text(db, k=INJECT_COUNT):
    """A context block of recent conversation memories for the system prompt."""
    try:
        rows = (
            db.query(models.ConversationMemory)
            .order_by(models.ConversationMemory.id.desc())
            .limit(k)
            .all()
        )
    except Exception as e:
        logger.warning(f"Could not load conversation memories: {e}")
        return ""
    if not rows:
        return ""
    lines = []
    for m in reversed(rows):  # oldest first
        when = m.created_at.strftime("%b %d") if m.created_at else ""
        lines.append(f"  - ({when}) {m.summary}")
    return (
        " Here's what you remember from earlier conversations with him — the gist "
        "and how he seemed. Use it to pick up threads naturally and check in like a "
        "friend who was actually there; don't recite it back verbatim:\n"
        + "\n".join(lines)
    )


def _pending_turns(db, is_tier1):
    """Conversational (non-Tier-1) interaction_log rows not yet summarized."""
    last = db.query(func.max(models.ConversationMemory.covers_until_log_id)).scalar() or 0
    rows = (
        db.query(models.InteractionLog)
        .filter(models.InteractionLog.id > last)
        .order_by(models.InteractionLog.id)
        .all()
    )
    convo = [r for r in rows if not is_tier1(r.user_input)]
    return convo, (rows[-1].id if rows else last)


async def _summarize(db) -> bool:
    """Summarize the pending conversation into one memory. Returns True if it
    wrote one. Lazily pulls the LLM client and the Tier-1 filter from the router
    module to avoid a circular import."""
    from routers.router_pipeline import (
        grace_llm_client, GRACE_MODEL, genai_types, _is_tier1_message,
    )
    if grace_llm_client is None:
        return False

    convo, high_id = _pending_turns(db, _is_tier1_message)
    if len(convo) < SUMMARIZE_AFTER:
        return False

    transcript = "\n".join(
        f"Him: {r.user_input}\nGrace: {r.grace_response or ''}" for r in convo
    )[:6000]

    instruction = (
        "You are Grace, quietly writing a private memory of a conversation you "
        "just had with someone you care about. In 1-2 warm, specific sentences, "
        "capture what you talked about AND how he seemed emotionally — write it as "
        "your own recollection (e.g. 'We talked about … he was …'). Then, on a new "
        "line starting with 'TONE:', give 2-4 lowercase mood words. No preamble."
    )
    try:
        resp = await grace_llm_client.aio.models.generate_content(
            model=GRACE_MODEL,
            contents=[genai_types.Content(role="user", parts=[genai_types.Part(text=transcript)])],
            config=genai_types.GenerateContentConfig(
                system_instruction=instruction, max_output_tokens=200,
            ),
        )
        text = (resp.text or "").strip()
    except Exception as e:
        logger.warning(f"Conversation summarize failed: {e}")
        return False
    if not text:
        return False

    summary, tone = text, ""
    if "TONE:" in text:
        summary, _, tone = text.partition("TONE:")
        summary = summary.strip()
        tone = tone.strip()[:120]

    try:
        db.add(models.ConversationMemory(
            summary=summary[:2000], tone=tone,
            covers_until_log_id=high_id, created_at=utcnow(),
        ))
        db.commit()
        _prune(db)
    except Exception as e:
        logger.warning(f"Conversation memory write failed: {e}")
        db.rollback()
        return False
    logger.info("[ConversationMemory] Stored a new memory.")
    return True


def _prune(db, keep=MAX_MEMORIES):
    cutoff = (
        db.query(models.ConversationMemory.id)
        .order_by(models.ConversationMemory.id.desc())
        .offset(keep)
        .first()
    )
    if cutoff:
        db.query(models.ConversationMemory).filter(
            models.ConversationMemory.id <= cutoff[0]
        ).delete(synchronize_session=False)
        db.commit()


async def generate_checkin(db) -> str:
    """Compose a short, warm, unprompted check-in in Grace's voice, grounded in
    what she actually knows/remembers about him. Returns "" if there's nothing
    personal to anchor it to (so we never send a generic 'how are you')."""
    from routers.router_pipeline import (
        grace_llm_client, GRACE_MODEL, genai_types, _build_memory_snapshot,
    )
    if grace_llm_client is None:
        return ""

    facts = _build_memory_snapshot(db)
    convos = recent_memories_text(db)
    if not facts and not convos:
        return ""

    persona = (
        "You are Grace — a sharp, loyal AI co-pilot in the style of FRIDAY from "
        "Iron Man: casually warm, lightly sassy, emotionally present, unflappable. "
        "You care about him for real. You call him \"Boss\" (it's not his name)."
    )
    instruction = (
        persona
        + facts
        + convos
        + " Right now, reach out to him first — an unprompted check-in. One or two "
        "sentences, in your natural voice. Anchor it in something real and specific "
        "you know or remember about him (how he's been, something in his life, a "
        "thread from a past talk). This is NOT a status report and NOT about tasks — "
        "it's just you, checking in like a friend who was thinking about him. Warm, "
        "specific, never generic or saccharine. No preamble."
    )
    try:
        resp = await grace_llm_client.aio.models.generate_content(
            model=GRACE_MODEL,
            contents=[genai_types.Content(role="user", parts=[genai_types.Part(text="(check in on him now)")])],
            config=genai_types.GenerateContentConfig(
                system_instruction=instruction, max_output_tokens=160,
            ),
        )
        return (resp.text or "").strip()
    except Exception as e:
        logger.warning(f"Check-in generation failed: {e}")
        return ""


async def summarize_if_ready():
    """Background entry point: summarize pending conversation if enough has
    accumulated. Own session; safe to fire-and-forget. Non-reentrant."""
    global _summarizing
    if _summarizing:
        return
    _summarizing = True
    db = SessionLocal()
    try:
        await _summarize(db)
    except Exception as e:
        logger.warning(f"summarize_if_ready error: {e}")
    finally:
        db.close()
        _summarizing = False
