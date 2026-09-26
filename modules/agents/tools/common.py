"""Shared helpers for every tool handler (ADR 0056, split from tools.py).

Identity masking, the daily-send quota check, and AgentToolCallLog writes -
used by both execution.py and config_mode.py.
"""
import asyncio
import logging
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from infra.ids.client import next_id
from infra.ratelimit.service import check_and_increment, check_sliding_window
from modules.agents.models import Agent, AgentToolCallLog

logger = logging.getLogger(__name__)

_SECONDS_PER_DAY = 86400


class ToolDeniedError(Exception):
    """Raised internally when a restriction blocks a tool call - carries the
    denial_reason string logged onto AgentToolCallLog and reported back to
    Gemini as the function response, so the model can adapt (e.g. stop
    trying to message a blocked chat)."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


async def _resolve_sender_labels(session: AsyncSession, sender_ids: list) -> dict:
    """Hard, server-side identity mask (never a prompt instruction): every
    tool result handed to Gemini must carry a human-readable label instead of
    a raw internal id, matching the CLAUDE.md frontend rule (user_id is
    strictly for backend logic, never surfaced to an end user) applied here
    to agent output instead of the UI. Returns {user_id_str: {"name": ...,
    "phone_number": ...}}; "name" falls back display_name -> username ->
    phone_number, same convention as ADR 0024.

    sender_ids may be int (ORM rows, e.g. get_message_history) or str
    (Pydantic IdStr-typed search results, per the Snowflake-id-as-string
    convention) - normalized to int for the users.id (BigInteger) lookup,
    keyed back by the original string form for str-agnostic caller lookups.
    """
    from modules.users.crud import get_users_by_ids

    ids = {int(sid) for sid in sender_ids if sid is not None}
    users = await get_users_by_ids(session, list(ids))
    return {
        str(uid): {
            "name": user.display_name or user.username or user.phone_number,
            "phone_number": user.phone_number,
        }
        for uid, user in users.items()
    }


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


async def _consume_owner_send_budget(agent: Agent) -> None:
    """ADR 0058: the agent impersonates its owner on every send
    (sender_id=agent.owner_user_id in execution.py), so it must draw from the
    SAME per-user WS send_message sliding-window budget the owner's own
    client consumes - never a separate or nonexistent one. Checks the exact
    Redis keys the Rust ws_gateway writes (rlsw:send_message:{owner_user_id}
    + rlsw:send_message_burst:{owner_user_id}), bypassing the gateway only as
    a transport, never as a limit.

    Retries with exponential backoff instead of failing the tool call - the
    outer per-turn asyncio.wait_for(AGENT_TURN_TIMEOUT_SECONDS) in
    invoke_worker.py is the real ceiling, so this loop has no independent
    deadline of its own; it just keeps trying until that wait_for cancels it.
    """
    owner_id = agent.owner_user_id
    backoff_ms = settings.AGENT_SEND_RATE_LIMIT_BACKOFF_MS
    while True:
        allowed_rate = await check_sliding_window(
            owner_id, "send_message", settings.WS_SEND_MESSAGE_RATE_MAX, settings.WS_SEND_MESSAGE_RATE_WINDOW_SECONDS,
        )
        allowed_burst = await check_sliding_window(
            owner_id, "send_message_burst", settings.WS_SEND_MESSAGE_BURST_MAX, settings.WS_SEND_MESSAGE_BURST_WINDOW_SECONDS,
        )
        if allowed_rate and allowed_burst:
            return
        await asyncio.sleep(backoff_ms / 1000)
        backoff_ms = min(backoff_ms * 2, settings.AGENT_SEND_RATE_LIMIT_BACKOFF_MAX_MS)


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


async def _chat_is_group(session: AsyncSession, chat_id: int) -> bool:
    from modules.chats.crud.crud_chat import get_chat_by_id

    chat = await get_chat_by_id(session, chat_id)
    return bool(chat is not None and chat.is_group)


async def _describe_escalation_counterpart(session: AsyncSession, chat_id: int, owner_user_id: int) -> str:
    """Human-readable label for who the paused chat is with, for the owner's
    handoff notice - name + phone number for a 1:1, or the group title (never
    a raw chat_id/user_id, per the identity-masking rule elsewhere in this
    module)."""
    from modules.chats.crud.crud_chat import get_chat_by_id
    from modules.chats.crud.crud_participant import get_chat_participants_with_users

    chat = await get_chat_by_id(session, chat_id)
    if chat is not None and chat.is_group:
        return f'the group "{chat.title}"' if chat.title else "a group chat"

    participants = await get_chat_participants_with_users(session, chat_id)
    for participant in participants:
        user = participant.user
        if user is None or user.id == owner_user_id:
            continue
        name = user.display_name or user.username or user.phone_number
        if name != user.phone_number:
            return f"{name} ({user.phone_number})"
        return user.phone_number
    return "a customer"
