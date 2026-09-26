"""Config-mode tool handlers (ADR 0047 decision 6, split from tools.py).

Entirely disjoint set from the execution tools in execution.py - only
reachable when is_config_mode(agent, chat_id) is True (the hard gate,
dispatch.py). All writes here go through the same Agent.triggers/
system_prompt/active_skill mutation + cache-invalidation path as
PATCH /agents/me and update_own_triggers - one write path, multiple entry
points.
"""
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from infra.ratelimit.service import peek_fixed_window, peek_sliding_window
from modules.agents.cache import sync_agent_cache
from modules.agents.crud import (
    ScheduleQuotaExceededError,
    count_knowledge_chunks,
    count_knowledge_documents,
    is_chat_actively_paused,
    resume_agent_chat,
    update_agent_config,
    update_agent_triggers,
)
from modules.agents.models import Agent
from modules.agents.personas import STORABLE_SKILLS
from modules.agents.schedule import sync_schedule_zset
from modules.agents.tools.common import ToolDeniedError
from modules.chats import service as chat_service
from modules.users.crud import get_user_by_phone, get_user_by_username


async def _tool_set_agent_persona(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    skill = str(arguments["skill"])
    if skill not in STORABLE_SKILLS:
        raise ToolDeniedError(f"skill must be one of {sorted(STORABLE_SKILLS)}")
    updated = await update_agent_config(session, agent, {"active_skill": skill})
    await sync_agent_cache(updated)
    return {"active_skill": updated.active_skill}


async def _tool_update_agent_rules(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    rules = str(arguments["rules"])
    updated = await update_agent_config(session, agent, {"system_prompt": rules})
    return {"system_prompt": updated.system_prompt}


async def _tool_set_trigger(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    """Writes into Agent.triggers, same shape as the update_own_triggers
    execution tool - kept separate since this one is the config-mode entry
    point (agent_builder persona only, never reachable from an execution-mode
    chat)."""
    patch = arguments.get("triggers")
    if not isinstance(patch, dict):
        raise ToolDeniedError("triggers argument must be an object")
    try:
        updated = await update_agent_triggers(session, agent.id, patch)
    except ScheduleQuotaExceededError as exc:
        raise ToolDeniedError(str(exc))
    await sync_agent_cache(updated)
    if "on_schedule" in patch:
        await sync_schedule_zset(updated)
    return {"triggers": updated.triggers}


async def _tool_resolve_user(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    """Looks up a real person by phone_number or username - the ONLY two
    identifiers this ever accepts (never a display_name/nickname, which is
    free-form, unverified, and not unique - ADR 0024). Config-mode only:
    this is how the Builder/Help/Supervisor turn what the owner said ("wake
    up when 0501234567 messages", "send it to @dana") into a real,
    verified chat_id, instead of trusting the owner's spoken identifier
    outright. Returns {"found": false} rather than raising when no match -
    a routine "not found" outcome the model must relay to the owner, not an
    error path. Every config-mode prompt is instructed to call this before
    calling set_trigger/schedule_one_off_task with a chat_id derived from
    what the owner said, and before confirming success back to the owner."""
    phone_number = arguments.get("phone_number")
    username = arguments.get("username")
    if bool(phone_number) == bool(username):
        raise ToolDeniedError("provide exactly one of phone_number or username")

    if phone_number:
        user = await get_user_by_phone(session, str(phone_number))
    else:
        user = await get_user_by_username(session, str(username))

    if user is None:
        return {"found": False}

    chat = await chat_service.get_or_create_private_chat(session, agent.owner_user_id, user.id)
    return {
        "found": True,
        "chat_id": str(chat.id),
        "name": user.display_name or user.username or user.phone_number,
        "phone_number": user.phone_number,
        "username": user.username,
    }


async def _tool_resume_paused_chat(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    """ADR 0055: explicit, conversational counterpart to the human-only
    POST /agents/me/resume-chat/{chat_id} endpoint - resolves phone_number/
    username via the same resolve_user contract, then un-pauses that one
    chat specifically. Additive to ADR 0054's implicit most-recent-pause
    resume, which still fires unconditionally elsewhere - this is for when
    that heuristic isn't enough (multiple concurrent pauses)."""
    phone_number = arguments.get("phone_number")
    username = arguments.get("username")
    if bool(phone_number) == bool(username):
        raise ToolDeniedError("provide exactly one of phone_number or username")

    if phone_number:
        user = await get_user_by_phone(session, str(phone_number))
    else:
        user = await get_user_by_username(session, str(username))

    if user is None:
        return {"found": False}

    chat = await chat_service.get_or_create_private_chat(session, agent.owner_user_id, user.id)

    if not is_chat_actively_paused(agent, chat.id):
        return {"found": True, "chat_id": str(chat.id), "was_paused": False}

    updated = await resume_agent_chat(session, agent, chat.id)
    await sync_agent_cache(updated)
    return {"found": True, "chat_id": str(chat.id), "was_paused": True}


async def _tool_get_agent_status(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    """Reports the full runtime picture the agent_builder persona needs to
    answer "what are you doing right now" in natural language - not just
    restrictions/triggers, but active_skill and paused_chat_ids too (ADR
    0047 decision 5). Only actively-paused (non-expired) entries are
    reported (ADR 0054) - a lapsed pause is functionally resumed already."""
    from modules.agents.crud import _active_pauses

    return {
        "is_enabled": agent.is_enabled,
        "active_skill": agent.active_skill,
        "restrictions": agent.restrictions,
        "triggers": agent.triggers,
        "paused_chat_ids": [entry["chat_id"] for entry in _active_pauses(agent)],
    }


_API_USAGE_ESTIMATES = {
    "send_message": "~1 Gemini call, well under a cent at current pricing.",
    "reply_message": "~1 Gemini call, well under a cent at current pricing.",
    "schedule_daily_summary": "~1 Gemini call per firing, once per day - negligible monthly cost.",
    "knowledge_lookup": "~1-3 Gemini calls per question (index browse + a couple of chunk fetches).",
}


async def _tool_estimate_api_usage(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    """Static/approximate token-cost estimate, informational only - no DB
    write, no tie-in to the hard rate limits (those are enforced elsewhere
    regardless of what this tool reports)."""
    action = str(arguments.get("action") or "")
    estimate = _API_USAGE_ESTIMATES.get(
        action, "Cost depends on the specific action; typically well under a cent per turn."
    )
    return {"action": action, "estimate": estimate}


async def _tool_get_capacity_status(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    """Read-only introspection (ADR 0057), Builder-state only: every rate
    limit relevant to this agent, its configured max alongside current
    usage read live from Redis/Postgres, plus a rough capacity_estimate
    doing the one calculation an owner actually wants ("roughly how many
    conversations/hour before it needs to catch up"). Never increments or
    enforces anything - peek_fixed_window is a bare Redis GET."""
    activation_used = await peek_fixed_window(agent.id, "agent_activation")
    gemini_used = await peek_fixed_window(agent.id, "agent_gemini_calls")
    active_seconds_used = await peek_fixed_window(agent.id, "agent_active_seconds")
    # ADR 0058: the agent impersonates its owner on every send, so this is
    # the OWNER's own WS send_message budget, not a separate agent bucket -
    # shared with whatever the owner's own client is doing right now.
    send_rate_used = await peek_sliding_window(
        agent.owner_user_id, "send_message", settings.WS_SEND_MESSAGE_RATE_WINDOW_SECONDS,
    )
    send_burst_used = await peek_sliding_window(
        agent.owner_user_id, "send_message_burst", settings.WS_SEND_MESSAGE_BURST_WINDOW_SECONDS,
    )
    doc_count = await count_knowledge_documents(session, agent.id)
    chunk_count = await count_knowledge_chunks(session, agent.id)
    schedule_used = len(agent.triggers.get("on_schedule", []))
    auto_chats_used = sum(
        1 for entry in agent.triggers.get("on_specific_chats", {}).values()
        if isinstance(entry, dict) and "_auto_added_at" in entry
    )

    activation_max = settings.AGENT_ACTIVATION_QUOTA_PER_HOUR
    activation_window = settings.AGENT_ACTIVATION_QUOTA_WINDOW_SECONDS
    gemini_max = settings.AGENT_GEMINI_CALLS_PER_MINUTE
    gemini_window = settings.AGENT_GEMINI_CALLS_WINDOW_SECONDS
    daily_seconds_max = settings.AGENT_DAILY_ACTIVE_SECONDS_BUDGET

    # Approximation only - a real turn's Gemini-call count and duration vary
    # with tool use (e.g. Agentic RAG round-trips). Never used for real
    # enforcement, purely to give the owner a ballpark during setup.
    hourly_cap_by_activation = activation_max
    hourly_cap_by_gemini = gemini_max * (activation_window / gemini_window)
    hourly_cap_by_time_budget = daily_seconds_max / settings.AGENT_ESTIMATED_SECONDS_PER_TURN
    conversations_per_hour_estimate = max(
        0,
        int(min(hourly_cap_by_activation, hourly_cap_by_gemini, hourly_cap_by_time_budget))
        - activation_used,
    )

    return {
        "activation_quota": {
            "used": activation_used, "max": activation_max, "window_seconds": activation_window,
        },
        "gemini_calls": {
            "used": gemini_used, "max": gemini_max, "window_seconds": gemini_window,
            "note": "not enforced (unlimited) when the owner has set their own Gemini API key" if agent.encrypted_gemini_api_key else None,
        },
        "daily_active_seconds": {"used": active_seconds_used, "max": daily_seconds_max},
        "unknown_sender_daily_quota": {
            "max": settings.AGENT_UNKNOWN_SENDER_QUOTA_PER_DAY,
            "window_seconds": settings.AGENT_UNKNOWN_SENDER_QUOTA_WINDOW_SECONDS,
            "note": "per individual sender, not agent-wide",
        },
        "max_messages_per_day": agent.restrictions.get("max_messages_per_day"),
        "send_message_quota": {
            "used": send_rate_used, "max": settings.WS_SEND_MESSAGE_RATE_MAX,
            "window_seconds": settings.WS_SEND_MESSAGE_RATE_WINDOW_SECONDS,
            "burst_used": send_burst_used, "burst_max": settings.WS_SEND_MESSAGE_BURST_MAX,
            "burst_window_seconds": settings.WS_SEND_MESSAGE_BURST_WINDOW_SECONDS,
            "note": "shared with the owner's own manual messages - the agent sends as the owner, not as a separate identity",
        },
        "knowledge_base": {
            "documents_used": doc_count, "documents_max": settings.AGENT_KNOWLEDGE_MAX_DOCUMENTS_PER_AGENT,
            "chunks_used": chunk_count, "chunks_max": settings.AGENT_KNOWLEDGE_MAX_CHUNKS_PER_AGENT,
        },
        "schedule_entries": {"used": schedule_used, "max": settings.AGENT_MAX_SCHEDULE_ENTRIES},
        "auto_registered_chats": {"used": auto_chats_used, "max": settings.AGENT_MAX_AUTO_CHATS},
        "capacity_estimate": {
            "approx_new_conversations_per_hour": conversations_per_hour_estimate,
            "disclaimer": "Rough estimate only - actual capacity varies with how much work each conversation needs.",
        },
    }


async def _tool_schedule_one_off_task(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    """Reuses the on_schedule mechanism directly (ADR 0046 decision 3) -
    kind: "once", not a new scheduling path."""
    task = str(arguments["task"])
    execute_at = str(arguments["execute_at"])
    entry = {
        "id": uuid.uuid4().hex,
        "kind": "once",
        "at": execute_at,
        "instruction": task,
        "chat_id": arguments.get("chat_id"),
        "enabled": True,
    }
    entries = [*agent.triggers.get("on_schedule", []), entry]
    try:
        updated = await update_agent_triggers(session, agent.id, {"on_schedule": entries})
    except ScheduleQuotaExceededError as exc:
        raise ToolDeniedError(str(exc))
    await sync_agent_cache(updated)
    await sync_schedule_zset(updated)
    return {"schedule_id": entry["id"]}


CONFIG_TOOL_HANDLERS = {
    "set_agent_persona": _tool_set_agent_persona,
    "update_agent_rules": _tool_update_agent_rules,
    "set_trigger": _tool_set_trigger,
    "get_agent_status": _tool_get_agent_status,
    "estimate_api_usage": _tool_estimate_api_usage,
    "schedule_one_off_task": _tool_schedule_one_off_task,
    "resolve_user": _tool_resolve_user,
    "resume_paused_chat": _tool_resume_paused_chat,
    "get_capacity_status": _tool_get_capacity_status,
}
