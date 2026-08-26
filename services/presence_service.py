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


def _presence_key(user_id: int) -> str:
    return f"{_KEY_PREFIX}{user_id}"


def _server_key(user_id: int, connection_id: str) -> str:
    return f"{_SERVER_KEY_PREFIX}{user_id}:{connection_id}"


async def mark_online(user_id: int, connection_id: str, server_id: str) -> None:
    """
    Called on WebSocket connect. A user can have multiple simultaneous
    connections (multi-device), so this tracks a *set* of connection_ids
    rather than a single boolean - the user only goes offline once every
    connection in the set is gone.
    """
    key = _presence_key(user_id)
    await redis_client.sadd(key, connection_id)
    await redis_client.expire(key, _PRESENCE_TTL_SECONDS)
    await redis_client.set(_server_key(user_id, connection_id), server_id, ex=_PRESENCE_TTL_SECONDS)


async def heartbeat(user_id: int, connection_id: str) -> None:
    """Called periodically (e.g. every 30s) by the WebSocket handler to keep the TTL alive."""
    await redis_client.expire(_presence_key(user_id), _PRESENCE_TTL_SECONDS)
    await redis_client.expire(_server_key(user_id, connection_id), _PRESENCE_TTL_SECONDS)


async def mark_offline(user_id: int, connection_id: str) -> None:
    """Called on WebSocket disconnect (clean close, or the handler's own error path)."""
    await redis_client.srem(_presence_key(user_id), connection_id)
    await redis_client.delete(_server_key(user_id, connection_id))


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
