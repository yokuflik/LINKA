"""Execution-mode tool handlers (ADR 0056, split from tools.py).

Every tool dispatches to an existing service handler (modules.messaging /
modules.chats) using the owner's own user_id - an agent is a service account,
never a bypass of normal per-user permissions or rate limits. Enforcement of
Agent.restrictions happens here, server-side, never left to the system
prompt (see docs/adr/0045).
"""
import logging
import uuid
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from modules.agents.cache import sync_agent_cache
from modules.agents.crud import (
    ScheduleQuotaExceededError,
    get_knowledge_chunk,
    list_knowledge_index,
    pause_agent_chat,
    update_agent_triggers,
)
from modules.agents.models import Agent
from modules.agents.schedule import sync_schedule_zset
from modules.agents.tools.common import (
    ToolDeniedError,
    _check_daily_send_quota,
    _chat_is_group,
    _describe_escalation_counterpart,
    _resolve_sender_labels,
)
from modules.chats import service as chat_service
from modules.chats.crud.crud_participant import is_participant
from modules.messaging import service as message_service
from modules.messaging.common import AGENT_REPLY_MESSAGE_TYPE
from modules.messaging.read_api import get_message_history
from modules.search import service as search_service
from modules.search.errors import SearchQueryTooShortError
from realtime.notification_service import send_push

logger = logging.getLogger(__name__)


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
    labels = await _resolve_sender_labels(session, [m.sender_id for m in messages])
    # Structured, not raw text: sender + timestamp let Gemini reason about
    # who said what and when, which matters in group chats where several
    # senders' lines would otherwise be indistinguishable. Never sender_id or
    # chat_id themselves - see _resolve_sender_labels.
    return {
        "messages": [
            {
                "sender_name": labels.get(str(m.sender_id), {}).get("name") if m.sender_id is not None else None,
                "sender_phone_number": labels.get(str(m.sender_id), {}).get("phone_number") if m.sender_id is not None else None,
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

    labels = await _resolve_sender_labels(session, [r.sender_id for r in result.results])
    # No message_id/sender_id in the output handed to Gemini - see
    # _resolve_sender_labels. chat_id is the one internal id deliberately
    # kept here: it's the sole handle the model has to target a follow-up
    # read_history(chat_id=...) call on a specific hit - never text it would
    # reproduce to a person, only an argument it passes back into another
    # tool call.
    return {
        "results": [
            {
                "chat_id": str(r.chat_id),
                "sender_name": labels.get(str(r.sender_id), {}).get("name") if r.sender_id is not None else None,
                "sender_phone_number": labels.get(str(r.sender_id), {}).get("phone_number") if r.sender_id is not None else None,
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
    counterpart = await _describe_escalation_counterpart(session, chat_id, agent.owner_user_id)

    try:
        await send_push(
            agent.owner_user_id,
            title="Your agent needs you",
            body=reason,
            data={"chat_id": str(chat_id)},
        )
    except Exception:
        logger.exception("agent %s: send_push failed for pause_and_escalate on chat %s", agent.id, chat_id)

    try:
        from modules.messaging.send import send_system_message

        # reason is written by the model itself, in whatever language it's
        # been conversing with the owner in (CHAT_STYLE_RULES instructs it to
        # write the full notice, not a fill-in-the-blank fragment). The only
        # fixed part is the counterpart line - deliberately label-free (👤
        # rather than an English word like "With:") since it's built
        # server-side with no language context to translate a label into.
        # *bold* renders in the PoC (poc/composables/messageFormat.js),
        # matching the WhatsApp-style formatting CHAT_STYLE_RULES already
        # teaches the model to use in its own replies - the counterpart name
        # is the one part worth making visually stand out (bold), not a
        # dash/bullet list.
        notice = f"🤝 {reason}\n👤 *{counterpart}*"
        await send_system_message(session, agent.owner_agent_chat_id, notice)
    except Exception:
        logger.exception("agent %s: failed to post handoff system message for chat %s", agent.id, chat_id)

    return {"paused_chat_id": str(chat_id)}


# ADR 0047 decision 5: get_knowledge_index/fetch_chunk supersede
# search_knowledge for the document-RAG use case (browse-then-fetch is a
# cleaner mental model than a keyword query, and the chunks table has no
# natural "search terms" to key off of). search_knowledge is dropped from
# the registry - not implemented, per the ADR's "no migration cost, just
# don't build the superseded tool."
EXECUTION_TOOL_HANDLERS = {
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

# pause_and_escalate is the one execution tool whose handler needs the
# turn's triggering chat_id (to know which chat to pause) - every other
# handler has the uniform (session, agent, arguments) signature dispatched
# generically in dispatch.py, so this name is special-cased there rather
# than changing that signature for every other tool.
CHAT_SCOPED_TOOL_NAMES = frozenset({"pause_and_escalate"})
