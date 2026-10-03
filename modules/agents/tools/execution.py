"""Execution-mode tool handlers (ADR 0056, split from tools.py).

Every tool dispatches to an existing service handler (modules.messaging /
modules.chats) using the owner's own user_id - an agent is a service account,
never a bypass of normal per-user permissions or rate limits. Enforcement of
Agent.restrictions happens here, server-side, never left to the system
prompt (see docs/adr/0045).
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from modules.agents.attachment_judge import evaluate_attachment_match
from modules.agents.cache import sync_agent_cache
from modules.agents.crud import (
    ScheduleQuotaExceededError,
    TriggerNotFoundError,
    delete_agent_trigger,
    get_knowledge_chunk,
    list_knowledge_index,
    update_agent_triggers,
)
from modules.agents.knowledge_service import KnowledgeValidationError, search_knowledge_semantic
from modules.agents.models import Agent
from modules.agents.schedule import sync_schedule_zset
from modules.agents.tools.common import (
    SELF_TARGET_REASON,
    ToolDeniedError,
    _chat_is_group,
    _consume_owner_send_budget,
    _resolve_sender_labels,
    escalate_chat,
)
from modules.chats import service as chat_service
from modules.chats.crud.crud_participant import is_participant
from modules.chats.crud.crud_private_chat_pair import get_pair_chat_id
from modules.messaging import service as message_service
from modules.messaging.common import AGENT_REPLY_MESSAGE_TYPE
from modules.messaging.crud import (
    count_messages_in_range,
    get_latest_incoming_message,
    get_message_by_id,
    get_messages_in_range,
    list_attached_files,
)
from modules.messaging.read_api import get_message_history
from modules.search import service as search_service
from modules.search.errors import SearchQueryTooShortError
from modules.vector_search import service as vector_search_service
from modules.vector_search.errors import (
    EmbeddingProviderError,
    EmbeddingProviderQuotaExceededError,
    EmbeddingProviderUnavailableError,
    VectorSearchQueryTooShortError,
)

logger = logging.getLogger(__name__)


def _new_client_message_id() -> str:
    # The agent has no real client - a fresh uuid per send just satisfies the
    # idempotency-key shape process_outgoing expects.
    return f"agent-{uuid.uuid4().hex}"


async def _tool_send_message(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    content = str(arguments["content"])
    target_user_id = arguments.get("target_user_id")
    if arguments.get("chat_id") in (None, ""):
        if target_user_id in (None, ""):
            raise ToolDeniedError("provide chat_id or target_user_id")
        # No chat yet: open (or fetch) the 1:1 chat as part of the send itself,
        # saving the model a separate open-chat round-trip.
        target_user_id = int(target_user_id)
        if target_user_id == agent.owner_user_id:
            raise ToolDeniedError(SELF_TARGET_REASON)
        if await get_pair_chat_id(session, agent.owner_user_id, target_user_id) is None:
            if not agent.restrictions.get("can_message_new_private_contacts", True):
                raise ToolDeniedError("can_message_new_private_contacts is disabled")
            if not agent.restrictions.get("can_message_private", True):
                raise ToolDeniedError("can_message_private is disabled")
        chat = await chat_service.get_or_create_private_chat(
            session, agent.owner_user_id, target_user_id, sender_agent_id=agent.id
        )
        chat_id = chat.id
    else:
        chat_id = int(arguments["chat_id"])

    if chat_id == agent.owner_agent_chat_id:
        # This tool is unioned into the Supervisor/Builder toolset (ADR 0062)
        # so the owner can ask the agent to message a THIRD party directly
        # mid-conversation. It must never target the agent's own config chat
        # itself - that channel is exclusively the turn's own plain-text
        # reply (_post_config_reply), posted once per turn. Without this
        # guard, a config-mode turn could legally call send_message on its
        # own owner_agent_chat_id in one round-trip and still fall through to
        # a normal plain-text reply in a later round-trip, landing two
        # separate messages in the owner's chat for a single turn (2026-09-28,
        # user-reported: got a good reply immediately followed by a stray
        # third-person status-report message).
        raise ToolDeniedError("cannot use send_message on the agent's own config chat")
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

    await _consume_owner_send_budget(agent)

    message = await message_service.process_outgoing(
        session,
        sender_id=agent.owner_user_id,
        chat_id=chat_id,
        client_message_id=_new_client_message_id(),
        content=content,
        type=AGENT_REPLY_MESSAGE_TYPE,
        sender_agent_id=agent.id,
    )
    return {"message_id": str(message.id)}


async def _tool_continue_message(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    """ADR 0102: same send path/restrictions/quota as send_message; the
    per-turn cap lives in invoke_turn_loop.dispatch_tool_call (it needs the
    turn state, which tool handlers don't receive)."""
    return await _tool_send_message(session, agent, arguments)


async def _tool_reply_message(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    chat_id = int(arguments["chat_id"])
    reply_to_message_id = int(arguments["reply_to_message_id"])
    content = str(arguments["content"])

    if chat_id == agent.owner_agent_chat_id:
        # Same reasoning as _tool_send_message above - never a valid target
        # for this tool, only for the turn's own plain-text reply.
        raise ToolDeniedError("cannot use reply_message on the agent's own config chat")
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

    await _consume_owner_send_budget(agent)

    message = await message_service.process_outgoing(
        session,
        sender_id=agent.owner_user_id,
        chat_id=chat_id,
        client_message_id=_new_client_message_id(),
        content=content,
        reply_to_message_id=reply_to_message_id,
        sender_agent_id=agent.id,
    )
    return {"message_id": str(message.id)}


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
        sender_agent_id=agent.id,
    )
    return {"left": True}


READ_HISTORY_PAGE_SIZE = 20


def _clamp_tool_limit(requested: Optional[object], *, default: int, max_limit: Optional[int] = None) -> int:
    """Model-requested `limit` for a paginated/top-K tool: falls back to
    `default` when omitted, always clamped to [1, max_limit] regardless of
    what the model asks for - never trusted as-is (same posture as every
    other Agent.restrictions/quota check in this module)."""
    max_limit = settings.AGENT_TOOL_RESULT_MAX_LIMIT if max_limit is None else max_limit
    if requested is None:
        return default
    try:
        value = int(requested)
    except (TypeError, ValueError):
        raise ToolDeniedError(f"invalid limit '{requested}', expected a positive integer")
    return max(1, min(value, max_limit))


async def _tool_read_history(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    chat_id = int(arguments["chat_id"])
    before_id = arguments.get("before_id")
    before_id = int(before_id) if before_id is not None else None
    page_size = _clamp_tool_limit(arguments.get("limit"), default=READ_HISTORY_PAGE_SIZE)

    if chat_id in {int(cid) for cid in agent.restrictions.get("blocked_read_chat_ids", [])}:
        raise ToolDeniedError("chat_id is in blocked_read_chat_ids")

    # Fetch one extra row to detect has_more without a separate count query
    # (ADR 0067) - same trick cursor-paginated endpoints use elsewhere.
    messages = list(
        await get_message_history(
            session, agent.owner_user_id, chat_id, before_id=before_id, limit=page_size + 1
        )
    )
    has_more = len(messages) > page_size
    messages = messages[:page_size]
    next_before_id = str(messages[-1].id) if has_more else None

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
            for m in reversed(messages)  # oldest first for a readable transcript
        ],
        # ADR 0067: has_more tells the model this page isn't the whole chat.
        "has_more": has_more,
        "next_before_id": next_before_id,
    }


async def _tool_count_messages_in_range(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    """ADR 0072: cheap pre-check the model must call before bulk_fetch_messages.
    Decides and stashes the confirmation itself (rather than leaving that to
    the prompt alone) so a mismatched/skipped confirmation is structurally
    impossible, not just discouraged: bulk_fetch_messages's hard gate only
    ever honors a pending_confirmation written by this handler."""
    chat_id = int(arguments["chat_id"])
    start_at = _parse_tool_datetime(arguments.get("start_date"), is_end=False)
    end_at = _parse_tool_datetime(arguments.get("end_date"), is_end=True)

    if chat_id in {int(cid) for cid in agent.restrictions.get("blocked_read_chat_ids", [])}:
        raise ToolDeniedError("chat_id is in blocked_read_chat_ids")
    if not await is_participant(session, chat_id, agent.owner_user_id):
        raise ToolDeniedError("owner is not a participant of chat_id")

    count = await count_messages_in_range(
        session, chat_id, agent.owner_user_id, start_at=start_at, end_at=end_at
    )

    if count > settings.AGENT_BULK_FETCH_MAX_MESSAGES:
        agent.pending_confirmation = None
        return {
            "count": count,
            "too_large": True,
            "max_allowed": settings.AGENT_BULK_FETCH_MAX_MESSAGES,
        }

    now = datetime.now(timezone.utc)
    agent.pending_confirmation = {
        "tool": "bulk_fetch_messages",
        "chat_id": str(chat_id),
        "start_at": start_at.isoformat() if start_at else None,
        "end_at": end_at.isoformat() if end_at else None,
        "count": count,
        "created_at": now.isoformat(),
    }
    await session.flush()
    return {
        "count": count,
        "too_large": False,
        "needs_confirmation": True,
    }


def _pending_confirmation_matches(
    agent: Agent, chat_id: int, start_at: Optional[datetime], end_at: Optional[datetime]
) -> bool:
    pending = agent.pending_confirmation
    if not isinstance(pending, dict) or pending.get("tool") != "bulk_fetch_messages":
        return False

    created_at = pending.get("created_at")
    try:
        if created_at is None or datetime.now(timezone.utc) - datetime.fromisoformat(created_at) > timedelta(
            minutes=settings.AGENT_PENDING_CONFIRMATION_TTL_MINUTES
        ):
            return False
    except ValueError:
        return False

    if pending.get("chat_id") != str(chat_id):
        return False
    pending_start = pending.get("start_at")
    pending_end = pending.get("end_at")
    call_start = start_at.isoformat() if start_at else None
    call_end = end_at.isoformat() if end_at else None
    return pending_start == call_start and pending_end == call_end


async def _tool_bulk_fetch_messages(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    """ADR 0072: single-shot fetch of up to AGENT_BULK_FETCH_MAX_MESSAGES
    messages, for whole-chat summarization. Hard-gated, server-side, on an
    exactly-matching, unexpired Agent.pending_confirmation written by
    count_messages_in_range - never runs off the model's say-so alone, and
    re-verifies the true count at call time (closes the race where messages
    arrived between the confirmation and this call)."""
    chat_id = int(arguments["chat_id"])
    start_at = _parse_tool_datetime(arguments.get("start_date"), is_end=False)
    end_at = _parse_tool_datetime(arguments.get("end_date"), is_end=True)
    fetch_limit = _clamp_tool_limit(
        arguments.get("limit"),
        default=settings.AGENT_BULK_FETCH_MAX_MESSAGES,
        max_limit=settings.AGENT_BULK_FETCH_MAX_MESSAGES,
    )

    if chat_id in {int(cid) for cid in agent.restrictions.get("blocked_read_chat_ids", [])}:
        raise ToolDeniedError("chat_id is in blocked_read_chat_ids")
    if not await is_participant(session, chat_id, agent.owner_user_id):
        raise ToolDeniedError("owner is not a participant of chat_id")

    if not _pending_confirmation_matches(agent, chat_id, start_at, end_at):
        raise ToolDeniedError(
            "no matching confirmed request - call count_messages_in_range for this exact "
            "chat_id/date range first and get the owner's explicit confirmation before "
            "calling bulk_fetch_messages"
        )

    count = await count_messages_in_range(
        session, chat_id, agent.owner_user_id, start_at=start_at, end_at=end_at
    )
    if count > settings.AGENT_BULK_FETCH_MAX_MESSAGES:
        agent.pending_confirmation = None
        raise ToolDeniedError(
            f"chat now has {count} messages in range, over the {settings.AGENT_BULK_FETCH_MAX_MESSAGES} limit "
            "- narrow the range and confirm again"
        )

    after_id = None
    if arguments.get("after_message_id") is not None:
        try:
            after_id = int(arguments["after_message_id"])
        except (TypeError, ValueError):
            raise ToolDeniedError("after_message_id must be the next_after_message_id from a previous page")

    messages = await get_messages_in_range(
        session, chat_id, agent.owner_user_id, start_at=start_at, end_at=end_at,
        limit=fetch_limit, after_id=after_id,
    )

    # ADR 0105: bound the result's size - per-message cap, then a total
    # character budget (always at least one message so paging progresses).
    msg_cap = settings.AGENT_BULK_FETCH_MESSAGE_MAX_CHARS
    budget = settings.AGENT_BULK_FETCH_MAX_CHARS
    page: list[tuple] = []
    used = 0
    for m in messages:
        text = m.content or ""
        if len(text) > msg_cap:
            text = f"{text[:msg_cap]} …[truncated {len(text) - msg_cap} chars]"
        if page and used + len(text) > budget:
            break
        page.append((m, text))
        used += len(text)
    has_more = len(page) < len(messages)
    if not has_more:
        agent.pending_confirmation = None
    await session.flush()

    labels = await _resolve_sender_labels(session, [m.sender_id for m, _ in page])
    result = {
        "messages": [
            {
                "sender_name": labels.get(str(m.sender_id), {}).get("name") if m.sender_id is not None else None,
                "sender_phone_number": labels.get(str(m.sender_id), {}).get("phone_number") if m.sender_id is not None else None,
                "timestamp": m.created_at.isoformat(),
                "content": text,
            }
            for m, text in page
        ],
        "count": len(page),
        "has_more": has_more,
    }
    if has_more:
        result["next_after_message_id"] = str(page[-1][0].id)
    return result


def _check_require_expiry(patch: dict) -> None:
    """ADR 0095: ONE_OFF_ACTION may only create/edit disposable triggers -
    every on_specific_chats entry and the on_unknown_sender/on_any_message
    objects in the patch must carry expires_at and/or max_fires. A patch
    value of None (deleting a chat entry) is exempt - deletion needs no
    expiry. Permanent triggers stay exclusive to BuilderState.BUILDER (its
    call site passes require_expiry=False, the default)."""
    chats = patch.get("on_specific_chats")
    if isinstance(chats, dict):
        for chat_id, chat_patch in chats.items():
            if chat_patch is None:
                continue
            if not (chat_patch.get("expires_at") or chat_patch.get("max_fires")):
                raise ToolDeniedError(
                    f"on_specific_chats[{chat_id}] must set expires_at and/or max_fires here - "
                    "only BuilderState.BUILDER can create a permanent trigger"
                )
    for flag in ("on_unknown_sender", "on_any_message"):
        flag_patch = patch.get(flag)
        if isinstance(flag_patch, dict) and flag_patch.get("enabled"):
            if not (flag_patch.get("expires_at") or flag_patch.get("max_fires")):
                raise ToolDeniedError(
                    f"{flag} must set expires_at and/or max_fires here - "
                    "only BuilderState.BUILDER can create a permanent trigger"
                )


async def _tool_update_own_triggers(
    session: AsyncSession, agent: Agent, arguments: dict, *, require_expiry: bool = False
) -> dict:
    # Hard-scoped to the caller's own agent_id and only the triggers column -
    # never restrictions, never another agent's row (ADR 0045).
    patch = arguments.get("triggers")
    if not isinstance(patch, dict):
        raise ToolDeniedError("triggers argument must be an object")
    if require_expiry:
        _check_require_expiry(patch)

    try:
        updated = await update_agent_triggers(session, agent.id, patch)
    except ScheduleQuotaExceededError as exc:
        raise ToolDeniedError(str(exc))
    await sync_agent_cache(updated)
    if "on_schedule" in patch:
        await sync_schedule_zset(updated)
    return {"triggers": updated.triggers}


async def _tool_update_own_triggers_disposable(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    """ADR 0095: ONE_OFF_ACTION's dispatch entry - same handler, require_expiry
    pinned True so the schema/handler wiring in builder_handoff.py stays a
    plain {name: handler} dict like every other entry."""
    return await _tool_update_own_triggers(session, agent, arguments, require_expiry=True)


async def _tool_delete_own_trigger(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    """ADR 0095: explicit, model-invoked removal of one trigger - the
    counterpart to expires_at/max_fires lapsing on their own. Reachable from
    both ONE_OFF_ACTION and BUILDER (deleting a trigger, unlike creating a
    permanent one, isn't a privileged action)."""
    kind = arguments.get("kind")
    chat_id = arguments.get("chat_id")
    try:
        updated = await delete_agent_trigger(session, agent.id, kind, chat_id=chat_id)
    except TriggerNotFoundError as exc:
        raise ToolDeniedError(str(exc))
    await sync_agent_cache(updated)
    return {"triggers": updated.triggers}


def _parse_tool_datetime(raw: Optional[str], *, is_end: bool) -> Optional[datetime]:
    """ADR 0068/0070: the model passes either a bare ISO date (`YYYY-MM-DD` -
    defaults to midnight for a start bound, end-of-day for an end bound, so a
    same-day range is non-empty) or a full ISO datetime
    (`YYYY-MM-DDTHH:MM:SS`) for a specific time. A malformed value is a model
    mistake, not a system error, so it's rejected as a denial rather than
    raising a 500 deep in fromisoformat."""
    if not raw:
        return None
    raw = str(raw)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        raise ToolDeniedError(f"invalid date/time '{raw}', expected YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS")
    if len(raw) <= 10 and is_end:
        # A bare date with no time component, used as the end bound - extend
        # to the last microsecond of that day instead of leaving it at midnight.
        parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def _tool_search_messages(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    """ADR 0046 decision 3: thin wrapper over the existing ADR 0040 keyword
    search, scoped to the owner's own chats (search_in_chat/search_global
    already enforce membership via the same participants-JOIN pattern used
    everywhere else - never a bypass). Powers schedule-fired turns like
    "check who's waiting for a reply"."""
    query = str(arguments["query"])
    chat_id = arguments.get("chat_id")
    cursor = arguments.get("cursor")
    limit = _clamp_tool_limit(arguments.get("limit"), default=10)
    start_at = _parse_tool_datetime(arguments.get("start_date"), is_end=False)
    end_at = _parse_tool_datetime(arguments.get("end_date"), is_end=True)

    blocked_ids = [int(cid) for cid in agent.restrictions.get("blocked_read_chat_ids", [])]
    if chat_id is not None:
        chat_id = int(chat_id)
        if chat_id in blocked_ids:
            raise ToolDeniedError("chat_id is in blocked_read_chat_ids")

    try:
        if chat_id is not None:
            result = await search_service.search_in_chat(
                session,
                user_id=agent.owner_user_id,
                chat_id=chat_id,
                raw_query=query,
                cursor=cursor,
                limit=limit,
                start_at=start_at,
                end_at=end_at,
            )
        else:
            result = await search_service.search_global(
                session,
                user_id=agent.owner_user_id,
                raw_query=query,
                cursor=cursor,
                limit=limit,
                start_at=start_at,
                end_at=end_at,
                exclude_chat_ids=blocked_ids,
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
        ],
        # ADR 0067: has_more/next_cursor tell the model these aren't all the matches.
        "has_more": result.has_more,
        "next_cursor": result.next_cursor,
    }


async def _tool_search_semantic(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    """ADR 0069: meaning-based counterpart to _tool_search_messages, wrapping
    the existing ADR 0042 semantic search - same owner-scoping (participants
    JOIN enforces membership, never a bypass) and same ADR 0068 date-range
    support. Flat top-K list, no cursor (semantic search isn't paginated)."""
    query = str(arguments["query"])
    chat_id = arguments.get("chat_id")
    vector_limits = vector_search_service.DEFAULT_VECTOR_SEARCH_LIMITS
    limit = _clamp_tool_limit(
        arguments.get("limit"),
        default=vector_limits.default_limit,
        max_limit=min(settings.AGENT_TOOL_RESULT_MAX_LIMIT, vector_limits.max_limit),
    )
    start_at = _parse_tool_datetime(arguments.get("start_date"), is_end=False)
    end_at = _parse_tool_datetime(arguments.get("end_date"), is_end=True)

    blocked_ids = [int(cid) for cid in agent.restrictions.get("blocked_read_chat_ids", [])]
    if chat_id is not None:
        chat_id = int(chat_id)
        if chat_id in blocked_ids:
            raise ToolDeniedError("chat_id is in blocked_read_chat_ids")

    try:
        result = await vector_search_service.semantic_search(
            session,
            user_id=agent.owner_user_id,
            raw_query=query,
            chat_id=chat_id,
            limit=limit,
            start_at=start_at,
            end_at=end_at,
            exclude_chat_ids=blocked_ids,
        )
    except VectorSearchQueryTooShortError as exc:
        raise ToolDeniedError(str(exc))
    except EmbeddingProviderUnavailableError:
        raise ToolDeniedError("semantic search is not available right now")
    except (EmbeddingProviderError, EmbeddingProviderQuotaExceededError):
        raise ToolDeniedError("semantic search failed, try again later")

    labels = await _resolve_sender_labels(session, [r.sender_id for r in result.results])
    # Same identity-masking rule as _tool_search_messages - no raw sender_id
    # in what's handed to Gemini, chat_id kept as the model's tool-call handle.
    return {
        "results": [
            {
                "chat_id": str(r.chat_id),
                "sender_name": labels.get(str(r.sender_id), {}).get("name") if r.sender_id is not None else None,
                "sender_phone_number": labels.get(str(r.sender_id), {}).get("phone_number") if r.sender_id is not None else None,
                "content": r.content,
                "distance": r.distance,
                "created_at": r.created_at.isoformat(),
            }
            for r in result.results
        ],
    }


async def _tool_search_knowledge_semantic(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    """ADR 0078: ranked chunk retrieval over this agent's own knowledge base -
    embeds the query, cosine-searches agent_knowledge_chunks, returns the top
    matches' content directly (no index-then-fetch two-hop). Chunks lacking an
    embedding (e.g. an embed failure mid-ingest) never surface here - they
    remain reachable only via get_knowledge_index/fetch_chunk, which stay
    registered as the fallback."""
    query = str(arguments["query"])
    limit = _clamp_tool_limit(arguments.get("limit"), default=settings.AGENT_KNOWLEDGE_SEARCH_LIMIT, max_limit=20)
    try:
        matches = await search_knowledge_semantic(session, agent, query=query, limit=limit)
    except KnowledgeValidationError as exc:
        raise ToolDeniedError(str(exc))
    return {
        "matches": [
            {
                "document_id": str(m["document_id"]),
                "filename": m["filename"],
                "chunk_id": str(m["chunk_id"]),
                "content": m["content"],
                "distance": m["distance"],
            }
            for m in matches
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


ATTACHED_FILES_LIST_LIMIT = 20


async def _tool_list_attached_files(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    """ADR 0083: what send_attached_file can resend - media messages the
    owner personally sent into their own owner-agent chat. Deliberately
    scoped to (owner_agent_chat_id, owner_user_id) here, not a model-supplied
    chat_id - see the ADR's security-boundary section."""
    limit = _clamp_tool_limit(arguments.get("limit"), default=ATTACHED_FILES_LIST_LIMIT)
    files = await list_attached_files(session, agent.owner_agent_chat_id, agent.owner_user_id, limit=limit)
    return {
        "files": [
            {
                "file_id": str(f.id),
                "filename": f.media_name,
                "caption": f.content,
                "kind": settings.MEDIA_KIND_BY_MESSAGE_TYPE.get(f.type),
                "mime": f.media_mime,
                "size_bytes": f.media_size,
                "uploaded_at": f.created_at.isoformat(),
            }
            for f in files
        ]
    }


async def _tool_send_attached_file(session: AsyncSession, agent: Agent, arguments: dict) -> dict:
    """ADR 0083: resend a file the owner previously attached in their own
    owner-agent chat into a real target chat - same restriction/quota gate as
    send_message, plus the file_id lookup re-verifies both chat_id and
    sender_id at call time (never trusts a stale/model-fabricated file_id)."""
    chat_id = int(arguments["chat_id"])
    file_id = int(arguments["file_id"])
    caption = arguments.get("caption")

    if not agent.restrictions.get("can_send_messages", True):
        raise ToolDeniedError("can_send_messages is disabled")
    if chat_id in {int(cid) for cid in agent.restrictions.get("blocked_read_chat_ids", [])}:
        raise ToolDeniedError("chat_id is in blocked_read_chat_ids")

    source = await get_message_by_id(session, agent.owner_agent_chat_id, file_id)
    if (
        source is None
        or source.sender_id != agent.owner_user_id
        or source.media_key is None
        or not (source.content or "").strip()
        or source.deleted_at is not None
        or source.purged_at is not None
    ):
        raise ToolDeniedError("file_id not found among files the owner attached in this chat")

    if not await is_participant(session, chat_id, agent.owner_user_id):
        raise ToolDeniedError("owner is not a participant of chat_id")

    is_group = await _chat_is_group(session, chat_id)
    if is_group and not agent.restrictions.get("can_message_groups", True):
        raise ToolDeniedError("can_message_groups is disabled")
    if not is_group and not agent.restrictions.get("can_message_private", True):
        raise ToolDeniedError("can_message_private is disabled")

    # ADR 0086: dedicated relevance judge - does this file plausibly match
    # what the other party actually asked for? Judged from text only (their
    # latest message + the owner's own caption/filename), separate from and
    # in addition to judge.py's message-content gate.
    requester_message = await get_latest_incoming_message(session, chat_id, agent.owner_user_id)
    verdict = await evaluate_attachment_match(
        session,
        agent,
        chat_id,
        file_id,
        requester_message=(requester_message.content or "") if requester_message else "",
        caption=source.content or "",
        filename=source.media_name or "",
    )
    if not verdict.is_approved:
        raise ToolDeniedError(f"attachment does not appear to match the request: {verdict.reason}")

    await _consume_owner_send_budget(agent)

    message = await message_service.process_outgoing(
        session,
        sender_id=agent.owner_user_id,
        chat_id=chat_id,
        client_message_id=_new_client_message_id(),
        content=str(caption) if caption else None,
        type=source.type,
        media={
            "key": source.media_key,
            "name": source.media_name,
            "duration_seconds": source.media_duration_seconds,
            "blur_hash": source.media_blur_hash,
        },
        sender_agent_id=agent.id,
    )
    return {"message_id": str(message.id)}


async def _tool_pause_and_escalate(session: AsyncSession, agent: Agent, arguments: dict, chat_id: Optional[int] = None) -> dict:
    """ADR 0047 decision 5: freezes the agent for the *triggering chat only*
    and wakes the human owner. chat_id comes from the turn context (the tool
    has no chat_id argument of its own - it always escalates the chat it was
    invoked from), never from a model-supplied argument, so it can't be used
    to pause an arbitrary chat by guessing an id."""
    if chat_id is None:
        raise ToolDeniedError("pause_and_escalate requires a triggering chat")

    # reason is written by the model itself, in whatever language it's been
    # conversing with the owner in (CHAT_STYLE_RULES instructs it to write
    # the full notice, not a fill-in-the-blank fragment) - escalate_chat
    # posts it verbatim behind the fixed 🤝/counterpart formatting.
    reason = str(arguments.get("reason") or "The agent needs your input to continue.")
    await escalate_chat(session, agent, chat_id, reason)

    return {"paused_chat_id": str(chat_id)}


# ADR 0047 decision 5: get_knowledge_index/fetch_chunk supersede
# search_knowledge for the document-RAG use case (browse-then-fetch is a
# cleaner mental model than a keyword query, and the chunks table has no
# natural "search terms" to key off of). search_knowledge is dropped from
# the registry - not implemented, per the ADR's "no migration cost, just
# don't build the superseded tool."
#
# update_own_triggers/delete_own_trigger are deliberately NOT registered
# here: it let an execution-mode persona talking to a third party
# (sales_agent/support_agent/one_off_executor/summarizer) rewrite its own
# wake-up triggers from attacker-controlled chat text - a prompt-injection
# surface. They stay config-mode-only, wired into ONE_OFF_ACTION/BUILDER's
# handler dicts in builder_handoff.py instead (ADR 0091/0095).
EXECUTION_TOOL_HANDLERS = {
    "send_message": _tool_send_message,
    "reply_message": _tool_reply_message,
    "continue_message": _tool_continue_message,
    "leave_group": _tool_leave_group,
    "read_history": _tool_read_history,
    "count_messages_in_range": _tool_count_messages_in_range,
    "bulk_fetch_messages": _tool_bulk_fetch_messages,
    "search_messages": _tool_search_messages,
    "search_semantic": _tool_search_semantic,
    "search_knowledge_semantic": _tool_search_knowledge_semantic,
    "get_knowledge_index": _tool_get_knowledge_index,
    "fetch_chunk": _tool_fetch_chunk,
    "list_attached_files": _tool_list_attached_files,
    "send_attached_file": _tool_send_attached_file,
    "pause_and_escalate": _tool_pause_and_escalate,
}

# pause_and_escalate is the one execution tool whose handler needs the
# turn's triggering chat_id (to know which chat to pause) - every other
# handler has the uniform (session, agent, arguments) signature dispatched
# generically in dispatch.py, so this name is special-cased there rather
# than changing that signature for every other tool.
CHAT_SCOPED_TOOL_NAMES = frozenset({"pause_and_escalate"})
