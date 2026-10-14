"""Producer side of the trigger-evaluation stream (ADR 0106).

The fan-out worker already holds a chat's participants, so it is the cheapest
place to ask "does any recipient own an enabled agent?" - one SMISMEMBER on the
enabled-owners SET. Only on a hit is a reference XADDed to
``agent_trigger_stream`` for the agent_worker's AgentTriggerConsumer.
"""
import logging
from typing import Optional, Sequence

from config import settings
from infra.redis.client import redis_client

logger = logging.getLogger(__name__)


async def any_enabled_owner(candidate_user_ids: Sequence[int]) -> bool:
    if not candidate_user_ids:
        return False
    hits = await redis_client.smismember(settings.AGENT_ENABLED_OWNERS_SET_KEY, list(candidate_user_ids))
    return any(hits)


async def maybe_enqueue_trigger_eval(
    *, message_id: int, chat_id: int, sender_id: Optional[int], recipient_ids: Sequence[int]
) -> bool:
    """Best-effort: never raises (a failure must not affect message delivery).
    Returns True if an entry was enqueued. A chat with no recipients (the
    owner<->agent chat) is gated on the sender instead."""
    try:
        candidates = list(recipient_ids) or ([sender_id] if sender_id is not None else [])
        if not await any_enabled_owner(candidates):
            return False
        await redis_client.xadd(
            settings.AGENT_TRIGGER_STREAM_KEY,
            {"message_id": str(message_id), "chat_id": str(chat_id)},
            maxlen=settings.AGENT_TRIGGER_STREAM_MAXLEN,
            approximate=True,
        )
        return True
    except Exception:
        logger.exception("agent trigger enqueue failed for message %s", message_id)
        return False
