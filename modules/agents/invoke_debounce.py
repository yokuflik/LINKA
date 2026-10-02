"""Message-batch debounce/coalescing for agent_invoke_stream (ADR 0063).

A matched trigger no longer enqueues onto agent_invoke_stream directly - it
schedules a due member on `agent_invoke_debounce_due` (this module), score =
now + AGENT_INVOKE_DEBOUNCE_SECONDS. A second match for the same
(agent_id, chat_id) before that fires just overwrites the score (plain
ZADD), coalescing a fast burst/self-correction into a single turn instead of
two racing ones. Same due-ZSET pattern as modules.agents.schedule's
AGENT_SCHEDULE_DUE_ZSET_KEY, polled by its own tight loop in invoke_worker.

Also holds the ADR 00732 supersede flag: set alongside the turn-mutex re-arm
path when a new message arrives while a previous turn for the same pair is
already running, so that in-flight turn can notice and end without
delivering its (now-stale) reply.

ADR 0075 adds two things on top: `arm_debounce_now` (re-fire immediately
instead of waiting out another full debounce window - used once a
mid-turn supersede has already implicitly merged the newer message via the
replacement turn's own fresh history read) and the `agent_typing_active`
marker, which lets the peer-visible typing indicator's lifetime span
"there is unanswered agent activity for this chat" rather than "this one
turn object is still alive" - so a superseded turn's indicator hand-off to
its replacement has no visible gap.

ADR 0077 adds `is_turn_running`, a read-only check against the turn lock -
trigger_engine._evaluate_triggers calls it before arming the debounce so a
second message can detect an already-running turn immediately (one Redis
GET) instead of waiting for the next debounce cycle's acquire_turn_lock
failure in process_entry to discover the conflict.
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


async def arm_debounce(
    agent_id: int, chat_id: int, message_id: Optional[int] = None, *, keep_newer: bool = False
) -> None:
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
                # keep_newer: re-arming a popped entry must never clobber a
                # newer message stashed since (ADR 0103).
                nx=keep_newer,
            )
        await redis_client.zadd(
            settings.AGENT_INVOKE_DEBOUNCE_ZSET_KEY,
            {_member(agent_id, chat_id): time.time() + settings.AGENT_INVOKE_DEBOUNCE_SECONDS},
        )
    except Exception:
        logger.exception("agent invoke debounce arm failed for agent %s chat %s", agent_id, chat_id)


async def arm_debounce_now(agent_id: int, chat_id: int) -> None:
    """ADR 0075: re-fires this pair immediately (score = now) instead of
    waiting out another full AGENT_INVOKE_DEBOUNCE_SECONDS window - used only
    right after a mid-turn supersede (case B), where the replacement turn's
    own `_build_initial_contents` will already read history fresh (including
    whatever newer message caused the supersede), so there is nothing left
    to gain by coalescing further. Never touches the stashed message_id
    (whatever `mark_superseded`'s caller already stored via a plain
    `arm_debounce` call stays as-is)."""
    try:
        await redis_client.zadd(
            settings.AGENT_INVOKE_DEBOUNCE_ZSET_KEY, {_member(agent_id, chat_id): time.time()}
        )
    except Exception:
        logger.exception("agent invoke immediate re-arm failed for agent %s chat %s", agent_id, chat_id)


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


async def is_turn_running(agent_id: int, chat_id: Optional[int]) -> bool:
    """ADR 0077: read-only existence check against the turn lock, called from
    trigger_engine._evaluate_triggers before arming the debounce - lets a
    second message detect an already-running turn immediately (a single
    Redis GET) instead of waiting for the next debounce cycle to rediscover
    it via a failed acquire_turn_lock in process_entry. Never acquires or
    releases the lock itself - only the worker does that."""
    try:
        return bool(await redis_client.exists(_lock_key(agent_id, chat_id)))
    except Exception:
        logger.exception("agent turn running check failed for agent %s chat %s", agent_id, chat_id)
        return False


async def release_turn_lock(agent_id: int, chat_id: Optional[int]) -> None:
    try:
        await redis_client.delete(_lock_key(agent_id, chat_id))
    except Exception:
        logger.exception("agent turn lock release failed for agent %s chat %s", agent_id, chat_id)


def _superseded_key(agent_id: int, chat_id: Optional[int]) -> str:
    return f"{settings.AGENT_TURN_SUPERSEDED_KEY_PREFIX}:{agent_id}:{chat_id}"


async def mark_superseded(agent_id: int, chat_id: Optional[int]) -> None:
    """ADR 00732: set the moment a new message matches a trigger while the
    turn lock for this pair is already held - the in-flight turn checks this
    flag and, if set, ends without delivering its reply (does not attempt to
    cancel the underlying Gemini call itself). TTL mirrors the turn lock so a
    flag nobody ever reads self-heals on the same bound."""
    try:
        await redis_client.set(
            _superseded_key(agent_id, chat_id),
            "1",
            ex=int(settings.AGENT_TURN_TIMEOUT_SECONDS),
        )
    except Exception:
        logger.exception("agent turn supersede mark failed for agent %s chat %s", agent_id, chat_id)


async def is_superseded(agent_id: int, chat_id: Optional[int]) -> bool:
    """Atomic get-and-delete so the flag can never leak into a later,
    unrelated turn for the same pair."""
    try:
        value = await redis_client.getdel(_superseded_key(agent_id, chat_id))
    except Exception:
        logger.exception("agent turn supersede check failed for agent %s chat %s", agent_id, chat_id)
        return False
    return value is not None


def _typing_active_key(chat_id: int) -> str:
    return f"{settings.AGENT_TYPING_ACTIVE_KEY_PREFIX}:{chat_id}"


async def claim_typing_indicator(chat_id: int) -> bool:
    """ADR 0075: SET NX so only the first turn touching this chat while
    activity is unanswered starts (and owns) the peer-visible typing loop -
    a turn that gets superseded before delivering anything must not tear the
    indicator down and leave a gap before its replacement's own loop spins
    up. TTL is a self-healing bound in case the owning turn crashes before
    releasing it; the owning loop refreshes it on every publish tick so it
    never expires mid-turn."""
    try:
        return bool(
            await redis_client.set(
                _typing_active_key(chat_id), "1", nx=True, ex=int(settings.AGENT_TURN_TIMEOUT_SECONDS)
            )
        )
    except Exception:
        logger.exception("agent typing indicator claim failed for chat %s", chat_id)
        return True  # fail open: better a duplicate loop than none at all


async def refresh_typing_indicator(chat_id: int) -> None:
    try:
        await redis_client.expire(_typing_active_key(chat_id), int(settings.AGENT_TURN_TIMEOUT_SECONDS))
    except Exception:
        logger.exception("agent typing indicator refresh failed for chat %s", chat_id)


async def release_typing_indicator(chat_id: int) -> None:
    try:
        await redis_client.delete(_typing_active_key(chat_id))
    except Exception:
        logger.exception("agent typing indicator release failed for chat %s", chat_id)
