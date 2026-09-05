"""
Cross-process cap on concurrent WebSocket connections per user
(COMMS_SECURITY_PLAN step 5 / ADR 0012).

`ZSET ws:conns:{user_id}` holds every live connection for a user across all
app processes: member = ``"{server_id}:{connection_id}"``, score = connect
epoch-ms. One Lua script runs on connect and, atomically:

  1. sweeps entries older than ``WS_CONN_MAX_AGE_SECONDS`` (a crashed process
     that never got to ``unregister`` would otherwise leak its slot forever),
  2. adds this connection,
  3. while the set is over ``WS_CONN_MAX_CONNECTIONS``, pops the *oldest*
     member and returns it as an eviction.

Because the whole sequence is one script, N sockets opened simultaneously all
converge to exactly the newest ``WS_CONN_MAX_CONNECTIONS`` - each surplus open
is evicted by whichever call first sees the count exceed the cap.

The caller (routers/websocket.py) publishes a ``force_disconnect`` to each
evicted member's instance inbox; that process closes the socket (silently -
business answer 4, 2026-09-06).
"""
import logging

from config import WS_CONN_MAX_AGE_SECONDS, WS_CONN_MAX_CONNECTIONS
from services.redis_client import redis_client

logger = logging.getLogger(__name__)

_KEY_PREFIX = "ws:conns:"

# KEYS[1] = ws:conns:{user_id}
# ARGV[1] = now_ms   ARGV[2] = max_age_ms   ARGV[3] = max_connections
# ARGV[4] = this member ("{server_id}:{connection_id}")
# Returns the list of evicted members (possibly empty).
_REGISTER_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local max_age = tonumber(ARGV[2])
local max_conns = tonumber(ARGV[3])
local member = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, '-inf', now - max_age)
redis.call('ZADD', key, now, member)

local evicted = {}
local count = redis.call('ZCARD', key)
while count > max_conns do
    local popped = redis.call('ZPOPMIN', key)
    -- ZPOPMIN returns {member, score}; guard against the just-added member
    -- being the one popped (only possible if max_conns < 1).
    if popped[1] == nil then break end
    table.insert(evicted, popped[1])
    count = count - 1
end

-- Keep the key from lingering once a user's last connection is gone.
-- (guard against a 0/negative max_age deleting the key we just wrote)
if max_age > 0 then
    redis.call('PEXPIRE', key, max_age)
end
return evicted
"""

_register_script = redis_client.register_script(_REGISTER_LUA)


def member(server_id: str, connection_id: str) -> str:
    return f"{server_id}:{connection_id}"


def split_member(m: str) -> tuple[str, str]:
    """Inverse of ``member`` - ``connection_id`` is a UUID (no colons), so a
    single rsplit is safe even if a server_id ever contained one."""
    server_id, _, connection_id = m.rpartition(":")
    return server_id, connection_id


async def register(user_id: int, server_id: str, connection_id: str) -> list[str]:
    """Record a new connection; return the members evicted to stay under the
    cap (each ``"{server_id}:{connection_id}"``). Best-effort: a Redis failure
    logs and returns ``[]`` rather than blocking the connection."""
    import time

    now_ms = int(time.time() * 1000)
    try:
        evicted = await _register_script(
            keys=[f"{_KEY_PREFIX}{user_id}"],
            args=[now_ms, WS_CONN_MAX_AGE_SECONDS * 1000, WS_CONN_MAX_CONNECTIONS, member(server_id, connection_id)],
        )
    except Exception:
        logger.exception("ws_connection_registry.register failed for user %s", user_id)
        return []
    return [m.decode() if isinstance(m, (bytes, bytearray)) else m for m in (evicted or [])]


async def unregister(user_id: int, server_id: str, connection_id: str) -> None:
    """Drop a connection's slot (normal or forced disconnect). Best-effort."""
    try:
        await redis_client.zrem(f"{_KEY_PREFIX}{user_id}", member(server_id, connection_id))
    except Exception:
        logger.exception("ws_connection_registry.unregister failed for user %s", user_id)
