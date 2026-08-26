import json
from typing import Any, AsyncIterator

from services.redis_client import redis_client

_CHANNEL_PREFIX = "chat_events:"
_USER_CHANNEL_PREFIX = "user_events:"
_PRESENCE_CHANNEL_PREFIX = "presence_events:"


def _channel(chat_id: int) -> str:
    return f"{_CHANNEL_PREFIX}{chat_id}"


def _user_channel(user_id: int) -> str:
    return f"{_USER_CHANNEL_PREFIX}{user_id}"


def _presence_channel(user_id: int) -> str:
    return f"{_PRESENCE_CHANNEL_PREFIX}{user_id}"


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


async def publish_user_event(user_id: int, event: dict[str, Any]) -> None:
    """
    A per-user channel, separate from any chat's - for events about a user's
    own membership changing (e.g. "you were just added to chat X") rather
    than events happening inside a chat they're already subscribed to.
    This is what makes a *newly* created chat reach an already-connected
    client at all: connection_manager only subscribes a connection to the
    chats that existed at connect time, so without a signal on a channel
    that connection was already listening to, a chat created afterward is
    simply invisible to it until the next reconnect.
    """
    await redis_client.publish(_user_channel(user_id), json.dumps(event))


async def subscribe_to_user(user_id: int) -> AsyncIterator[dict[str, Any]]:
    """Same contract as subscribe_to_chat, for a user's personal channel."""
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(_user_channel(user_id))
    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            yield json.loads(message["data"])
    finally:
        await pubsub.unsubscribe(_user_channel(user_id))
        await pubsub.aclose()


async def publish_presence_event(user_id: int, event: dict[str, Any]) -> None:
    """
    One channel per user whose presence might be watched - deliberately NOT
    the same channel as publish_user_event, so a presence_update can never
    be confused with (or accidentally broadcast alongside) that user's own
    "you were added/removed from a chat" events. A publish here reaches
    subscribers only if someone is actually subscribed via
    connection_manager.subscribe_presence right now (subscribe-on-demand -
    see CLAUDE.md's presence architecture section); with none, this is a
    publish to a channel with zero listeners.
    """
    await redis_client.publish(_presence_channel(user_id), json.dumps(event))


async def subscribe_to_presence(user_id: int) -> AsyncIterator[dict[str, Any]]:
    """Same contract as subscribe_to_chat, for one user's presence channel."""
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(_presence_channel(user_id))
    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            yield json.loads(message["data"])
    finally:
        await pubsub.unsubscribe(_presence_channel(user_id))
        await pubsub.aclose()
