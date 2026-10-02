"""Owner-facing notifications and typing-indicator publishing for agent turns
(split out of invoke_worker.py by ADR 0082). Two independent concerns living
side by side: the private `agent_thinking`/peer `typing` WS events
`_run_turn` publishes while a turn is running, and the once-per-exhaustion
budget notices posted into the owner's own agent chat.
"""
import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from infra.redis.client import redis_client
from modules.agents.invoke_debounce import refresh_typing_indicator
from modules.agents.models import Agent
from modules.agents.time_budget import seconds_until_reset
from realtime import realtime_service

logger = logging.getLogger(__name__)


async def _publish_agent_thinking(owner_user_id: int, status: str, detail: str | None = None) -> None:
    """Ephemeral, fire-and-forget - never persisted, never replayed on
    reconnect (same semantics as the existing `typing` WS event). A failure
    here must never interrupt or fail the turn itself."""
    try:
        await realtime_service.publish_user_event(
            owner_user_id,
            {"event": "agent_thinking", "status": status, "detail": detail},
        )
    except Exception:
        logger.exception("agent_worker: failed to publish agent_thinking for owner %s", owner_user_id)


# How often to re-publish the real chat `typing` event while a turn is
# working - matches useTyping.js's TYPING_SEND_THROTTLE_MS/TYPING_EXPIRY_MS on
# the client (a real user's browser resends every 3s, and a receiver's
# indicator expires 5s after the last one), so a working agent keeps looking
# "typing" continuously instead of flickering off between updates.
_PEER_TYPING_REFRESH_SECONDS = 3.0

# Tools whose execution posts a message into the triggering chat - once one of
# these lands, the peer-visible typing loop must stop immediately (see its
# cancellation right after execute_tool_call in invoke_worker.py).
_MESSAGE_SENDING_TOOL_NAMES = frozenset({"send_message", "reply_message", "continue_message"})


async def _publish_peer_typing_loop(chat_id: int, sender_id: int, *, owns_indicator: bool) -> None:
    """Real `typing` event fanned out to the chat's other participants (same
    `publish_event` a genuine user's WS `typing` frame goes through) - runs
    for the lifetime of an execution-mode turn targeting a real chat, so
    whoever the agent is about to message sees an ordinary "typing…"
    indicator instead of nothing, until the reply itself lands. Distinct from
    `_publish_agent_thinking`, which is a private, owner-only signal for the
    agent drawer and is never seen by other chat members.

    `owns_indicator` (ADR 0075): True when this call's `claim_typing_indicator`
    won the race for this chat - only the owning loop actually refreshes the
    shared marker and releases it on exit. A non-owning loop (this turn was
    superseded and its replacement already holds the marker) still publishes
    the real `typing` WS event on the same cadence - the frontend indicator
    itself is per-publish, not keyed off the marker - it just never touches
    the marker's lifecycle, so a superseded turn's own cancellation can never
    tear down the replacement turn's ownership of it."""
    try:
        while True:
            # user_id must be a string, matching every other id in every
            # other event on the wire (Snowflake-id-as-string convention,
            # .claude_docs/database_schema.md) - the Rust ws_gateway forwards
            # this payload byte-for-byte from Redis with no reserialization
            # (crates/ws_gateway/src/fanin.rs), so a raw Python int here
            # reaches the browser as a JSON number while every real `typing`
            # frame's user_id is `.to_string()`'d (handlers.rs). The frontend
            # compares ids with strict `===` throughout, so this one event
            # type silently failed every identity check downstream.
            await realtime_service.publish_event(
                chat_id, {"event": "typing", "user_id": str(sender_id), "kind": "typing"}
            )
            if owns_indicator:
                await refresh_typing_indicator(chat_id)
            await asyncio.sleep(_PEER_TYPING_REFRESH_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("agent_worker: failed to publish peer typing for chat %s", chat_id)


# ADR 0059: fixed English notice, posted at most once per exhaustion event
# (SET NX cooldown, same pattern as trigger_engine._notify_activation_quota_
# exceeded), always into the owner's own agent chat - never into whatever
# third-party chat the turn was actually serving.
_TOKEN_BUDGET_EXHAUSTED_NOTICE = (
    "Your agent has used up its token budget for this time window and will "
    "pause responding until it resets. It'll pick back up automatically."
)

# Fixed English notice for the AGENT_TURN_MAX_TOOL_ROUNDTRIPS cap - always
# posted (no cooldown, unlike the token-budget notice above: this cap is per-
# turn, not a standing pause, so there is no ongoing state to avoid re-
# notifying about).
_ROUND_TRIP_CAP_NOTICE = (
    "This request was too complex to finish in one go, so your agent stopped "
    "partway through. Please try again with a simpler or more specific request."
)


async def _notify_token_budget_exhausted(session: AsyncSession, agent: Agent, window: str) -> None:
    cooldown_key = f"agent_token_budget_notice_sent:{window}:{agent.id}"
    window_seconds = (
        settings.AGENT_TOKEN_BUDGET_5H_WINDOW_SECONDS
        if window == "5h"
        else settings.AGENT_TOKEN_BUDGET_7D_WINDOW_SECONDS
    )
    try:
        acquired = await redis_client.set(cooldown_key, "1", nx=True, ex=window_seconds)
        if not acquired:
            return
        from modules.messaging.send import send_system_message

        await send_system_message(session, agent.owner_agent_chat_id, _TOKEN_BUDGET_EXHAUSTED_NOTICE)
        await session.commit()
    except Exception:
        logger.exception("failed to notify owner of agent %s token budget exhaustion", agent.id)


async def _notify_daily_budget_exhausted(session: AsyncSession, agent: Agent) -> None:
    """Daily active-processing-time budget (has_budget_remaining) exhausted -
    checked in process_entry *before* _run_turn is ever called, so unlike
    every other exhaustion path above this one previously had no notice path
    at all (not even the owner-only agent_thinking 'error' status, since that
    lives inside _run_turn). Same SET NX cooldown pattern as
    _notify_token_budget_exhausted, one notice per exhaustion event. Unlike
    the per-minute Gemini call budget (invoke_worker._run_turn's in-place
    retry), this window doesn't reset again soon, so there is nothing to
    usefully retry - only a notice makes sense here."""
    cooldown_key = f"agent_daily_budget_notice_sent:{agent.id}"
    try:
        remaining = await seconds_until_reset(agent.id)
        # remaining can be 0 right at the boundary (key just expired) -
        # cooldown TTL still needs a positive value, so floor it.
        cooldown_seconds = max(remaining, 60)
        acquired = await redis_client.set(cooldown_key, "1", nx=True, ex=cooldown_seconds)
        if not acquired:
            return
        hours = max(1, round(remaining / 3600))
        notice = (
            "Your agent has used up its processing time budget for today and "
            f"will pause responding for up to {hours} hour{'s' if hours != 1 else ''}. "
            "It'll pick back up automatically."
        )
        from modules.messaging.send import send_system_message

        await send_system_message(session, agent.owner_agent_chat_id, notice)
        await session.commit()
    except Exception:
        logger.exception("failed to notify owner of agent %s daily budget exhaustion", agent.id)
