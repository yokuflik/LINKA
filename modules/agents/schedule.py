"""Time-schedule trigger (ADR 0046, decision 3): recurring/one-off entries
that run a full agent turn (not a canned message) at a given time.

The Redis ZSET (`agent_schedule_due`) is purely a "when to next check" index
- member `{agent_id}:{schedule_id}`, score = next-fire unix timestamp. The
schedule definition itself lives in `Agent.triggers.on_schedule` (Postgres,
source of truth); this module keeps the ZSET in lockstep with every write to
that JSONB list and does the next-occurrence math the poll loop
(`modules/agents/invoke_worker.py`) needs.
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from config import settings
from infra.redis.client import redis_client
from modules.agents.models import Agent

logger = logging.getLogger(__name__)


def _member(agent_id: int, schedule_id: str) -> str:
    return f"{agent_id}:{schedule_id}"


def next_daily_occurrence(time_str: str, after: Optional[datetime] = None) -> datetime:
    """Next UTC instant matching daily HH:MM, strictly after `after` (default
    now) - if today's slot already passed, rolls to tomorrow."""
    after = after or datetime.now(timezone.utc)
    hour, minute = (int(p) for p in time_str.split(":"))
    candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= after:
        candidate += timedelta(days=1)
    return candidate


async def sync_schedule_zset(agent: Agent) -> None:
    """Re-derives this agent's ZSET membership from Agent.triggers.on_schedule
    - called after any write that can change on_schedule (PATCH /agents/me,
    update_own_triggers, and internally after a fire). Removes stale members
    for entries that got deleted or disabled/fired-once, (re)adds/updates the
    rest. Best-effort, same reasoning as modules.agents.cache - a Redis
    failure here must never fail the write it's attached to."""
    try:
        entries = agent.triggers.get("on_schedule", []) or []
        live_members = set()
        async with redis_client.pipeline(transaction=True) as pipe:
            for entry in entries:
                schedule_id = entry.get("id")
                if not schedule_id or not entry.get("enabled", True):
                    continue
                score = _next_fire_score(entry)
                if score is None:
                    continue
                member = _member(agent.id, schedule_id)
                live_members.add(member)
                pipe.zadd(settings.AGENT_SCHEDULE_DUE_ZSET_KEY, {member: score})
            await pipe.execute()

        # Drop any previously-scheduled member for this agent that isn't
        # live anymore (entry removed, disabled, or a "once" that already
        # fired) - cheap since one agent rarely has more than a handful.
        stale_prefix = f"{agent.id}:"
        existing = await redis_client.zrangebyscore(settings.AGENT_SCHEDULE_DUE_ZSET_KEY, "-inf", "+inf")
        stale = [m for m in existing if m.startswith(stale_prefix) and m not in live_members]
        if stale:
            await redis_client.zrem(settings.AGENT_SCHEDULE_DUE_ZSET_KEY, *stale)
    except Exception:
        logger.exception("agent schedule ZSET sync failed for agent %s", agent.id)


def _next_fire_score(entry: dict) -> Optional[float]:
    if entry.get("kind") == "recurring":
        time_str = entry.get("time")
        if not time_str:
            return None
        return next_daily_occurrence(time_str).timestamp()
    if entry.get("kind") == "once":
        at = entry.get("at")
        if not at:
            return None
        try:
            dt = datetime.fromisoformat(at.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt.timestamp()
    return None


async def due_members(now: Optional[float] = None) -> list[str]:
    """`{agent_id}:{schedule_id}` members due to fire at or before `now`
    (default: current time)."""
    return await redis_client.zrangebyscore(
        settings.AGENT_SCHEDULE_DUE_ZSET_KEY, "-inf", now if now is not None else time.time()
    )


async def remove_due_member(agent_id: int, schedule_id: str) -> None:
    await redis_client.zrem(settings.AGENT_SCHEDULE_DUE_ZSET_KEY, _member(agent_id, schedule_id))


async def reschedule_recurring(agent_id: int, schedule_id: str, time_str: str) -> None:
    """After a recurring entry fires: re-ZADD it for tomorrow's occurrence."""
    next_at = next_daily_occurrence(time_str)
    await redis_client.zadd(
        settings.AGENT_SCHEDULE_DUE_ZSET_KEY, {_member(agent_id, schedule_id): next_at.timestamp()}
    )
