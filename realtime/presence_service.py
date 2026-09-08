from datetime import datetime, timezone

from services import realtime_service
from services.redis_client import redis_client

# "Online" means an open, foreground WebSocket connection - exactly like
# WhatsApp: having the app installed, or even backgrounded, is not enough.
# The presence key only exists for as long as a connection is actually live,
# so there's nothing to interpret - the key's mere existence *is* "online".

# Safety net against a connection that dies without a clean disconnect event
# (app killed, phone loses signal): the key expires on its own so a crashed
# client doesn't stay "online" forever. Refreshed periodically by the
# WebSocket handler while the connection is alive (a heartbeat/ping).
_PRESENCE_TTL_SECONDS = 60

_KEY_PREFIX = "presence:"          # presence:{user_id} -> set of connection_ids
_SERVER_KEY_PREFIX = "presence_srv:"  # presence_srv:{user_id}:{connection_id} -> server_id
_LAST_SEEN_KEY_PREFIX = "presence_last_seen:"  # presence_last_seen:{user_id} -> ISO timestamp, no TTL

# "Last seen" tracks the moment of the user's most recently connected device:
# it is refreshed on every connect and on every disconnect of *any* device
# (not just the 0-connections edge). While the user is online the value keeps
# moving forward on each connect/heartbeat, so the instant the last device
# drops the stored timestamp is already "now" - the last device to have been
# connected is the one that sets it.

# Presence changes are published as targeted events (see realtime_service's
# per-user presence channel), never a global broadcast - only whoever has
# actually subscribed to *this specific* user's presence (via
# connection_manager.subscribe_presence, gated on sharing a private chat -
# see routers/websocket.py) has a live listener on that channel at all, so a
# publish with zero subscribers is effectively a no-op.


def _presence_key(user_id: int) -> str:
    return f"{_KEY_PREFIX}{user_id}"


def _server_key(user_id: int, connection_id: str) -> str:
    return f"{_SERVER_KEY_PREFIX}{user_id}:{connection_id}"


def _last_seen_key(user_id: int) -> str:
    return f"{_LAST_SEEN_KEY_PREFIX}{user_id}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _touch_last_seen(user_id: int) -> str:
    """Stamp the user's last_seen at the current instant and return it."""
    last_seen_at = _now_iso()
    await redis_client.set(_last_seen_key(user_id), last_seen_at)
    return last_seen_at


async def mark_online(user_id: int, connection_id: str, server_id: str) -> None:
    """
    Called on WebSocket connect. A user can have multiple simultaneous
    connections (multi-device), so this tracks a *set* of connection_ids
    rather than a single boolean - the user only goes offline once every
    connection in the set is gone. A presence_update is only published when
    this connection is the *first* one (0 -> 1 devices) - a second device
    connecting doesn't change the user's externally-visible status.
    """
    key = _presence_key(user_id)
    await redis_client.sadd(key, connection_id)
    await redis_client.expire(key, _PRESENCE_TTL_SECONDS)
    await redis_client.set(_server_key(user_id, connection_id), server_id, ex=_PRESENCE_TTL_SECONDS)

    # A live device means the user is "seen" right now; keep the stamp moving
    # forward so it is already current when the last device eventually drops.
    await _touch_last_seen(user_id)

    if await redis_client.scard(key) == 1:
        await realtime_service.publish_presence_event(
            user_id, {"type": "presence_update", "user_id": str(user_id), "status": "online"}
        )


async def heartbeat(user_id: int, connection_id: str) -> None:
    """Called periodically (e.g. every 30s) by the WebSocket handler to keep the TTL alive."""
    await redis_client.expire(_presence_key(user_id), _PRESENCE_TTL_SECONDS)
    await redis_client.expire(_server_key(user_id, connection_id), _PRESENCE_TTL_SECONDS)
    await _touch_last_seen(user_id)


async def mark_offline(user_id: int, connection_id: str) -> None:
    """
    Called on WebSocket disconnect (clean close, or the handler's own error
    path). last_seen is stamped on *every* device dropping (the last device
    to have been connected sets it); the offline `presence_update` is only
    published once every device is gone (0 remaining connections).
    """
    await redis_client.srem(_presence_key(user_id), connection_id)
    await redis_client.delete(_server_key(user_id, connection_id))

    last_seen_at = await _touch_last_seen(user_id)

    if await redis_client.scard(_presence_key(user_id)) == 0:
        await realtime_service.publish_presence_event(
            user_id, {"type": "presence_update", "user_id": str(user_id), "status": "offline", "last_seen_at": last_seen_at}
        )


async def get_status(user_id: int) -> dict:
    """
    The "pull" half of subscribe-on-demand: a subscriber's first snapshot on
    subscribe_presence, before any presence_update event has had a chance to
    arrive.
    """
    online = await is_online(user_id)
    last_seen_at = await redis_client.get(_last_seen_key(user_id))
    return {"status": "online" if online else "offline", "last_seen_at": last_seen_at}


async def is_online(user_id: int) -> bool:
    return await redis_client.scard(_presence_key(user_id)) > 0


async def get_online_participants(user_ids: list[int]) -> set[int]:
    """
    Bulk check for `message_service`: given every participant of a chat, which
    ones actually have a live connection right now (and so should get a
    real-time fanout instead of a push notification).
    """
    if not user_ids:
        return set()

    pipe = redis_client.pipeline()
    for user_id in user_ids:
        pipe.scard(_presence_key(user_id))
    counts = await pipe.execute()

    return {user_id for user_id, count in zip(user_ids, counts) if count > 0}


async def get_connections(user_id: int) -> set[str]:
    """Every live connection_id for a user - e.g. to fan out to all of their devices."""
    return await redis_client.smembers(_presence_key(user_id))
