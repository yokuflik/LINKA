"""Producer side of the agent invocation queue (ADR 0045, step 2).

A matched + quota-allowed trigger XADDs a tiny reference onto
``agent_invoke_stream``; the (not yet built) ``agent_worker`` consumer group
will load the agent/chat/message and run the Gemini turn. No consumer group
is created here yet - that lands with the worker in a later step.
"""
from typing import Optional

from config import settings
from infra.redis.client import redis_client


def _clean(value: Optional[object]) -> str:
    return "" if value is None else str(value)


async def enqueue_invocation(*, agent_id: int, chat_id: int, message_id: int) -> str:
    """Append one trigger match onto the agent invoke stream. Returns the
    stream entry id. Best-effort from the caller's point of view - the
    Trigger Rule Engine never lets a failure here affect message delivery."""
    return await redis_client.xadd(
        settings.AGENT_INVOKE_STREAM_KEY,
        {
            "agent_id": _clean(agent_id),
            "chat_id": _clean(chat_id),
            "message_id": _clean(message_id),
            "kind": "message",
        },
        maxlen=settings.AGENT_INVOKE_STREAM_MAXLEN,
        approximate=True,
    )


async def enqueue_knowledge_event(*, agent_id: int, chat_id: int, instruction: str) -> str:
    """ADR 0085: append a knowledge-base ingestion outcome (success or PDF
    parse failure) onto the same agent_invoke_stream, tagged kind=knowledge
    so process_entry seeds the turn from the free-text instruction built by
    the caller (router.py) instead of chat history. chat_id is always
    agent.owner_agent_chat_id - the turn always lands in config-mode."""
    return await redis_client.xadd(
        settings.AGENT_INVOKE_STREAM_KEY,
        {
            "agent_id": _clean(agent_id),
            "chat_id": _clean(chat_id),
            "instruction": instruction,
            "kind": "knowledge",
        },
        maxlen=settings.AGENT_INVOKE_STREAM_MAXLEN,
        approximate=True,
    )


async def enqueue_schedule_fire(*, agent_id: int, schedule_id: str) -> str:
    """ADR 0046 decision 3: append a due on_schedule entry onto the same
    agent_invoke_stream, tagged kind=schedule so process_entry seeds the turn
    from the entry's instruction instead of chat history. process_entry
    re-loads the live entry itself (including any scoped_system_prompt, ADR
    0061) rather than trusting anything carried on the stream payload - only
    agent_id/schedule_id are needed here to look it up."""
    return await redis_client.xadd(
        settings.AGENT_INVOKE_STREAM_KEY,
        {
            "agent_id": _clean(agent_id),
            "schedule_id": schedule_id,
            "kind": "schedule",
        },
        maxlen=settings.AGENT_INVOKE_STREAM_MAXLEN,
        approximate=True,
    )
