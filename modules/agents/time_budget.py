"""Daily active-time budget (ADR 0045): 1 hour/day of actual processing
wall-clock (Gemini calls + tool execution) per agent, separate from the
hourly *activation* quota in trigger_engine.py. A Redis fixed-window counter
keyed per agent, counting elapsed seconds rather than call count, so it does
not fit infra.ratelimit.service.check_and_increment (always +1 per call).
"""
from config import settings
from infra.redis.client import redis_client

_KEY_PREFIX = "ratelimit:agent_active_seconds:"
_SECONDS_PER_DAY = 86400


def _key(agent_id: int) -> str:
    return f"{_KEY_PREFIX}{agent_id}"


async def has_budget_remaining(agent_id: int) -> bool:
    """True if the agent has not yet exhausted today's time budget. Checked
    before starting a turn; the turn itself is still allowed to finish once
    started (see record_active_seconds)."""
    used = await redis_client.get(_key(agent_id))
    return int(used or 0) < settings.AGENT_DAILY_ACTIVE_SECONDS_BUDGET


async def record_active_seconds(agent_id: int, seconds: float) -> int:
    """Add elapsed processing time to today's counter. Returns the new total.
    First increment in the window sets the TTL so the counter resets daily."""
    key = _key(agent_id)
    new_total = await redis_client.incrby(key, max(0, round(seconds)))
    if new_total == max(0, round(seconds)):
        await redis_client.expire(key, _SECONDS_PER_DAY)
    return new_total
