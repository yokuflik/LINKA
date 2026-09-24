import redis.asyncio as redis

from config import REDIS_URL, REDIS_MAX_CONNECTIONS

# socket_timeout must exceed the longest blocking-read `block_ms` any stream
# worker passes to XREADGROUP (currently AGENT_INVOKE_STREAM_BLOCK_MS, 5000ms)
# - redis-py's async client derives its own socket-level read timeout from
# `block_ms` with no slack, so leaving socket_timeout at its 5s default (equal
# to that same 5000ms) meant ordinary scheduling jitter made the client's own
# timeout race Redis's, raising a spurious `TimeoutError` on a perfectly idle,
# healthy Redis - the recurring noisy "drain iteration failed" tracebacks seen
# throughout local dev turned out not to be cosmetic: one such timeout landing
# exactly on XACK (fixed separately in base_worker.py) caused a fully-answered
# agent turn to be redelivered and reprocessed, producing a duplicate reply.
_SOCKET_TIMEOUT_SECONDS = 15

# One shared connection pool for the whole process - every service module
# below imports this instead of opening its own connection.
redis_client: redis.Redis = redis.from_url(
    REDIS_URL,
    decode_responses=True,
    max_connections=REDIS_MAX_CONNECTIONS,
    socket_timeout=_SOCKET_TIMEOUT_SECONDS,
    retry_on_timeout=True,
)


async def close_redis() -> None:
    """Call once on app shutdown, alongside database.connection.dispose_engine()."""
    await redis_client.aclose()
