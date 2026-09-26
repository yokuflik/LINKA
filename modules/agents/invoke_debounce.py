"""Message-batch debounce/coalescing for agent_invoke_stream (ADR 0063).

A matched trigger no longer enqueues onto agent_invoke_stream directly - it
schedules a due member on `agent_invoke_debounce_due` (this module), score =
now + AGENT_INVOKE_DEBOUNCE_SECONDS. A second match for the same
(agent_id, chat_id) before that fires just overwrites the score (plain
ZADD), coalescing a fast burst/self-correction into a single turn instead of
two racing ones. Same due-ZSET pattern as modules.agents.schedule's
AGENT_SCHEDULE_DUE_ZSET_KEY, polled by its own tight loop in invoke_worker.
"""
import logging
import time
from typing import Optional

from config import settings
from infra.redis.client import redis_client

logger = logging.getLogger(__name__)


def _member(agent_id: int, chat_id: int) -> str:
    return f"{agent_id}:{chat_id}"


def _split_member(member: str) -> tuple[int, int]:
    agent_id, chat_id = member.split(":", 1)
    return int(agent_id), int(chat_id)


def _message_key(agent_id: int, chat_id: int) -> str:
    # Separate STRING per pair rather than a value packed onto the ZSET
    # member itself (ZSET scores/members can't carry structured payload) -
    # holds the latest matched message_id so the poll loop knows what to
    # enqueue without a DB round trip. Overwritten on every re-arm, so it
    # always reflects the most recent message in the coalesced batch.
    return f"agent_invoke_debounce_msg:{agent_id}:{chat_id}"


async def arm_debounce(agent_id: int, chat_id: int, message_id: Optional[int] = None) -> None:
    """(Re)schedules the fire time for this (agent_id, chat_id) pair
    AGENT_INVOKE_DEBOUNCE_SECONDS out from now, and remembers `message_id` as
    the one to enqueue when it fires - called from both a fresh trigger
    match and the turn-mutex's re-arm path (which omits message_id, keeping
    whatever was last recorded). Best-effort: a failure here must never
    affect message delivery (same fire-and-forget contract as
    enqueue_invocation)."""
    try:
        if message_id is not None:
            await redis_client.set(
                _message_key(agent_id, chat_id),
                str(message_id),
                ex=int(settings.AGENT_INVOKE_DEBOUNCE_SECONDS) + 60,
            )
        await redis_client.zadd(
            settings.AGENT_INVOKE_DEBOUNCE_ZSET_KEY,
            {_member(agent_id, chat_id): time.time() + settings.AGENT_INVOKE_DEBOUNCE_SECONDS},
        )
    except Exception:
        logger.exception("agent invoke debounce arm failed for agent %s chat %s", agent_id, chat_id)


async def due_pairs(now: Optional[float] = None) -> list[tuple[int, int]]:
    """Pops (atomically removes + returns) every (agent_id, chat_id) pair due
    to fire at or before `now` (default: current time)."""
    cutoff = now if now is not None else time.time()
    members = await redis_client.zrangebyscore(
        settings.AGENT_INVOKE_DEBOUNCE_ZSET_KEY, "-inf", cutoff
    )
    if not members:
        return []
    await redis_client.zrem(settings.AGENT_INVOKE_DEBOUNCE_ZSET_KEY, *members)
    return [_split_member(m) for m in members]


async def pop_latest_message_id(agent_id: int, chat_id: int) -> Optional[int]:
    """Reads + clears the message_id stashed for this pair by arm_debounce -
    called once, right before enqueueing the fired turn."""
    key = _message_key(agent_id, chat_id)
    value = await redis_client.get(key)
    if value is None:
        return None
    await redis_client.delete(key)
    return int(value)


def _lock_key(agent_id: int, chat_id: Optional[int]) -> str:
    return f"{settings.AGENT_TURN_LOCK_KEY_PREFIX}:{agent_id}:{chat_id}"


async def acquire_turn_lock(agent_id: int, chat_id: Optional[int]) -> bool:
    """Per-(agent_id, chat_id) mutex (ADR 0063) held for the duration of one
    _run_turn call - `chat_id=None` (schedule-fired turns with no chat
    target) never contends with anything else, same reasoning as the
    config-mode gate treating a None chat_id as its own case. TTL equals
    AGENT_TURN_TIMEOUT_SECONDS so a crashed worker holding the lock
    self-heals on the same bound the turn itself is already capped at."""
    return bool(
        await redis_client.set(
            _lock_key(agent_id, chat_id),
            "1",
            nx=True,
            ex=int(settings.AGENT_TURN_TIMEOUT_SECONDS),
        )
    )


async def release_turn_lock(agent_id: int, chat_id: Optional[int]) -> None:
    try:
        await redis_client.delete(_lock_key(agent_id, chat_id))
    except Exception:
        logger.exception("agent turn lock release failed for agent %s chat %s", agent_id, chat_id)
