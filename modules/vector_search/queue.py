"""Redis-list flush-on-demand queue (ADR 0042). Plain LIST, not a Stream: this
queue has no consumer-group / at-least-once requirement - a dropped batch on a
Gemini failure is an accepted gap (see the ADR), so the simplest primitive
that supports "push" + "pop up to N" + "length" is enough.

Each entry is a JSON string `{"id": <message_id str>, "content": <str>}` -
message ids are Snowflake, so kept as strings the same way they cross the wire
elsewhere in the app (api/schemas.py's IdStr rule).
"""

import json

from config import settings
from infra.redis.client import redis_client


async def enqueue(message_id: int, content: str) -> int:
    """Push one message onto the queue. Returns the queue length after the push."""
    payload = json.dumps({"id": str(message_id), "content": content})
    return await redis_client.rpush(settings.VECTOR_QUEUE_KEY, payload)


async def queue_length() -> int:
    return await redis_client.llen(settings.VECTOR_QUEUE_KEY)


async def pop_batch(max_n: int) -> list[dict]:
    """Atomically pop up to `max_n` entries (oldest first) via one LPOP count call."""
    raw = await redis_client.lpop(settings.VECTOR_QUEUE_KEY, max_n)
    if not raw:
        return []
    return [json.loads(item) for item in raw]
