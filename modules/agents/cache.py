"""Trigger pre-filter cache (ADR 0046, decision 1).

Redis cache-aside layer in front of `get_enabled_agents_for_owners` so the
common case - no participant in a chat owns an enabled agent - never touches
Postgres. Source of truth stays the `agents` table; these two keys are
strictly a read-through cache:

- `agent:enabled_owners` (SET): owner_user_ids with Agent.is_enabled=true.
- `agent:trigger_cfg:{owner_user_id}` (STRING, JSON): "cached runtime config"
  for that owner's agent - id, full Agent.triggers, active_skill, and
  paused_chat_ids (ADR 0047 decision 5 widens this from "cached triggers"
  to cover pause_and_escalate's dynamic state too - same key, wider payload,
  same invalidation discipline).

Both are rewritten by `sync_agent_cache` on every trigger-affecting write
(agent create, PATCH /agents/me, the update_own_triggers tool) and dropped by
`remove_agent_cache` when an agent is disabled. A cache miss during
evaluation is not an error - `trigger_engine` falls back to the DB query for
that one owner and repopulates via `sync_agent_cache`.
"""
import json
import logging
from typing import Optional

from config import settings
from infra.redis.client import redis_client
from modules.agents.models import Agent

logger = logging.getLogger(__name__)


def _trigger_cfg_key(owner_user_id: int) -> str:
    return f"{settings.AGENT_TRIGGER_CFG_KEY_PREFIX}{owner_user_id}"


async def sync_agent_cache(agent: Agent) -> None:
    """Called after any write that can change an agent's is_enabled or
    triggers (create, PATCH /agents/me, update_own_triggers). Best-effort -
    a Redis failure here must never fail the write it's attached to; the
    cache just stays stale until the next self-healing miss."""
    try:
        if agent.is_enabled:
            payload = json.dumps({
                "id": str(agent.id),
                "triggers": agent.triggers,
                "active_skill": agent.active_skill,
                "paused_chat_ids": agent.paused_chat_ids,
            })
            async with redis_client.pipeline(transaction=True) as pipe:
                pipe.sadd(settings.AGENT_ENABLED_OWNERS_SET_KEY, agent.owner_user_id)
                pipe.set(_trigger_cfg_key(agent.owner_user_id), payload)
                await pipe.execute()
        else:
            await remove_agent_cache(agent.owner_user_id)
    except Exception:
        logger.exception("agent cache sync failed for owner %s", agent.owner_user_id)


async def remove_agent_cache(owner_user_id: int) -> None:
    """Drops an owner from the pre-filter cache (agent disabled/deleted).
    Best-effort, same reasoning as sync_agent_cache."""
    try:
        async with redis_client.pipeline(transaction=True) as pipe:
            pipe.srem(settings.AGENT_ENABLED_OWNERS_SET_KEY, owner_user_id)
            pipe.delete(_trigger_cfg_key(owner_user_id))
            await pipe.execute()
    except Exception:
        logger.exception("agent cache removal failed for owner %s", owner_user_id)


async def is_owner_cached_enabled(owner_user_id: int) -> bool:
    return bool(await redis_client.sismember(settings.AGENT_ENABLED_OWNERS_SET_KEY, owner_user_id))


async def get_cached_trigger_cfg(owner_user_id: int) -> Optional[dict]:
    """Returns {"id": str, "triggers": dict, "active_skill": str,
    "paused_chat_ids": list} or None on a cache miss (key absent, or JSON
    somehow malformed - treated the same as a miss so the caller falls back
    to Postgres rather than crashing trigger evaluation)."""
    raw = await redis_client.get(_trigger_cfg_key(owner_user_id))
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("malformed agent trigger_cfg cache for owner %s", owner_user_id)
        return None
