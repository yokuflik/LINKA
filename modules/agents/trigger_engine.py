"""Trigger Rule Engine (ADR 0045, step 2; pre-filter cache ADR 0046 decision 1).

Evaluated synchronously but cheaply right after a message is persisted
(modules/messaging/send.py, ``process_outgoing``), in parallel with the
existing fan-out enqueue - never blocking or failing the send path. For each
other participant in the chat, checks the Redis pre-filter cache
(``agent:enabled_owners`` SET + ``agent:trigger_cfg:{owner_user_id}``
STRING) instead of querying Postgres directly - the overwhelming majority of
chats involve zero agents, so this keeps the common case at O(1) Redis
lookups. Postgres is only touched: (a) as a per-owner fallback on a cache
miss (cold Redis, self-healing - see ``_load_trigger_cfg``), and (b) once,
to load the full ``Agent`` row right before enqueueing a matched trigger
onto ``agent_invoke_stream``.
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from infra.db.connection import session_scope
from infra.ratelimit.service import check_and_increment
from infra.redis.client import redis_client
from modules.agents.cache import (
    get_cached_trigger_cfg,
    is_owner_cached_enabled,
    sync_agent_cache,
)
from modules.agents.crud import (
    auto_register_unknown_sender_chat,
    get_agent_by_id,
    get_agent_by_owner_chat,
    get_enabled_agents_for_owners,
    resume_most_recent_pause,
)
from modules.agents.invoke_queue import enqueue_invocation
from modules.chats.crud.crud_chat import get_chat_by_id
from modules.chats.crud.crud_participant import get_chat_participants
from modules.messaging.common import AGENT_REPLY_MESSAGE_TYPE, SYSTEM_MESSAGE_TYPE
from modules.messaging.crud import has_prior_messages
from modules.messaging.models import Message

logger = logging.getLogger(__name__)

_ACTIVATION_QUOTA_NOTICE = (
    "Your agent hit its hourly activation limit and won't respond to new "
    "messages until it resets. It'll pick back up automatically."
)


async def _notify_activation_quota_exceeded(session: AsyncSession, owner_agent_chat_id: int) -> None:
    """Posts one system-message notice per activation-quota window into the
    owner's own agent chat, so a silently-dropped trigger doesn't look like
    the agent went dark for no reason. Gated by a SET NX cooldown key (own
    TTL, independent of the ratelimit counter) so a burst of dropped triggers
    within the same window produces exactly one notice, not one per message."""
    cooldown_key = f"agent_quota_notice_sent:{owner_agent_chat_id}"
    try:
        acquired = await redis_client.set(
            cooldown_key, "1", nx=True, ex=settings.AGENT_ACTIVATION_QUOTA_WINDOW_SECONDS
        )
        if not acquired:
            return
        # Imported here, not at module level, to avoid a circular import:
        # modules.messaging.send imports evaluate_triggers from this module.
        from modules.messaging.send import send_system_message

        await send_system_message(session, owner_agent_chat_id, _ACTIVATION_QUOTA_NOTICE)
    except Exception:
        logger.exception("failed to notify owner of agent activation quota for chat %s", owner_agent_chat_id)


def _within_time_window(window: dict, now: datetime) -> bool:
    if not window.get("enabled"):
        return True
    try:
        start = datetime.strptime(window["start"], "%H:%M").time()
        end = datetime.strptime(window["end"], "%H:%M").time()
    except (KeyError, ValueError):
        # Malformed config fails open rather than silently gagging the agent.
        return True
    current = now.time()
    if start <= end:
        return start <= current <= end
    # Window wraps past midnight (e.g. 22:00-06:00).
    return current >= start or current <= end


def _matches_trigger_config(triggers: dict, message: Message) -> bool:
    """Matches on_specific_chats/on_time_window only - blocked_read_chat_ids
    lives in Agent.restrictions, which the pre-filter cache does not carry
    (ADR 0046 decision 1 caches only id + triggers), so that check happens
    separately once the full Agent row is loaded, right before enqueue."""
    chat_rule = triggers.get("on_specific_chats", {}).get(str(message.chat_id))
    if chat_rule is None:
        return False

    keywords = chat_rule.get("keywords") or []
    if keywords and message.content:
        content_lower = message.content.lower()
        if not any(kw.lower() in content_lower for kw in keywords):
            return False
    elif keywords and not message.content:
        # No text to match a keyword against (e.g. media-only message).
        return False

    return _within_time_window(triggers.get("on_time_window", {}), datetime.now(timezone.utc))


async def _matches_unknown_sender(
    session: AsyncSession, triggers: dict, message: Message
) -> bool:
    """ADR 0046 decision 2: fires when `message` is the first-ever message in
    a private (non-group) chat - independent of on_specific_chats/
    on_time_window. Checked separately from _matches_trigger_config because
    it needs a DB round trip (chat.is_group + prior-message existence),
    unlike the pure cache-only checks above."""
    if not triggers.get("on_unknown_sender", {}).get("enabled"):
        return False
    chat = await get_chat_by_id(session, message.chat_id)
    if chat is None or chat.is_group:
        return False
    return not await has_prior_messages(session, message.chat_id, message.id)


async def _matches_any_message(
    session: AsyncSession, triggers: dict, message: Message
) -> bool:
    """ADR 0052: fires on every message in every private (non-group) chat -
    a broader, stateless catch-all, unlike on_unknown_sender (first-message-
    only, mutates on_specific_chats on match). Still gated by on_time_window
    (checked by the caller via _matches_trigger_config's fallback path) -
    here we only decide chat scope. Needs the same DB round trip as
    _matches_unknown_sender for chat.is_group."""
    if not triggers.get("on_any_message", {}).get("enabled"):
        return False
    chat = await get_chat_by_id(session, message.chat_id)
    if chat is None or chat.is_group:
        return False
    return _within_time_window(triggers.get("on_time_window", {}), datetime.now(timezone.utc))


async def _load_trigger_cfg(session: AsyncSession, owner_user_id: int) -> Optional[dict]:
    """Cache-aside read for one owner: SISMEMBER -> GET; on a miss, falls
    back to the DB query for that single owner and repopulates the cache
    (self-healing, no separate warm-up script per ADR 0046)."""
    if await is_owner_cached_enabled(owner_user_id):
        cfg = await get_cached_trigger_cfg(owner_user_id)
        if cfg is not None:
            return cfg
        # SET said enabled but the STRING was missing/malformed - fall
        # through to the DB fallback below to repopulate both keys.

    agents = await get_enabled_agents_for_owners(session, [owner_user_id])
    if not agents:
        return None
    agent = agents[0]
    await sync_agent_cache(agent)
    return {
        "id": str(agent.id),
        "triggers": agent.triggers,
        "active_skill": agent.active_skill,
        "paused_chat_ids": agent.paused_chat_ids,
    }


def _active_paused_chat_ids(paused_chat_ids: list) -> set[int]:
    """ADR 0054: paused_chat_ids entries are {"chat_id", "paused_at",
    "expires_at"} objects - this pulls out just the chat_ids that haven't
    lapsed yet (lazy expiry, checked here on read). Tolerates the pre-0054
    flat-string shape by treating it as already expired."""
    now = datetime.now(timezone.utc)
    active = set()
    for entry in paused_chat_ids:
        if not isinstance(entry, dict):
            continue
        expires_at = entry.get("expires_at")
        if not expires_at:
            continue
        try:
            if datetime.fromisoformat(expires_at) > now:
                active.add(int(entry["chat_id"]))
        except ValueError:
            continue
    return active


async def evaluate_triggers(message: Message) -> None:
    """Fire-and-forget: exceptions are logged, never raised, so a bug here
    can never take down message delivery. Skips system messages outright.
    Runs on its own DB session - the send path's session may already be
    committed/closed by the time this task is scheduled."""
    try:
        async with session_scope() as session:
            await _evaluate_triggers(session, message)
    except Exception:
        logger.exception("agent trigger evaluation failed for message %s", message.id)


async def _evaluate_triggers(session: AsyncSession, message: Message) -> None:
    # AGENT_REPLY_MESSAGE_TYPE messages carry sender_id=owner_user_id (the
    # agent has no user_id of its own, ADR 0045) - indistinguishable from a
    # real owner-authored message by sender_id alone. Without this check, the
    # owner-chat branch below (message.sender_id == owner_agent.owner_user_id)
    # treated the agent's own reply as a fresh wake, re-invoking itself on
    # every turn - a genuine self-triggering loop (found via a real case: one
    # user message produced two full Gemini turns, the second answering
    # nothing since it had no new input, which is what broke the chain rather
    # than a fix).
    if message.type in (SYSTEM_MESSAGE_TYPE, AGENT_REPLY_MESSAGE_TYPE) or message.sender_id is None:
        return

    # Owner -> their own agent's dedicated 1:1 chat (AGENT_DRAWER_UI_PLAN.md
    # Wave 2, Step 5a): that chat has only the owner as a participant, so the
    # loop below (which only ever considers *other* participants' agents)
    # would never wake it - a message here is always an explicit, deliberate
    # wake, so it bypasses on_specific_chats/on_time_window/keyword gating
    # entirely, but still goes through the same is_enabled + hourly-quota
    # checks as any other trigger match.
    owner_agent = await get_agent_by_owner_chat(session, message.chat_id)
    if owner_agent is not None and owner_agent.owner_user_id == message.sender_id:
        # ADR 0054: any message from the owner in their own agent chat is
        # presumed to be about whichever escalation is freshest, so it
        # resumes just that one paused chat (not every paused chat at once)
        # before the owner's own turn is evaluated below.
        resumed_chat_id = await resume_most_recent_pause(session, owner_agent)
        if resumed_chat_id is not None:
            await sync_agent_cache(owner_agent)

        if owner_agent.is_enabled:
            allowed = await check_and_increment(
                owner_agent.id,
                "agent_activation",
                settings.AGENT_ACTIVATION_QUOTA_PER_HOUR,
                settings.AGENT_ACTIVATION_QUOTA_WINDOW_SECONDS,
            )
            if allowed:
                await enqueue_invocation(
                    agent_id=owner_agent.id, chat_id=message.chat_id, message_id=message.id
                )
            else:
                await _notify_activation_quota_exceeded(session, owner_agent.owner_agent_chat_id)
        return

    participants = await get_chat_participants(session, message.chat_id)
    candidate_owner_ids = [p.user_id for p in participants if p.user_id != message.sender_id]
    if not candidate_owner_ids:
        return

    for owner_user_id in candidate_owner_ids:
        cfg = await _load_trigger_cfg(session, owner_user_id)
        if cfg is None:
            continue

        # ADR 0047 decision 5: a chat the agent paused itself (pause_and_
        # escalate) via the cache is skipped before even matching triggers -
        # cheap pre-filter, same idea as the blocked_read_chat_ids check
        # further down against the full row.
        if message.chat_id in _active_paused_chat_ids(cfg.get("paused_chat_ids", [])):
            continue

        matched_unknown_sender = await _matches_unknown_sender(session, cfg["triggers"], message)
        matched = (
            matched_unknown_sender
            or _matches_trigger_config(cfg["triggers"], message)
            or await _matches_any_message(session, cfg["triggers"], message)
        )
        if not matched:
            continue

        agent_id = int(cfg["id"])

        # Postgres touched here, once, right before enqueue: re-checks
        # is_enabled + restrictions.blocked_read_chat_ids + paused_chat_ids
        # (defense in depth for the enqueue-to-dequeue-style window between
        # the cache read above and this point - same pattern as the existing
        # is_enabled recheck).
        agent = await get_agent_by_id(session, agent_id)
        if agent is None or not agent.is_enabled:
            continue
        blocked_chat_ids = {int(cid) for cid in agent.restrictions.get("blocked_read_chat_ids", [])}
        if message.chat_id in blocked_chat_ids:
            continue
        if message.chat_id in _active_paused_chat_ids(agent.paused_chat_ids):
            continue

        allowed = await check_and_increment(
            agent.id,
            "agent_activation",
            settings.AGENT_ACTIVATION_QUOTA_PER_HOUR,
            settings.AGENT_ACTIVATION_QUOTA_WINDOW_SECONDS,
        )
        if not allowed:
            # Hourly quota exceeded - drop the trigger (message still
            # delivered normally; no backlog/queueing of missed triggers per
            # ADR 0045), but let the owner know via their own agent chat.
            await _notify_activation_quota_exceeded(session, agent.owner_agent_chat_id)
            continue

        if matched_unknown_sender:
            # ADR 0046 decision 2: dedicated per-sender daily cap, on top of
            # the hourly activation quota above - stops one unknown sender
            # from burning the whole hourly budget by itself while staying
            # bounded by it in aggregate.
            sender_allowed = await check_and_increment(
                f"{agent.id}:{message.sender_id}",
                "agent_unknown_sender",
                settings.AGENT_UNKNOWN_SENDER_QUOTA_PER_DAY,
                settings.AGENT_UNKNOWN_SENDER_QUOTA_WINDOW_SECONDS,
            )
            if not sender_allowed:
                continue

            # ADR 0051: first-contact reply just cleared every gate - keep
            # the agent responding to this same person going forward by
            # folding the chat into on_specific_chats (FIFO-capped at
            # AGENT_MAX_AUTO_CHATS).
            await auto_register_unknown_sender_chat(session, agent, message.chat_id)

        await enqueue_invocation(agent_id=agent.id, chat_id=message.chat_id, message_id=message.id)
