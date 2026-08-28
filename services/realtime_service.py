import json
from typing import Any, AsyncIterator

from services.redis_client import redis_client

_USER_CHANNEL_PREFIX = "user_events:"
_PRESENCE_CHANNEL_PREFIX = "presence_events:"
_INSTANCE_INBOX_PREFIX = "instance_inbox:"


def _instance_inbox_channel(server_id: str) -> str:
    return f"{_INSTANCE_INBOX_PREFIX}{server_id}"


def _user_channel(user_id: int) -> str:
    return f"{_USER_CHANNEL_PREFIX}{user_id}"


def _presence_channel(user_id: int) -> str:
    return f"{_PRESENCE_CHANNEL_PREFIX}{user_id}"


async def publish_event(chat_id: int, event: dict[str, Any]) -> None:
    """
    Fans a single chat-scoped event (new message, edit, delete, read-receipt,
    typing...) out to exactly the FastAPI processes that currently serve this
    chat - i.e. have a locally-connected member of it.

    Routing layer (FANOUT_REWRITE_PLAN.md step 3): instead of publishing to a
    per-chat channel every process subscribes to and filters, we look up the
    serving processes in ``routing.instances_for_chat`` and publish once to
    each one's personal inbox channel. A 500-member group whose members sit on
    3 processes costs 3 publishes, not one-per-subscribed-process.

    The event carries ``chat_id`` so the receiving process routes it to its
    own local subscribers of that chat.
    """
    # Imported here, not at module load, to avoid a circular import
    # (routing -> redis_client is fine, but keep the dependency direction of
    # services/fanout -> realtime_service one-way).
    from services.fanout import routing

    event = {**event, "chat_id": str(chat_id)}
    payload = json.dumps(event)
    server_ids = await routing.instances_for_chat(chat_id)
    for server_id in server_ids:
        await redis_client.publish(_instance_inbox_channel(server_id), payload)


async def publish_to_instance(server_id: str, event: dict[str, Any]) -> None:
    await redis_client.publish(_instance_inbox_channel(server_id), json.dumps(event))


async def subscribe_to_instance_inbox(server_id: str) -> AsyncIterator[dict[str, Any]]:
    """
    One channel per process. Every chat-scoped event destined for any chat
    this process serves arrives here; the consumer (connection_manager)
    dispatches by ``event["chat_id"]`` to local subscribers. Same generator
    contract as the old ``subscribe_to_chat``.
    """
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(_instance_inbox_channel(server_id))
    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            yield json.loads(message["data"])
    finally:
        await pubsub.unsubscribe(_instance_inbox_channel(server_id))
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
