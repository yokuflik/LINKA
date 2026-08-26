import redis.asyncio as redis

from config import REDIS_URL, REDIS_MAX_CONNECTIONS

# One shared connection pool for the whole process - every service module
# below imports this instead of opening its own connection.
redis_client: redis.Redis = redis.from_url(
    REDIS_URL, decode_responses=True, max_connections=REDIS_MAX_CONNECTIONS
)


async def close_redis() -> None:
    """Call once on app shutdown, alongside database.connection.dispose_engine()."""
    await redis_client.aclose()
