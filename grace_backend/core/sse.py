import asyncio
import json
import logging
import time
from typing import Dict, Any, AsyncGenerator, Set

logger = logging.getLogger(__name__)

# One queue PER connected client. push_workspace_update fans an event out to
# every subscriber, so multiple tabs (and transient reconnect overlaps) each
# receive a full copy of the stream. A single shared queue would let one
# consumer STEAL packets meant for another — e.g. text deltas landing on a
# zombie connection while the visible tab only ever got the audio chunks.
_subscribers: Set["asyncio.Queue"] = set()

# Kept for backwards-compat imports; no longer the delivery mechanism.
workspace_broadcast_queue: asyncio.Queue = asyncio.Queue(maxsize=1000)

HEARTBEAT_INTERVAL = 15
_MAX_QUEUE = 1000


def subscriber_count() -> int:
    """Number of live SSE connections right now — one per open dashboard context."""
    return len(_subscribers)


async def event_generator() -> AsyncGenerator[str, None]:
    # Each client gets its own private queue, registered for the lifetime of
    # the connection and removed the moment it disconnects.
    queue: asyncio.Queue = asyncio.Queue(maxsize=_MAX_QUEUE)
    _subscribers.add(queue)
    logger.info(f"[SSE] Client subscribed. Active subscribers: {len(_subscribers)}")
    last_heartbeat = time.time()
    try:
        while True:
            try:
                event_data = await asyncio.wait_for(queue.get(), timeout=1.0)
                yield f"data: {json.dumps(event_data)}\n\n"
                queue.task_done()
                last_heartbeat = time.time()
            except asyncio.TimeoutError:
                if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL:
                    yield ": heartbeat\n\n"
                    last_heartbeat = time.time()
    except asyncio.CancelledError:
        logger.info("[SSE Multiplexer] Client disconnected from live stream workspace channel.")
        raise
    except Exception as e:
        logger.error(f"[SSE Multiplexer] Error in event generator: {e}", exc_info=True)
        raise
    finally:
        _subscribers.discard(queue)
        logger.info(f"[SSE] Client unsubscribed. Active subscribers: {len(_subscribers)}")


async def push_workspace_update(
    target_widget: str,
    payload: Dict[str, Any],
    modality: str = "widget_only",
    audio_url: str = ""
):
    event_packet = {
        "target_widget": target_widget,
        "response_modality": modality,
        "audio_url": audio_url,
        "data": payload
    }
    if not _subscribers:
        # No one is listening — nothing to do, but don't error the pipeline.
        return
    # Deliver a copy to every connected client. A full queue on one slow client
    # must not block or drop events for the others.
    for queue in list(_subscribers):
        try:
            queue.put_nowait(event_packet)
        except asyncio.QueueFull:
            logger.warning(f"[SSE] Subscriber queue full, dropping event for {target_widget}")
