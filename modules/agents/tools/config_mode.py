"""Config-mode tool handlers (ADR 0047 decision 6, split from tools.py).

Entirely disjoint set from the execution tools in execution.py - only
reachable when is_config_mode(agent, chat_id) is True (the hard gate,
dispatch.py). All writes here go through the same Agent.triggers/
system_prompt/active_skill mutation + cache-invalidation path as
PATCH /agents/me and update_own_triggers - one write path, multiple entry
points.
"""
import difflib
import uuid
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from infra.ratelimit.service import peek_fixed_window, peek_sliding_window
from modules.agents.cache import sync_agent_cache
from modules.agents.crud import (
    KnowledgeQuotaExceededError,
    ScheduleQuotaExceededError,
    count_knowledge_chunks,
    count_knowledge_documents,
    is_chat_actively_paused,
    resume_agent_chat,
    update_agent_config,
    update_agent_triggers,
)
from modules.agents.ephemeral_tasks import (
    EphemeralTaskQuotaExceededError,
    spawn_ephemeral_task,
)
from modules.agents.knowledge_service import KnowledgeValidationError, commit_knowledge_text
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


# ADR 0073: how similar a chat title must be to the owner's typed name to be
# offered as a candidate at all - below this, silently drop it rather than
# padding the result with noise the model would have to filter itself.
_FIND_CHAT_MIN_SIMILARITY = 0.5
_FIND_CHAT_MAX_MATCHES = 5


async def _tool_find_chat_by_name(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    """ADR 0073: resolves a free-form name/nickname the owner mentions to one
    of THEIR OWN chats, by matching against chat titles as the owner would
    see them in their own chat list (group Chat.title, or the 1:1 peer's
    display_name||username||phone_number) - never a table-wide user search
    (ADR 0017/0024 ban fuzzy matching over the unbounded users table). The
    candidate set is always the owner's own bounded chat list, fetched fresh
    per call; matching happens in Python over that already-small,
    already-authorized list, not via a DB-level ILIKE/trigram query.

    Returns {"matches": [...]}, 0 to _FIND_CHAT_MAX_MATCHES entries,
    best-first. Deliberately never collapses close matches into one pick -
    the caller (prompt) is responsible for asking the owner to disambiguate
    when there's more than one candidate, and for falling back to
    resolve_user (exact phone/username) when there are none."""
    name = str(arguments.get("name") or "").strip()
    if not name:
        raise ToolDeniedError("name must not be empty")

    from modules.chats import service as chat_service_module

    titles = await chat_service_module.get_chat_titles_for_user(session, agent.owner_user_id)

    needle = name.casefold()
    scored = []
    for entry in titles:
        haystack = (entry["name"] or "").casefold()
        if not haystack:
            continue
        if needle == haystack:
            score = 1.0
        elif needle in haystack or haystack in needle:
            score = 0.9
        else:
            score = difflib.SequenceMatcher(None, needle, haystack).ratio()
        if score >= _FIND_CHAT_MIN_SIMILARITY:
            scored.append((score, entry))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    matches = [
        {"chat_id": str(entry["chat_id"]), "name": entry["name"], "is_group": entry["is_group"]}
        for _score, entry in scored[:_FIND_CHAT_MAX_MATCHES]
    ]
    return {"matches": matches}


async def _tool_resume_paused_chat(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    """ADR 0055: explicit, conversational counterpart to the human-only
    POST /agents/me/resume-chat/{chat_id} endpoint - resolves phone_number/
    username via the same resolve_user contract, then un-pauses that one
    chat specifically. This and the REST endpoint are the only two ways a
    paused chat resumes before its lazy expiry (ADR 0054's original design
    also auto-resumed on any owner reply in their own agent chat, but that
    was removed - see trigger_engine.py)."""
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
    ephemeral_tasks_used = len(agent.triggers.get("on_ephemeral_task", {}))
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
        "ephemeral_tasks": {"used": ephemeral_tasks_used, "max": settings.AGENT_MAX_EPHEMERAL_TASKS},
        "auto_registered_chats": {"used": auto_chats_used, "max": settings.AGENT_MAX_AUTO_CHATS},
        "capacity_estimate": {
            "approx_new_conversations_per_hour": conversations_per_hour_estimate,
            "disclaimer": "Rough estimate only - actual capacity varies with how much work each conversation needs.",
        },
    }


async def _tool_schedule_one_off_task(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    """Reuses the on_schedule mechanism directly (ADR 0046 decision 3) -
    kind: "once", not a new scheduling path. execute_at either an ISO-8601 UTC
    instant, or the sentinel "now" for immediate-ish execution (picked up on
    the next AGENT_SCHEDULE_POLL_INTERVAL_SECONDS tick, same as any entry
    whose `at` is already in the past - this just makes that an explicit,
    documented choice instead of an emergent side effect). scoped_system_prompt
    (ADR 0061) is optional free text used INSTEAD OF the agent's persona +
    system_prompt for this one firing only - never written to agent.system_prompt,
    so the agent's persistent config is untouched before or after."""
    task = str(arguments["task"])
    execute_at = str(arguments["execute_at"])
    if execute_at.strip().lower() == "now":
        execute_at = datetime.now(timezone.utc).isoformat()
    scoped_system_prompt = arguments.get("scoped_system_prompt")
    entry = {
        "id": uuid.uuid4().hex,
        "kind": "once",
        "at": execute_at,
        "instruction": task,
        "scoped_system_prompt": str(scoped_system_prompt) if scoped_system_prompt else None,
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


async def _tool_spawn_ephemeral_task(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    """ADR 0061: starts a short-lived, self-cleaning task that messages one or
    more chats, waits for their replies, then summarizes to the owner and
    deletes its own trigger entry - see modules/agents/ephemeral_tasks.py.
    Registers the task AND schedules the initial outreach to each chat_id
    (each fires immediately, in its own execution-mode turn, via the same
    mechanism as schedule_one_off_task) - config-mode itself has no
    send_message tool, so this tool cannot message anyone directly."""
    instruction = str(arguments["instruction"])
    chat_ids = arguments.get("chat_ids")
    if not isinstance(chat_ids, list) or not chat_ids:
        raise ToolDeniedError("chat_ids must be a non-empty list")
    timeout_minutes = arguments.get("timeout_minutes")

    try:
        result = await spawn_ephemeral_task(
            session, agent, instruction=instruction, chat_ids=chat_ids, timeout_minutes=timeout_minutes,
        )
    except EphemeralTaskQuotaExceededError as exc:
        raise ToolDeniedError(str(exc))
    return result


async def _tool_no_reply_needed(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    """No-op (ADR 0065): lets the model end a config-mode turn without
    posting anything to the owner's agent chat. invoke_worker.py detects this
    call by name and skips _post_config_reply for the turn entirely - the
    return value here is only to close out the function-calling round-trip."""
    return {"status": "ok"}


async def _tool_save_knowledge_from_text(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    """ADR 0078: agent-decided ingestion of reference/lookup text (inventory,
    price lists, policies, FAQs) into the knowledge base, so it stops being
    replayed verbatim on every future config-mode turn. The model decides
    within the normal tool-calling turn whether the owner's latest message is
    this kind of data - see the ADR for the accepted classification-risk
    tradeoff. Text-only, no S3 involved (commit_knowledge_text)."""
    source_label = str(arguments.get("source_label") or "").strip()
    content = str(arguments.get("content") or "").strip()
    if not source_label:
        raise ToolDeniedError("source_label must not be empty")
    if not content:
        raise ToolDeniedError("content must not be empty")

    try:
        document = await commit_knowledge_text(session, agent, source_label=source_label, raw_text=content)
    except KnowledgeQuotaExceededError as exc:
        raise ToolDeniedError(str(exc))
    except KnowledgeValidationError as exc:
        raise ToolDeniedError(str(exc))
    return {"status": "saved", "document_id": str(document.id), "filename": document.filename}


CONFIG_TOOL_HANDLERS = {
    "set_agent_persona": _tool_set_agent_persona,
    "update_agent_rules": _tool_update_agent_rules,
    "set_trigger": _tool_set_trigger,
    "get_agent_status": _tool_get_agent_status,
    "estimate_api_usage": _tool_estimate_api_usage,
    "schedule_one_off_task": _tool_schedule_one_off_task,
    "resolve_user": _tool_resolve_user,
    "find_chat_by_name": _tool_find_chat_by_name,
    "resume_paused_chat": _tool_resume_paused_chat,
    "get_capacity_status": _tool_get_capacity_status,
    "spawn_ephemeral_task": _tool_spawn_ephemeral_task,
    "no_reply_needed": _tool_no_reply_needed,
    "save_knowledge_from_text": _tool_save_knowledge_from_text,
}
