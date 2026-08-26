import json
from typing import Any, AsyncIterator

from services.redis_client import redis_client

_CHANNEL_PREFIX = "chat_events:"


def _channel(chat_id: int) -> str:
    return f"{_CHANNEL_PREFIX}{chat_id}"


async def publish_event(chat_id: int, event: dict[str, Any]) -> None:
    """
    Fans a single event (new message, edit, delete, read-receipt, typing...)
    out to every FastAPI instance subscribed to this chat's channel - this is
    the piece that lets a 500-member group message avoid a 500-iteration loop
    on the server that received it: that server publishes once, and every
    instance with a locally-connected member of the chat delivers it to just
    its own WebSocket clients.
    """
    await redis_client.publish(_channel(chat_id), json.dumps(event))


async def subscribe_to_chat(chat_id: int) -> AsyncIterator[dict[str, Any]]:
    """
    Per-chat subscription, used when at least one of this chat's members is
    connected to this particular server instance. Yields decoded events as
    they arrive; the caller (the WebSocket handler) is responsible for
    routing each event to its own locally-connected clients and for closing
    the subscription (via `.aclose()` on the returned generator, e.g. through
    `async with contextlib.aclosing(...)`) when the last local member leaves.
    """
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(_channel(chat_id))
    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            yield json.loads(message["data"])
    finally:
        await pubsub.unsubscribe(_channel(chat_id))
        await pubsub.aclose()
