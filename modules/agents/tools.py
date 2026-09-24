"""Tool registry + execute_tool_call (ADR 0045, step 4).

Every tool dispatches to an existing service handler (modules.messaging /
modules.chats) using the owner's own user_id - an agent is a service account,
never a bypass of normal per-user permissions or rate limits. Enforcement of
Agent.restrictions happens here, server-side, never left to the system
prompt (see docs/adr/0045). Every attempt - allowed or denied - is written to
AgentToolCallLog for audit/debugging (denials included, per ADR 0045's
allowed/denial_reason columns).
"""
import logging
import uuid
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from infra.ids.client import next_id
from infra.ratelimit.service import check_and_increment
from modules.agents.builder_flow import (
    BuilderState,
    FINISH_BUILDING_AGENT_SCHEMA,
    TRANSFER_TO_BUILDER_SCHEMA,
    TRANSFER_TO_HELP_SCHEMA,
)
from modules.agents.cache import sync_agent_cache
from modules.agents.crud import (
    ScheduleQuotaExceededError,
    get_knowledge_chunk,
    list_knowledge_index,
    pause_agent_chat,
    update_agent_config,
    update_agent_triggers,
)
from modules.agents.models import Agent, AgentToolCallLog
from modules.agents.personas import STORABLE_SKILLS
from modules.agents.schedule import sync_schedule_zset
from modules.chats import service as chat_service
from modules.chats.crud.crud_participant import is_participant
from modules.messaging import service as message_service
from modules.messaging.common import AGENT_REPLY_MESSAGE_TYPE
from modules.messaging.read_api import get_message_history
from modules.search import service as search_service
from modules.search.errors import SearchQueryTooShortError
from realtime.notification_service import send_push

logger = logging.getLogger(__name__)

_SECONDS_PER_DAY = 86400


async def _check_daily_send_quota(agent: Agent) -> None:
    """Cumulative send cap independent of the Gemini API rate limit -
    guards against a technically rate-limit-compliant agent still blasting
    one chat with dozens of messages in a burst (ADR 0045). Null/absent
    means unlimited - no counter touched, so an agent that never sets this
    never pays for a Redis round trip it doesn't need."""
    limit = agent.restrictions.get("max_messages_per_day")
    if limit is None:
        return
    allowed = await check_and_increment(agent.id, "agent_messages_per_day", int(limit), _SECONDS_PER_DAY)
    if not allowed:
        raise ToolDeniedError("max_messages_per_day exceeded")


class ToolDeniedError(Exception):
    """Raised internally when a restriction blocks a tool call - carries the
    denial_reason string logged onto AgentToolCallLog and reported back to
    Gemini as the function response, so the model can adapt (e.g. stop
    trying to message a blocked chat)."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


async def _log_call(
    session: AsyncSession,
    agent_id: int,
    tool_name: str,
    arguments: dict,
    allowed: bool,
    denial_reason: Optional[str],
) -> None:
    session.add(
        AgentToolCallLog(
            id=await next_id(),
            agent_id=agent_id,
            tool_name=tool_name,
            arguments=arguments,
            allowed=allowed,
            denial_reason=denial_reason,
        )
    )


def _new_client_message_id() -> str:
    # The agent has no real client - a fresh uuid per send just satisfies the
    # idempotency-key shape process_outgoing expects.
    return f"agent-{uuid.uuid4().hex}"


async def _tool_send_message(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    chat_id = int(arguments["chat_id"])
    content = str(arguments["content"])

    if not agent.restrictions.get("can_send_messages", True):
        raise ToolDeniedError("can_send_messages is disabled")
    if chat_id in {int(cid) for cid in agent.restrictions.get("blocked_read_chat_ids", [])}:
        raise ToolDeniedError("chat_id is in blocked_read_chat_ids")

    if not await is_participant(session, chat_id, agent.owner_user_id):
        raise ToolDeniedError("owner is not a participant of chat_id")

    is_group = await _chat_is_group(session, chat_id)
    if is_group and not agent.restrictions.get("can_message_groups", True):
        raise ToolDeniedError("can_message_groups is disabled")
    if not is_group and not agent.restrictions.get("can_message_private", True):
        raise ToolDeniedError("can_message_private is disabled")

    await _check_daily_send_quota(agent)

    message = await message_service.process_outgoing(
        session,
        sender_id=agent.owner_user_id,
        chat_id=chat_id,
        client_message_id=_new_client_message_id(),
        content=content,
        type=AGENT_REPLY_MESSAGE_TYPE,
    )
    return {"message_id": str(message.id)}


async def _tool_reply_message(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    chat_id = int(arguments["chat_id"])
    reply_to_message_id = int(arguments["reply_to_message_id"])
    content = str(arguments["content"])

    if not agent.restrictions.get("can_send_messages", True):
        raise ToolDeniedError("can_send_messages is disabled")
    if chat_id in {int(cid) for cid in agent.restrictions.get("blocked_read_chat_ids", [])}:
        raise ToolDeniedError("chat_id is in blocked_read_chat_ids")

    if not await is_participant(session, chat_id, agent.owner_user_id):
        raise ToolDeniedError("owner is not a participant of chat_id")

    is_group = await _chat_is_group(session, chat_id)
    if is_group and not agent.restrictions.get("can_message_groups", True):
        raise ToolDeniedError("can_message_groups is disabled")
    if not is_group and not agent.restrictions.get("can_message_private", True):
        raise ToolDeniedError("can_message_private is disabled")

    await _check_daily_send_quota(agent)

    message = await message_service.process_outgoing(
        session,
        sender_id=agent.owner_user_id,
        chat_id=chat_id,
        client_message_id=_new_client_message_id(),
        content=content,
        reply_to_message_id=reply_to_message_id,
    )
    return {"message_id": str(message.id)}


async def _tool_create_chat(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    target_user_id = int(arguments["target_user_id"])

    if not agent.restrictions.get("can_message_new_private_contacts", True):
        raise ToolDeniedError("can_message_new_private_contacts is disabled")
    if not agent.restrictions.get("can_message_private", True):
        raise ToolDeniedError("can_message_private is disabled")

    chat = await chat_service.get_or_create_private_chat(session, agent.owner_user_id, target_user_id)
    return {"chat_id": str(chat.id)}


async def _tool_leave_group(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    chat_id = int(arguments["chat_id"])
    new_owner_id = arguments.get("new_owner_id")

    if not agent.restrictions.get("can_leave_groups", True):
        raise ToolDeniedError("can_leave_groups is disabled")

    await chat_service.remove_member(
        session,
        actor_id=agent.owner_user_id,
        chat_id=chat_id,
        target_user_id=agent.owner_user_id,
        new_owner_id=int(new_owner_id) if new_owner_id is not None else None,
    )
    return {"left": True}


async def _tool_read_history(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    chat_id = int(arguments["chat_id"])

    if chat_id in {int(cid) for cid in agent.restrictions.get("blocked_read_chat_ids", [])}:
        raise ToolDeniedError("chat_id is in blocked_read_chat_ids")

    messages = await get_message_history(session, agent.owner_user_id, chat_id, limit=20)
    # Structured, not raw text: sender + timestamp let Gemini reason about
    # who said what and when, which matters in group chats where several
    # senders' lines would otherwise be indistinguishable.
    return {
        "messages": [
            {
                "sender_id": str(m.sender_id) if m.sender_id is not None else None,
                "timestamp": m.created_at.isoformat(),
                "content": m.content,
            }
            for m in reversed(list(messages))  # oldest first for a readable transcript
        ]
    }


async def _tool_update_own_triggers(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    # Hard-scoped to the caller's own agent_id and only the triggers column -
    # never restrictions, never another agent's row (ADR 0045).
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


async def _tool_search_messages(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    """ADR 0046 decision 3: thin wrapper over the existing ADR 0040 keyword
    search, scoped to the owner's own chats (search_in_chat/search_global
    already enforce membership via the same participants-JOIN pattern used
    everywhere else - never a bypass). Powers schedule-fired turns like
    "check who's waiting for a reply"."""
    query = str(arguments["query"])
    chat_id = arguments.get("chat_id")

    if chat_id is not None:
        chat_id = int(chat_id)
        if chat_id in {int(cid) for cid in agent.restrictions.get("blocked_read_chat_ids", [])}:
            raise ToolDeniedError("chat_id is in blocked_read_chat_ids")

    try:
        if chat_id is not None:
            result = await search_service.search_in_chat(
                session, user_id=agent.owner_user_id, chat_id=chat_id, raw_query=query, cursor=None, limit=10,
            )
        else:
            result = await search_service.search_global(
                session, user_id=agent.owner_user_id, raw_query=query, cursor=None, limit=10,
            )
    except SearchQueryTooShortError as exc:
        raise ToolDeniedError(str(exc))

    return {
        "results": [
            {
                "message_id": str(r.id),
                "chat_id": str(r.chat_id),
                "sender_id": str(r.sender_id) if r.sender_id is not None else None,
                "snippet": r.snippet or r.content,
                "created_at": r.created_at.isoformat(),
            }
            for r in result.results
        ]
    }


async def _tool_get_knowledge_index(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    """ADR 0047 decision 5: browse-then-fetch Agentic RAG, supersedes
    search_knowledge for the document use case. Returns every chunk
    belonging to this agent as {document_id, filename, chunk_id, excerpt}
    (excerpt = first ~80 chars, free - no LLM summarization pass). Capped by
    AGENT_KNOWLEDGE_MAX_CHUNKS_PER_AGENT - a known gap for a large corpus,
    acceptable for v1 per the ADR."""
    rows = await list_knowledge_index(session, agent.id, settings.AGENT_KNOWLEDGE_MAX_CHUNKS_PER_AGENT)
    return {
        "chunks": [
            {
                "document_id": str(chunk.document_id),
                "filename": filename,
                "chunk_id": str(chunk.id),
                "excerpt": chunk.content[:80],
            }
            for chunk, filename in rows
        ]
    }


async def _tool_fetch_chunk(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    """ADR 0047 decision 5: returns the full content of one chunk, scoped by
    agent.id ownership (never another agent's documents)."""
    chunk_id = int(arguments["chunk_id"])
    chunk = await get_knowledge_chunk(session, agent.id, chunk_id)
    if chunk is None:
        raise ToolDeniedError("chunk_id not found for this agent")
    return {"document_id": str(chunk.document_id), "chunk_index": chunk.chunk_index, "content": chunk.content}


async def _tool_pause_and_escalate(session: AsyncSession, agent: Agent, arguments: dict, chat_id: Optional[int] = None) -> dict:
    """ADR 0047 decision 5: freezes the agent for the *triggering chat only*
    and wakes the human owner. chat_id comes from the turn context (the tool
    has no chat_id argument of its own - it always escalates the chat it was
    invoked from), never from a model-supplied argument, so it can't be used
    to pause an arbitrary chat by guessing an id."""
    if chat_id is None:
        raise ToolDeniedError("pause_and_escalate requires a triggering chat")

    updated = await pause_agent_chat(session, agent, chat_id)
    await sync_agent_cache(updated)

    reason = str(arguments.get("reason") or "The agent needs your input to continue.")
    try:
        await send_push(
            agent.owner_user_id,
            title="Your agent needs you",
            body=reason,
            data={"chat_id": str(chat_id)},
        )
    except Exception:
        logger.exception("agent %s: send_push failed for pause_and_escalate on chat %s", agent.id, chat_id)

    return {"paused_chat_id": str(chat_id)}


async def _chat_is_group(session: AsyncSession, chat_id: int) -> bool:
    from modules.chats.crud.crud_chat import get_chat_by_id

    chat = await get_chat_by_id(session, chat_id)
    return bool(chat is not None and chat.is_group)


# --- Config-mode tools (ADR 0047 decision 6) --------------------------------
#
# Entirely disjoint set from the execution tools above - only reachable when
# is_config_mode(agent, chat_id) is True (the hard gate, decision 4). All
# writes here go through the same Agent.triggers/system_prompt/active_skill
# mutation + cache-invalidation path as PATCH /agents/me and
# update_own_triggers - one write path, multiple entry points.

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


async def _tool_get_agent_status(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    """Reports the full runtime picture the agent_builder persona needs to
    answer "what are you doing right now" in natural language - not just
    restrictions/triggers, but active_skill and paused_chat_ids too (ADR
    0047 decision 5)."""
    return {
        "is_enabled": agent.is_enabled,
        "active_skill": agent.active_skill,
        "restrictions": agent.restrictions,
        "triggers": agent.triggers,
        "paused_chat_ids": [str(cid) for cid in agent.paused_chat_ids],
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


# --- Builder sub-state handoff tools (ADR 0049) ------------------------------
#
# Only reachable from within config mode's builder_agent/supervisor/help_agent
# states (see _BUILDER_STATE_HANDLERS below) - never from an execution-mode
# chat. All writes go through update_agent_config, the same single write path
# every other config tool uses.

async def _tool_transfer_to_builder(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    updated = await update_agent_config(session, agent, {"builder_state": BuilderState.BUILDER.value})
    return {"status": "transferred", "to": updated.builder_state}


async def _tool_transfer_to_help(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    updated = await update_agent_config(session, agent, {"builder_state": BuilderState.HELP.value})
    return {"status": "transferred", "to": updated.builder_state}


async def _tool_finish_building_agent(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    """Wrap-up signal, not a bulk config-apply - the 6 config tools already
    save incrementally during the interview (ADR 0049, confirmed with the
    user 2026-09-24). Auto-enables the agent, unlike every other config tool
    write, so sync_agent_cache is required here (is_enabled is part of the
    trigger pre-filter cache payload, unlike builder_state)."""
    updated = await update_agent_config(
        session, agent, {"builder_state": BuilderState.SUPERVISOR.value, "is_enabled": True}
    )
    await sync_agent_cache(updated)
    return {"status": "agent_activated"}


# --- Hard tool-mode gate (ADR 0047 decision 4, security-critical) ----------
#
# Which tool schemas are sent to Gemini is decided purely by the triggering
# chat_id, never by active_skill, system_prompt, or anything the model says
# about itself. chat_id == agent.owner_agent_chat_id -> config mode; any
# other chat, or a schedule-fired turn (chat_id is None or not the owner-
# agent chat) -> execution mode. This is a hard boundary: an end customer
# sending a prompt-injection payload into an execution-mode chat must be
# structurally unable to reach a config tool, regardless of what the model
# decides to believe.
#
# ADR 0047 decision 5: get_knowledge_index/fetch_chunk supersede
# search_knowledge for the document-RAG use case (browse-then-fetch is a
# cleaner mental model than a keyword query, and the chunks table has no
# natural "search terms" to key off of). search_knowledge is dropped from
# the registry - not implemented, per the ADR's "no migration cost, just
# don't build the superseded tool."
_EXECUTION_TOOL_HANDLERS = {
    "send_message": _tool_send_message,
    "reply_message": _tool_reply_message,
    "create_chat": _tool_create_chat,
    "leave_group": _tool_leave_group,
    "read_history": _tool_read_history,
    "update_own_triggers": _tool_update_own_triggers,
    "search_messages": _tool_search_messages,
    "get_knowledge_index": _tool_get_knowledge_index,
    "fetch_chunk": _tool_fetch_chunk,
    "pause_and_escalate": _tool_pause_and_escalate,
}

# ADR 0047 decision 6: entirely disjoint from the execution set - only
# reachable when is_config_mode(agent, chat_id) is True.
_CONFIG_TOOL_HANDLERS = {
    "set_agent_persona": _tool_set_agent_persona,
    "update_agent_rules": _tool_update_agent_rules,
    "set_trigger": _tool_set_trigger,
    "get_agent_status": _tool_get_agent_status,
    "estimate_api_usage": _tool_estimate_api_usage,
    "schedule_one_off_task": _tool_schedule_one_off_task,
}

# pause_and_escalate is the one execution tool whose handler needs the
# turn's triggering chat_id (to know which chat to pause) - every other
# handler has the uniform (session, agent, arguments) signature dispatched
# generically below, so this name is special-cased in execute_tool_call
# rather than changing that signature for every other tool.
_CHAT_SCOPED_TOOL_NAMES = frozenset({"pause_and_escalate"})


def is_config_mode(agent: Agent, chat_id: Optional[int]) -> bool:
    """The sole decision point for tool-mode selection. chat_id is the
    triggering chat: None (schedule-fired turn with no chat target) is never
    config mode - config mode requires an explicit, real owner_agent_chat_id
    match, not the absence of a chat."""
    return chat_id is not None and chat_id == agent.owner_agent_chat_id


# Gemini function-declaration schemas (raw REST shape - no SDK, ADR 0045).
TOOL_SCHEMAS = [
    {
        "name": "send_message",
        "description": "Send a new text message into an existing chat the owner already participates in.",
        "parameters": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "string", "description": "Target chat id"},
                "content": {"type": "string", "description": "Message text"},
            },
            "required": ["chat_id", "content"],
        },
    },
    {
        "name": "reply_message",
        "description": "Send a text message that replies to a specific earlier message in a chat.",
        "parameters": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "string"},
                "reply_to_message_id": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["chat_id", "reply_to_message_id", "content"],
        },
    },
    {
        "name": "create_chat",
        "description": "Open a brand-new 1:1 chat with a user the owner has not messaged before (or fetch the existing one).",
        "parameters": {
            "type": "object",
            "properties": {"target_user_id": {"type": "string"}},
            "required": ["target_user_id"],
        },
    },
    {
        "name": "leave_group",
        "description": "Leave a group chat on the owner's behalf. If the owner is the group's owner and other members remain, new_owner_id must name a successor.",
        "parameters": {
            "type": "object",
            "properties": {
                "chat_id": {"type": "string"},
                "new_owner_id": {"type": "string", "description": "Required only if the owner is this group's owner and other members remain"},
            },
            "required": ["chat_id"],
        },
    },
    {
        "name": "read_history",
        "description": "Read the last 20 messages of a chat, oldest first, each with sender_id/timestamp/content.",
        "parameters": {
            "type": "object",
            "properties": {"chat_id": {"type": "string"}},
            "required": ["chat_id"],
        },
    },
    {
        "name": "update_own_triggers",
        "description": "Modify this agent's own wake-up trigger configuration (time window / per-chat keywords). Cannot touch restrictions.",
        "parameters": {
            "type": "object",
            "properties": {
                "triggers": {
                    "type": "object",
                    "description": "Partial or full triggers object: {on_time_window: {enabled, start, end}, on_specific_chats: {chat_id: {keywords: [...]}}}",
                }
            },
            "required": ["triggers"],
        },
    },
    {
        "name": "search_messages",
        "description": "Keyword-search the owner's own messages (optionally scoped to one chat) - e.g. to check who has been waiting for a reply.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "chat_id": {"type": "string", "description": "Optional: restrict the search to this chat"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_knowledge_index",
        "description": "List every chunk of this agent's own uploaded knowledge-base documents as {document_id, filename, chunk_id, excerpt}. Browse this first, then call fetch_chunk on the chunk_id(s) that look relevant.",
    },
    {
        "name": "fetch_chunk",
        "description": "Fetch the full text of one knowledge-base chunk by chunk_id (from get_knowledge_index).",
        "parameters": {
            "type": "object",
            "properties": {"chunk_id": {"type": "string"}},
            "required": ["chunk_id"],
        },
    },
    {
        "name": "pause_and_escalate",
        "description": "Freeze yourself for this specific chat and notify the human owner that you need their input. Use this when you're stuck, unsure, or asked to do something outside your restrictions - you will not be woken again in this chat until the owner resumes it.",
        "parameters": {
            "type": "object",
            "properties": {"reason": {"type": "string", "description": "Short explanation for the owner of why you're escalating"}},
        },
    },
]

# Config-mode tool schemas (ADR 0047 decision 6) - reachable only when
# is_config_mode(agent, chat_id) is True (the hard gate, decision 4).
CONFIG_TOOL_SCHEMAS = [
    {
        "name": "set_agent_persona",
        "description": "Set which skill/persona the agent runs as in execution-mode chats (sales_agent, support_agent, summarizer, or one_off_executor).",
        "parameters": {
            "type": "object",
            "properties": {"skill": {"type": "string", "description": "One of: sales_agent, support_agent, summarizer, one_off_executor"}},
            "required": ["skill"],
        },
    },
    {
        "name": "update_agent_rules",
        "description": "Replace the agent's free-text soft rules (system prompt), layered on top of its persona's behavioral template.",
        "parameters": {
            "type": "object",
            "properties": {"rules": {"type": "string"}},
            "required": ["rules"],
        },
    },
    {
        "name": "set_trigger",
        "description": "Modify the agent's wake-up trigger configuration (time window / per-chat keywords / unknown-sender / schedule entries).",
        "parameters": {
            "type": "object",
            "properties": {
                "triggers": {
                    "type": "object",
                    "description": "Partial triggers object: {on_time_window: {...}, on_specific_chats: {...}, on_unknown_sender: {...}, on_schedule: [...]}",
                }
            },
            "required": ["triggers"],
        },
    },
    {
        "name": "get_agent_status",
        "description": "Report the agent's current configuration: enabled state, active skill, restrictions, triggers, and any chats currently paused awaiting the owner's input.",
    },
    {
        "name": "estimate_api_usage",
        "description": "Give the owner a rough, informational cost estimate for a described action (e.g. sending messages, a daily schedule, knowledge lookups). Does not affect any real rate limit.",
        "parameters": {
            "type": "object",
            "properties": {"action": {"type": "string"}},
            "required": ["action"],
        },
    },
    {
        "name": "schedule_one_off_task",
        "description": "Schedule a single one-time future task (reuses the same mechanism as a recurring schedule entry, kind=once).",
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Free-text instruction to execute at the scheduled time"},
                "execute_at": {"type": "string", "description": "ISO-8601 UTC instant"},
                "chat_id": {"type": "string", "description": "Optional: chat to join history from when the task fires"},
            },
            "required": ["task", "execute_at"],
        },
    },
]

# --- Builder sub-state dispatch tables (ADR 0049) ---------------------------
#
# Extends the config-mode gate above (decision 4 stays exactly as-is: chat_id
# decides execution vs. config). Within config mode, agent.builder_state picks
# one of three disjoint handler/schema sets - never a fallback to another
# state's tools, same one-decision-point discipline as is_config_mode itself.
_BUILDER_STATE_HANDLERS = {
    BuilderState.SUPERVISOR: {"transfer_to_builder": _tool_transfer_to_builder},
    BuilderState.BUILDER: {
        **_CONFIG_TOOL_HANDLERS,
        "transfer_to_help": _tool_transfer_to_help,
        "finish_building_agent": _tool_finish_building_agent,
    },
    BuilderState.HELP: {"transfer_to_builder": _tool_transfer_to_builder},
}

_BUILDER_STATE_TOOL_SCHEMAS = {
    BuilderState.SUPERVISOR: [TRANSFER_TO_BUILDER_SCHEMA],
    BuilderState.BUILDER: [*CONFIG_TOOL_SCHEMAS, TRANSFER_TO_HELP_SCHEMA, FINISH_BUILDING_AGENT_SCHEMA],
    BuilderState.HELP: [TRANSFER_TO_BUILDER_SCHEMA],
}


def get_tool_schemas_for_chat(agent: Agent, chat_id: Optional[int]) -> list:
    """Mode-aware schema selection (ADR 0047 decision 4) - the only inputs
    are agent and the triggering chat_id, never active_skill/system_prompt/
    anything model-controlled. Within config mode, ADR 0049 further narrows
    to the current builder_state's own disjoint schema set."""
    if not is_config_mode(agent, chat_id):
        return TOOL_SCHEMAS
    return _BUILDER_STATE_TOOL_SCHEMAS[BuilderState(agent.builder_state)]


async def execute_tool_call(
    session: AsyncSession, agent: Agent, tool_name: str, arguments: dict, chat_id: Optional[int] = None,
) -> dict[str, Any]:
    """Runs one tool call for `agent`, enforcing Agent.restrictions, and
    always logs the attempt (allowed or denied) to AgentToolCallLog. Returns
    the dict handed back to Gemini as the function response - on denial this
    is {"error": reason} so the model can see why and adjust, rather than
    the log being the only record.

    `chat_id` is the turn's triggering chat (None for a schedule-fired turn
    with no chat target) - ADR 0047 decision 4's defense-in-depth recheck:
    independently re-derives the mode-appropriate allowlist here and rejects
    a mismatched tool_name even if the wrong schema list were ever leaked to
    Gemini by a bug upstream (same belt-and-suspenders pattern as the
    existing Agent.is_enabled recheck at worker dequeue time). ADR 0049
    further narrows the config-mode allowlist to the current builder_state."""
    config_mode = is_config_mode(agent, chat_id)
    if config_mode:
        mode_label = f"config/{agent.builder_state}"
        handlers = _BUILDER_STATE_HANDLERS[BuilderState(agent.builder_state)]
    else:
        mode_label = "execution"
        handlers = _EXECUTION_TOOL_HANDLERS
    allowlist = frozenset(handlers)

    if tool_name not in allowlist:
        logger.warning(
            "agent %s tool %s denied: not in %s-mode allowlist",
            agent.id, tool_name, mode_label,
        )
        await _log_call(
            session, agent.id, tool_name, arguments, allowed=False,
            denial_reason=f"tool not available in {mode_label} mode",
        )
        return {"error": f"tool '{tool_name}' is not available in this context"}

    handler = handlers.get(tool_name)
    if handler is None:
        await _log_call(session, agent.id, tool_name, arguments, allowed=False, denial_reason="unknown tool")
        return {"error": f"unknown tool: {tool_name}"}

    try:
        if tool_name in _CHAT_SCOPED_TOOL_NAMES:
            result = await handler(session, agent, arguments, chat_id=chat_id)
        else:
            result = await handler(session, agent, arguments)
    except ToolDeniedError as exc:
        logger.info("agent %s tool %s denied: %s", agent.id, tool_name, exc.reason)
        await _log_call(session, agent.id, tool_name, arguments, allowed=False, denial_reason=exc.reason)
        return {"error": exc.reason}
    except Exception as exc:
        logger.exception("agent %s tool %s failed", agent.id, tool_name)
        await _log_call(session, agent.id, tool_name, arguments, allowed=False, denial_reason=f"error: {exc}")
        return {"error": str(exc)}

    await _log_call(session, agent.id, tool_name, arguments, allowed=True, denial_reason=None)
    return result
