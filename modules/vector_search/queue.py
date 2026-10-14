"""Redis-list flush-on-demand queue (ADR 0042). Plain LIST, not a Stream: this
queue has no consumer-group / at-least-once requirement - failed items are
simply pushed back (ADR 0107), so the simplest primitive that supports "push"
+ "pop up to N" + "length" is enough.

Each entry is a JSON string `{"id": <message_id str>, "content": <str>,
"attempts": <int>}` (missing `attempts` = 0, ADR 0107) -
message ids are Snowflake, so kept as strings the same way they cross the wire
elsewhere in the app (api/schemas.py's IdStr rule).
"""

import json

from config import settings
from infra.redis.client import redis_client


async def enqueue(message_id: int, content: str, attempts: int = 0) -> int:
    """Push one message onto the queue. Returns the queue length after the push."""
    payload = json.dumps({"id": str(message_id), "content": content, "attempts": attempts})
    return await redis_client.rpush(settings.VECTOR_QUEUE_KEY, payload)


async def requeue(items: list[dict]) -> None:
    """Push already-popped items back (they carry their updated `attempts`)."""
    if not items:
        return
    await redis_client.rpush(
        settings.VECTOR_QUEUE_KEY,
        *[json.dumps({"id": i["id"], "content": i["content"], "attempts": i.get("attempts", 0)}) for i in items],
    )


async def set_backoff(seconds: int) -> None:
    """Pause flushing globally (Gemini 429). One key, no per-item timestamps."""
    await redis_client.set(settings.VECTOR_EMBED_BACKOFF_KEY, "1", ex=seconds)


async def backoff_active() -> bool:
    return bool(await redis_client.exists(settings.VECTOR_EMBED_BACKOFF_KEY))


async def queue_length() -> int:
    return await redis_client.llen(settings.VECTOR_QUEUE_KEY)


async def pop_batch(max_n: int) -> list[dict]:
    """Atomically pop up to `max_n` entries (oldest first) via one LPOP count call."""
    raw = await redis_client.lpop(settings.VECTOR_QUEUE_KEY, max_n)
    if not raw:
        return []
    return [json.loads(item) for item in raw]
