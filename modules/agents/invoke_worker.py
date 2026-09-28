"""Consumer for agent_invoke_stream (ADR 0045, step 3).

Drains the stream the Trigger Rule Engine (trigger_engine.py) writes to,
re-checks the kill switch and the daily active-time budget (defense in
depth for the enqueue-to-dequeue window), then runs one agent turn. Runs in
its own `agent_worker` Docker Compose service/process, never inside the main
app - a stuck or crashing Gemini turn must not affect the WS gateway or the
send/fan-out/receipt workers.

The Gemini call + tool dispatch (step 4) run inside `_run_turn`: build a
conversation (system prompt + a read_history-shaped transcript of the
triggering chat), call Gemini, and follow up to AGENT_TURN_MAX_TOOL_ROUNDTRIPS
functionCall round-trips, each dispatched through
modules.agents.tools.execute_tool_call. The whole turn is wrapped in
asyncio.wait_for(AGENT_TURN_TIMEOUT_SECONDS) by process_entry below so a
stuck Gemini call or tool execution can't hold a worker slot indefinitely.

`process_entry` also holds a per-(agent_id, chat_id) turn mutex around
`_run_turn` (ADR 0063) - a debounced fire landing while a previous turn for
the same pair is still running re-arms the debounce timer instead of racing
a second concurrent turn. `_invoke_debounce_poll_loop` (alongside
`_schedule_poll_loop`, same due-ZSET-poll pattern) is what actually turns a
coalesced burst of trigger matches into the single `enqueue_invocation` call
that lands on this stream in the first place.

That same re-arm path also marks the in-flight turn `superseded` (ADR 00732):
`_run_turn` checks this flag at the top of every round-trip and again right
before dispatching send_message/reply_message, ending the turn without
delivering its reply if a newer message has already taken its place. This
does not cancel the underlying Gemini call - it only stops a now-stale
answer from reaching the chat.
"""
import asyncio
import contextlib
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

from redis.exceptions import ResponseError
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from infra.db.connection import session_scope
from infra.ratelimit.service import check_and_increment
from infra.redis.client import redis_client
from modules.agents.builder_flow import BuilderState, get_builder_state_prompt
from modules.agents.crud import get_agent_by_id, get_agents_with_ephemeral_tasks
from modules.agents.ephemeral_tasks import fire_summary_and_complete, sweep_expired_task_ids
from modules.agents.invoke_debounce import (
    acquire_turn_lock,
    arm_debounce,
    arm_debounce_now,
    claim_typing_indicator,
    due_pairs,
    is_superseded,
    mark_superseded,
    pop_latest_message_id,
    refresh_typing_indicator,
    release_turn_lock,
    release_typing_indicator,
)
from modules.agents.invoke_queue import enqueue_invocation, enqueue_schedule_fire
from modules.agents.gemini_client import (
    GeminiChatError,
    TurnResult,
    extract_function_call,
    extract_text,
    function_response_part,
    generate_turn,
)
from modules.agents.judge import evaluate_message, local_redirect_text
from modules.agents.tools.common import escalate_chat
from modules.agents.models import Agent
from modules.agents.personas import get_persona_system_prompt
from modules.agents.schedule import due_members, remove_due_member, reschedule_recurring
from modules.agents.time_budget import has_budget_remaining, record_active_seconds, seconds_until_reset
from modules.agents.token_budget import peek_usage, record_tokens
from modules.agents.tools import execute_tool_call, get_tool_schemas_for_chat, is_config_mode
from modules.messaging import service as message_service
from modules.messaging.common import AGENT_REPLY_MESSAGE_TYPE
from modules.messaging.crud import get_message_by_id
from modules.messaging.read_api import get_message_history
from realtime import realtime_service
from realtime.fanout.base_worker import BaseStreamConsumer

logger = logging.getLogger(__name__)


def _pending_confirmation_note(agent: Agent) -> str:
    """ADR 0072: re-derived every round-trip alongside system_prompt itself
    (same pattern as builder_state) - carries a stashed bulk_fetch_messages
    request across the turn boundary that count_messages_in_range's handler
    created it on. Purely advisory context for the model; the actual gate is
    server-side in bulk_fetch_messages's handler (modules/agents/tools/execution.py),
    which independently re-checks this same field before running."""
    pending = agent.pending_confirmation
    if not isinstance(pending, dict) or pending.get("tool") != "bulk_fetch_messages":
        return ""
    created_at = pending.get("created_at")
    try:
        if created_at is None or datetime.now(timezone.utc) - datetime.fromisoformat(created_at) > timedelta(
            minutes=settings.AGENT_PENDING_CONFIRMATION_TTL_MINUTES
        ):
            return ""
    except ValueError:
        return ""

    return (
        "\n\nYou previously asked the owner to confirm a bulk_fetch_messages request for "
        f"chat_id {pending.get('chat_id')} (start_date={pending.get('start_at') or 'none'}, "
        f"end_date={pending.get('end_at') or 'none'}, {pending.get('count')} messages). If the "
        "owner's latest message confirms it, call bulk_fetch_messages now with that exact "
        "chat_id/date range. If they declined or moved on to something else, do not call it - "
        "just continue normally."
    )

# Short human labels for the agent drawer's live "thinking" indicator
# (AGENT_DRAWER_UI_PLAN.md Wave 2) - purposely coarse (tool name only, no
# arguments), since this is a UX nicety, not a debug/audit log (that's
# AgentToolCallLog).
_TOOL_THINKING_LABELS = {
    "send_message": "Sending a message…",
    "reply_message": "Sending a reply…",
    "create_chat": "Starting a new chat…",
    "leave_group": "Leaving a group…",
    "read_history": "Reading chat history…",
    "update_own_triggers": "Updating its own triggers…",
    "get_knowledge_index": "Looking through its knowledge base…",
    "fetch_chunk": "Reading a knowledge document…",
    "search_messages": "Searching messages…",
    "pause_and_escalate": "Escalating to you…",
    "set_agent_persona": "Updating its persona…",
    "update_agent_rules": "Updating its rules…",
    "set_trigger": "Updating its triggers…",
    "get_agent_status": "Checking its own status…",
    "estimate_api_usage": "Estimating usage…",
    "schedule_one_off_task": "Scheduling a task…",
    "spawn_ephemeral_task": "Starting a one-off task…",
    "transfer_to_builder": "Bringing in the builder…",
    "transfer_to_help_building": "Bringing in help…",
    "transfer_to_help_general": "Bringing in help…",
    "finish_building_agent": "Finishing up and activating…",
}


async def _post_config_reply(session: AsyncSession, agent: Agent, chat_id: int, text: str) -> None:
    """Config-mode turns (Supervisor/Builder/Help, ADR 0049) have no
    send_message-shaped tool - the persona prompts just instruct the model to
    reply in plain text, expecting that text to reach the owner's chat. Unlike
    execution mode (where a persona is expected to call send_message/
    reply_message itself), there is no tool to invoke here, so the worker
    posts the turn's final text response directly via the same
    process_outgoing/AGENT_REPLY_MESSAGE_TYPE path _tool_send_message uses -
    otherwise the model's reply is generated and then silently discarded,
    which is exactly what made agent_builder turns look like the agent
    "thought and then said nothing" (found and fixed 2026-09-24, in-scope bug
    fix, not a new architectural decision)."""
    if not text:
        return
    await message_service.process_outgoing(
        session,
        sender_id=agent.owner_user_id,
        chat_id=chat_id,
        client_message_id=f"agent-{uuid.uuid4().hex}",
        content=text,
        type=AGENT_REPLY_MESSAGE_TYPE,
        sender_agent_id=agent.id,
    )


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
# cancellation right after execute_tool_call below).
_MESSAGE_SENDING_TOOL_NAMES = frozenset({"send_message", "reply_message"})


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


class _TurnSuperseded(Exception):
    """ADR 00732: raised internally by `_generate_turn_or_supersede` when the
    supersede flag fires while a Gemini call is in flight - caught right
    where it's raised, never escapes `_run_turn`."""


# How often to poll the supersede flag while a Gemini call is in flight
# (ADR 00732) - short enough that a superseded turn's outbound HTTP request
# gets cancelled quickly instead of running to completion and contending
# with the replacement turn's own call to the same API.
_SUPERSEDE_POLL_SECONDS = 0.5


async def _generate_turn_or_supersede(agent_id: int, chat_id: int | None, **kwargs) -> TurnResult:
    """Wraps generate_turn so a turn that gets superseded (ADR 00732) mid-call
    actually stops talking to Gemini instead of running the request to
    completion in the background - two real back-to-back generateContent
    calls for the same agent (the stale one finishing, then the replacement
    starting right after) was observed to make the outbound connection to
    Gemini time out under that back-to-back load. Cancelling the stale call's
    task also cancels its underlying httpx request.

    chat_id=None (schedule/ephemeral-task turns) never contends with
    anything, so it always just awaits generate_turn directly."""
    if chat_id is None:
        return await generate_turn(**kwargs)

    call_task = asyncio.ensure_future(generate_turn(**kwargs))
    try:
        while True:
            done, _ = await asyncio.wait({call_task}, timeout=_SUPERSEDE_POLL_SECONDS)
            if done:
                return call_task.result()
            if await is_superseded(agent_id, chat_id):
                # Re-mark it - is_superseded is a get-and-delete and the
                # round-trip-top/pre-send checks elsewhere in _run_turn still
                # need to see it, but this path returns straight out of
                # _run_turn instead of reaching either of them.
                call_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await call_task
                raise _TurnSuperseded()
    finally:
        if not call_task.done():
            call_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await call_task


async def _check_gemini_call_budget(agent_id: int) -> bool:
    return await check_and_increment(
        agent_id,
        "agent_gemini_calls",
        settings.AGENT_GEMINI_CALLS_PER_MINUTE,
        settings.AGENT_GEMINI_CALLS_WINDOW_SECONDS,
    )


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
    retry above), this window doesn't reset again soon, so there is nothing
    to usefully retry - only a notice makes sense here."""
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


def _format_history_transcript(history) -> str | None:
    """Formats a message history window into a single 'Agent: ...' /
    'Customer: ...' transcript block (oldest first) - the explicit role
    label (rather than a raw sender_id) reads far better to Gemini than a
    numeric id, and matches how a human would paste a chat log. Truncated to
    AGENT_HISTORY_TRANSCRIPT_MAX_CHARS from the start (oldest lines dropped
    first) so the most recent context always survives a long/verbose chat.

    Customer/Owner lines followed later in the transcript by an Agent line
    are marked '[already handled]' (ADR 0071) - the agent would not have
    replied without having acted on them, so re-seeing them unmarked on a
    later, unrelated turn otherwise reads to Gemini as still-pending and can
    trigger a re-execution of whatever tool call handled them the first time.
    Only the trailing run of Customer/Owner lines with no Agent reply after
    them - the genuinely unanswered tail - is left unmarked."""
    ordered = [m for m in reversed(list(history)) if m.content]
    is_agent = [m.type == AGENT_REPLY_MESSAGE_TYPE for m in ordered]
    handled = [any(is_agent[i + 1:]) for i in range(len(ordered))]
    lines = [
        f'{"Agent" if is_agent[i] else "Customer"}: {m.content}'
        + ("" if is_agent[i] or not handled[i] else " [already handled]")
        for i, m in enumerate(ordered)
    ]
    if not lines:
        return None
    transcript = "\n".join(lines)
    if len(transcript) > settings.AGENT_HISTORY_TRANSCRIPT_MAX_CHARS:
        transcript = "…(earlier messages truncated)…\n" + transcript[-settings.AGENT_HISTORY_TRANSCRIPT_MAX_CHARS:]
    return transcript


async def _build_initial_contents(session: AsyncSession, agent: Agent, chat_id: int) -> list[dict]:
    """Seeds the conversation with the same structured shape read_history
    hands back to the model - sender/timestamp/content - so Gemini's first
    turn already has context instead of starting from nothing."""
    history = await get_message_history(session, agent.owner_user_id, chat_id, limit=20)
    transcript = _format_history_transcript(history) or "(no prior text messages in this chat)"
    prompt = (
        f"You were woken up by new activity in chat {chat_id}. "
        f"Recent chat history (oldest first):\n{transcript}\n\n"
        "Decide whether and how to respond using the available tools."
    )
    return [{"role": "user", "parts": [{"text": prompt}]}]


async def _build_schedule_contents(session: AsyncSession, agent: Agent, instruction: str, chat_id: int | None) -> list[dict]:
    """Seeds a schedule-fired turn (ADR 0046 decision 3) from the entry's
    free-text instruction instead of chat history - optionally joined with
    the target chat's recent history when the entry set a chat_id."""
    parts = [f"Scheduled task: {instruction}"]
    if chat_id is not None:
        history = await get_message_history(session, agent.owner_user_id, chat_id, limit=20)
        transcript = _format_history_transcript(history)
        if transcript:
            parts.append(f"Recent history in chat {chat_id} (oldest first):\n{transcript}")
    parts.append("Decide whether and how to act using the available tools.")
    return [{"role": "user", "parts": [{"text": "\n\n".join(parts)}]}]


async def _run_turn(
    agent_id: int,
    chat_id: int | None,
    message_id: int | None = None,
    schedule_instruction: str | None = None,
    scoped_system_prompt: str | None = None,
) -> None:
    """Runs one Gemini + tool-calling turn for `agent_id`. Message-fired
    (`message_id` set) seeds from chat history; schedule-fired
    (`schedule_instruction` set, ADR 0046 decision 3) seeds from the entry's
    free-text instruction, optionally joined with `chat_id` history. Opens
    its own DB session (this consumer's caller session is per-batch and
    shouldn't be held across a slow Gemini call). Every failure is logged and
    swallowed - a bad turn must never crash the worker loop; the daily time
    budget still gets accounted by the caller's `finally`.

    `scoped_system_prompt` (ADR 0061) replaces the agent's persona +
    system_prompt for this one turn only, when set (schedule_one_off_task's
    optional field, or an ephemeral task's fixed relay/summarize template) -
    never written back to agent.system_prompt."""
    async with session_scope() as session:
        agent = await get_agent_by_id(session, agent_id)
        if agent is None or not agent.is_enabled:
            return

        # Live "thinking" status for the agent drawer (AGENT_DRAWER_UI_PLAN.md
        # Wave 2) - ephemeral, bounded by the turn's own limits (at most
        # AGENT_TURN_MAX_TOOL_ROUNDTRIPS + 1 tool_call pushes), so no separate
        # rate limit is needed. Owner-only signal, so it must only fire for
        # config-mode turns (the owner's own drawer chat) - an execution-mode
        # turn against a third-party chat has nothing to do with whatever the
        # owner might have open in their drawer right now, so it must not push
        # a "thinking" status there (2026-09-25, user-reported: the drawer lit
        # up "thinking" while the agent was mid-reply to an unrelated
        # customer). "done"/"error" mirrors the same gate in the finally below.
        owner_user_id = agent.owner_user_id
        ended_status = "error"
        # Must agree with the real tool-mode gate (dispatch.is_config_mode),
        # which returns False for chat_id=None - a schedule/ephemeral-task
        # turn with no chat target dispatches execution-mode tools, so it must
        # not be flagged as config-mode here either (previously `chat_id is
        # None or ...` disagreed with the gate, making a chat_id-less turn
        # look like config-mode for the drawer's "thinking" indicator while
        # actually running with execution-mode tool_schemas underneath).
        config_mode_turn = is_config_mode(agent, chat_id)
        if config_mode_turn:
            await _publish_agent_thinking(owner_user_id, "started")
        # Real peer-visible "typing" indicator (not the owner-only
        # agent_thinking above) - only for execution-mode turns against an
        # actual chat with the agent, never the owner's own config-mode
        # drawer chat (that already gets agent_thinking) and never a
        # schedule-fired turn with chat_id=None.
        peer_typing_task: asyncio.Task | None = None
        owns_typing_indicator = False
        if chat_id is not None and not is_config_mode(agent, chat_id):
            # ADR 0075: claim_typing_indicator is a SET NX - only the first
            # turn touching this chat while activity is unanswered actually
            # owns (refreshes/releases) the shared marker; a turn that starts
            # while a prior turn for the same chat already owns it (this
            # turn is itself the replacement for a mid-turn supersede) still
            # runs its own publish loop, just without touching the marker's
            # lifecycle - see _publish_peer_typing_loop's owns_indicator arg.
            owns_typing_indicator = await claim_typing_indicator(chat_id)
            peer_typing_task = asyncio.create_task(
                _publish_peer_typing_loop(chat_id, owner_user_id, owns_indicator=owns_typing_indicator)
            )
        try:
            # LLM Judge gate (ADR 0053) - execution-mode, message-fired turns
            # only (on_specific_chats/on_unknown_sender/on_any_message alike,
            # no trigger-type carve-out). Never runs for config-mode turns
            # (the owner's own drawer chat - not an untrusted party) or
            # schedule-fired turns (chat_id is None, the "message" is the
            # agent's own instruction, not external input). A rejected
            # message skips the main model entirely and gets a short, locally
            # -templated redirect instead - no second Gemini call.
            if message_id is not None and not config_mode_turn:
                message = await get_message_by_id(session, chat_id, message_id)
                verdict = await evaluate_message(session, agent, chat_id, message_id, message.content if message else None)
                await session.commit()
                if not verdict.is_approved:
                    logger.info(
                        "agent_worker: judge rejected agent %s chat %s message %s: %s",
                        agent_id, chat_id, message_id, verdict.reason,
                    )
                    await message_service.process_outgoing(
                        session,
                        sender_id=agent.owner_user_id,
                        chat_id=chat_id,
                        client_message_id=f"agent-{uuid.uuid4().hex}",
                        content=verdict.redirect_message or local_redirect_text(agent),
                        type=AGENT_REPLY_MESSAGE_TYPE,
                        sender_agent_id=agent.id,
                    )
                    await session.commit()
                    # ADR 0074: a message the judge flags as a deliberate
                    # attack (trade-secret probing, code-execution asks,
                    # targeted prompt-injection) - as opposed to ordinary
                    # off-topic drift - additionally gets the same
                    # freeze-and-notify treatment as a model-initiated
                    # pause_and_escalate, using the judge's own `reason` as
                    # the notice text (no conversational turn ran to author
                    # one). Customer-facing behavior above is unchanged.
                    if verdict.is_malicious:
                        logger.warning(
                            "agent_worker: judge flagged malicious intent, "
                            "agent %s chat %s message %s: %s",
                            agent_id, chat_id, message_id, verdict.reason,
                        )
                        await escalate_chat(
                            session, agent, chat_id, verdict.reason, notice_prefix="⚠️"
                        )
                        await session.commit()
                    ended_status = "done"
                    return

            if schedule_instruction is not None:
                contents = await _build_schedule_contents(session, agent, schedule_instruction, chat_id)
            else:
                contents = await _build_initial_contents(session, agent, chat_id)

            # BYOK (ADR 0046 decision 5) is disabled for now (2026-09-24, not
            # available yet in the frontend) - always use the shared key, even
            # if an agent has a stored encrypted_gemini_api_key from before
            # this was turned off. Decrypt logic (crypto.py) is left in place
            # so re-enabling later is just removing this early return.
            api_key: str | None = None
            # Explicit flag rather than `round_trip == 0` - the Gemini call
            # budget retry below can advance round_trip via `continue` while
            # still on the *first* iteration's Gemini call (never made yet),
            # which would otherwise make the case A/case B check below think
            # a call already happened when none has.
            gemini_call_made = False

            for round_trip in range(settings.AGENT_TURN_MAX_TOOL_ROUNDTRIPS + 1):
                # ADR 00732/0075: a new message matched a trigger for this
                # same (agent_id, chat_id) while this turn was still running.
                if chat_id is not None and await is_superseded(agent_id, chat_id):
                    if not gemini_call_made and message_id is not None and schedule_instruction is None:
                        # ADR 0075, case A: no Gemini call has been made yet
                        # for this turn (the debounce window alone already
                        # ate the latency, so a fast second message often
                        # lands before the first round-trip even starts) -
                        # merging is free. Re-read history now (it already
                        # includes the newer message, since trigger_engine
                        # only calls mark_superseded after the message that
                        # triggered it has been persisted) and keep running
                        # this same turn/lock/typing-loop instead of
                        # discarding it - one reply that addresses
                        # everything, never two replies and never a reply
                        # that silently ignores the newer message.
                        logger.info(
                            "agent_worker: turn for agent %s chat %s superseded pre-call, "
                            "merging newer history into this turn instead of discarding it",
                            agent_id, chat_id,
                        )
                        contents = await _build_initial_contents(session, agent, chat_id)
                        continue
                    # Case B: this turn already made at least one Gemini
                    # call - merging into a live tool-calling loop isn't
                    # safe (functionCall/thoughtSignature state is
                    # mid-sequence), so end here without delivering anything
                    # (no send_message/reply_message/_post_config_reply).
                    # Does not attempt to cancel the Gemini call itself -
                    # see ADR 00732's Context. Skip the replacement's own
                    # debounce wait (case B already implicitly merges via
                    # the replacement's fresh _build_initial_contents read,
                    # so there is nothing left to gain by waiting again) -
                    # arm_debounce_now only sets the ZSET due-score to now,
                    # it does not touch the turn lock, so the replacement
                    # still waits behind this turn's own process_entry
                    # releasing it normally on return (no double-release
                    # race between this and the caller's finally).
                    logger.info(
                        "agent_worker: turn for agent %s chat %s superseded mid-turn, ending turn "
                        "and re-firing the replacement immediately",
                        agent_id, chat_id,
                    )
                    await arm_debounce_now(agent_id, chat_id)
                    ended_status = "done"
                    return

                if api_key is None and not await _check_gemini_call_budget(agent_id):
                    # The per-minute call budget is a fixed window that resets
                    # within a minute on its own - a turn hitting it mid-flight
                    # is a transient stall, not a real failure, so back off and
                    # retry in place a few times before giving up (previously
                    # this ended the turn immediately with no retry and no
                    # notice - user-reported: messages in the owner's own
                    # agent chat went completely unanswered with no error
                    # shown anywhere). A `while` sub-loop rather than `continue`
                    # on the outer `for round_trip in range(...)` - `continue`
                    # would advance `round_trip` and consume one of its slots
                    # for a rate-limit backoff, an unrelated budget.
                    budget_ok = False
                    superseded_while_waiting = False
                    for _ in range(settings.AGENT_GEMINI_BUDGET_MAX_RETRIES):
                        logger.info(
                            "agent_worker: agent %s over Gemini call budget, retrying in %ss",
                            agent_id, settings.AGENT_GEMINI_BUDGET_RETRY_SECONDS,
                        )
                        await asyncio.sleep(settings.AGENT_GEMINI_BUDGET_RETRY_SECONDS)
                        # A newer message may have superseded this turn while
                        # it was asleep (ADR 00732) - break out to the normal
                        # supersede handling at the top of the loop instead of
                        # wasting further retries/a notice on a now-stale turn.
                        if chat_id is not None and await is_superseded(agent_id, chat_id):
                            await mark_superseded(agent_id, chat_id)
                            superseded_while_waiting = True
                            break
                        if api_key is not None or await _check_gemini_call_budget(agent_id):
                            budget_ok = True
                            break
                    if superseded_while_waiting:
                        continue
                    if not budget_ok:
                        logger.warning(
                            "agent_worker: agent %s still over Gemini call budget after %d retries, "
                            "ending turn",
                            agent_id, settings.AGENT_GEMINI_BUDGET_MAX_RETRIES,
                        )
                        await _post_config_reply(
                            session,
                            agent,
                            agent.owner_agent_chat_id,
                            "Your agent is handling a lot of requests right now. "
                            "Please try again in a moment.",
                        )
                        await session.commit()
                        ended_status = "done"
                        return

                # Re-derived every round-trip, not just once before the loop:
                # a config-mode handoff tool (transfer_to_builder/
                # transfer_to_help_building/transfer_to_help_general/
                # finish_building_agent, ADR 0049/0064) flips
                # agent.builder_state mid-turn, and without this the very next
                # Gemini call would still run under the OLD state's prompt and
                # (more importantly) its OLD, now-wrong tool_schemas - unable
                # to actually act as the new state. Refreshing here means a
                # handoff takes effect immediately within the same turn, so
                # e.g. Supervisor->Builder responds to the user's original
                # request ("I want an agent that sells iPhones") in the same
                # reply instead of a generic "handed off" line, then going
                # silent until the user's next message. ADR 0047 decisions
                # 3+4 are otherwise unchanged: tool set is still decided
                # purely by chat_id (+ builder_state for config mode), never
                # by active_skill/system_prompt/anything model-controlled.
                tool_schemas = get_tool_schemas_for_chat(agent, chat_id)
                if scoped_system_prompt:
                    # ADR 0061: a scoped one-off/ephemeral turn runs under its
                    # own short-lived prompt instead of the agent's persistent
                    # persona/system_prompt/builder_state prompt - deliberately
                    # bypasses all three so the task stays limited to exactly
                    # what it was told to do, regardless of the agent's normal
                    # configuration.
                    system_prompt = scoped_system_prompt
                elif is_config_mode(agent, chat_id):
                    persona_prompt = get_builder_state_prompt(BuilderState(agent.builder_state))
                    system_prompt = f"{persona_prompt}\n\n{agent.system_prompt}" if agent.system_prompt else persona_prompt
                    system_prompt += _pending_confirmation_note(agent)
                else:
                    persona_prompt = get_persona_system_prompt(agent.active_skill)
                    system_prompt = f"{persona_prompt}\n\n{agent.system_prompt}" if agent.system_prompt else persona_prompt

                # ADR 0059: token-usage tracking only, no gating (2026-09-26) -
                # the char-per-token estimate is too coarse to make send/skip
                # or output-size decisions from. Calls always run at the
                # fixed technical output ceiling; record_tokens still tracks
                # real usage after the fact for the /agents/me/usage display.
                # (Previously max_output_tokens was derived from estimated
                # remaining budget, which could clamp to as little as 1 token
                # on a stale/tight estimate - Gemini would then genuinely
                # truncate to nothing and report MAX_TOKENS, a self-inflicted
                # false "out of budget" that had nothing to do with real
                # capacity.) BYOK (api_key is not None) draws from the
                # owner's own Gemini quota, not this project's budget.
                max_output_tokens = settings.AGENT_MAX_OUTPUT_TOKENS_CEILING

                gemini_call_made = True
                try:
                    result = await _generate_turn_or_supersede(
                        agent_id,
                        chat_id,
                        system_prompt=system_prompt,
                        contents=contents,
                        tool_schemas=tool_schemas,
                        api_key=api_key,
                        max_output_tokens=max_output_tokens,
                    )
                except _TurnSuperseded:
                    # ADR 00732/0075: a newer message took over mid-call - the
                    # in-flight Gemini request was just cancelled. End
                    # cleanly, no owner-facing notice (unlike GeminiChatError
                    # below, this isn't a failure). Always case B (a call was
                    # already in flight) - re-fire the replacement
                    # immediately rather than waiting out another full
                    # debounce window.
                    logger.info(
                        "agent_worker: turn for agent %s chat %s superseded mid-call, ending turn "
                        "and re-firing the replacement immediately",
                        agent_id, chat_id,
                    )
                    # ADR 0077: no TurnResult/usageMetadata ever comes back
                    # from a cancelled call, so record_tokens never runs for
                    # it via the normal path below - charge a flat estimate
                    # instead, since real input+output tokens were plausibly
                    # still spent on Google's side (no real cancellation
                    # exists, per ADR 00732's Context).
                    await record_tokens(agent_id, settings.AGENT_SUPERSEDED_CALL_TOKEN_PENALTY)
                    await arm_debounce_now(agent_id, chat_id)
                    ended_status = "done"
                    return
                except GeminiChatError:
                    logger.exception("agent_worker: Gemini call failed for agent %s", agent_id)
                    # Same owner-facing UX as the outer asyncio.TimeoutError
                    # handler in process_entry (frontend error UX rule: never
                    # leave a turn silently dead with no notice) - a Gemini
                    # HTTP failure/timeout here is otherwise indistinguishable
                    # from the agent just not responding at all.
                    await _post_config_reply(
                        session,
                        agent,
                        agent.owner_agent_chat_id,
                        "This took a bit too long to process. Please try again in a moment.",
                    )
                    await session.commit()
                    return

                content = result.content
                if result.usage is not None:
                    await record_tokens(agent_id, result.usage.total_tokens)
                    # Notify as soon as a window is exhausted, regardless of
                    # which chat this call was serving or whether this
                    # particular call itself got truncated - a call that
                    # tips used>=limit but still finishes with a normal
                    # STOP (rather than MAX_TOKENS) previously left the
                    # owner with no notice at all (2026-09-26, user-
                    # reported: ran out mid-conversation with someone else
                    # and got nothing). Same once-per-exhaustion cooldown as
                    # the MAX_TOKENS path below, so this and that path never
                    # double-send for the same exhaustion event.
                    usage = await peek_usage(agent_id)
                    for window, window_usage in usage.items():
                        if window_usage.is_blocked:
                            await _notify_token_budget_exhausted(session, agent, window)

                if result.finish_reason == "MAX_TOKENS":
                    # ADR 0059: stop the turn immediately, no further
                    # round-trips - the response is necessarily truncated.
                    # Execution-mode turns (a real chat with someone else)
                    # never forward the partial text to that chat - only the
                    # owner is told, via the fixed notice, in their own agent
                    # chat. Config-mode turns are the owner's own
                    # conversation with their own agent, so the partial text
                    # itself is useful to show them (same _post_config_reply
                    # path a normal config-mode text reply already uses).
                    logger.info("agent_worker: agent %s hit MAX_TOKENS, ending turn", agent_id)
                    if chat_id is not None and is_config_mode(agent, chat_id):
                        partial_text = extract_text(content)
                        if partial_text:
                            await _post_config_reply(session, agent, chat_id, partial_text)
                            await session.commit()
                    # Notification for the exhausted window(s) already fired
                    # right above, from the same record_tokens check.
                    ended_status = "done"
                    return

                call = extract_function_call(content)
                if call is None:
                    text = extract_text(content)
                    logger.info(
                        "agent_worker: agent %s turn ended with text response: %r",
                        agent_id,
                        text,
                    )
                    # Config-mode personas (Supervisor/Builder/Help) have no
                    # send_message-shaped tool - their plain-text replies must
                    # be posted directly or the owner never sees them. Never
                    # done for execution mode: those personas are expected to
                    # use send_message/reply_message themselves, and chat_id
                    # is None on a schedule-fired turn anyway.
                    if chat_id is not None and is_config_mode(agent, chat_id):
                        await _post_config_reply(session, agent, chat_id, text)
                        await session.commit()
                    ended_status = "done"
                    return

                # Append Gemini's own returned content dict verbatim (not a
                # hand-rebuilt {name, args} part) - gemini-flash-latest is a
                # "thinking" model that attaches an opaque thoughtSignature to
                # functionCall parts, which it then requires echoed back on
                # the next call's contents; reconstructing the part from just
                # call["name"]/call["args"] silently drops it and Gemini 400s
                # on the following round-trip ("Function call is missing a
                # thought_signature in functionCall parts").
                content.setdefault("role", "model")
                contents.append(content)

                if round_trip == settings.AGENT_TURN_MAX_TOOL_ROUNDTRIPS:
                    # Budget exhausted - report this back so the model doesn't
                    # just silently stop, then end the turn regardless of what
                    # it replies (no more round-trips left).
                    contents.append(
                        {"role": "user", "parts": [function_response_part(call["name"], {"error": "tool round-trip limit reached for this turn"})]}
                    )
                    logger.info("agent_worker: agent %s hit the %d round-trip cap", agent_id, settings.AGENT_TURN_MAX_TOOL_ROUNDTRIPS)
                    # Same owner-facing UX as the MAX_TOKENS/Gemini-error paths
                    # above (frontend error UX rule: never leave a turn
                    # silently dead with no notice) - always into the owner's
                    # own agent chat, never into whatever third-party chat an
                    # execution-mode turn was actually serving (found
                    # 2026-09-26: a turn hitting this cap previously ended
                    # with zero message anywhere).
                    await _post_config_reply(
                        session,
                        agent,
                        agent.owner_agent_chat_id,
                        _ROUND_TRIP_CAP_NOTICE,
                    )
                    await session.commit()
                    ended_status = "done"
                    return

                # ADR 00732/0075: last checkpoint before anything actually
                # leaves the process - closes the race where the flag was
                # set while this round-trip's Gemini call was already in
                # flight (caught on return, not before the call was made)
                # and the model came back wanting to call send_message/
                # reply_message. Every other tool has no externally-visible
                # side effect worth gating on this same check. Always case B
                # (a call has necessarily already happened to reach a
                # function-call result) - discard and re-fire the
                # replacement immediately, same as the top-of-loop mid-turn
                # branch.
                if (
                    chat_id is not None
                    and call["name"] in _MESSAGE_SENDING_TOOL_NAMES
                    and await is_superseded(agent_id, chat_id)
                ):
                    logger.info(
                        "agent_worker: turn for agent %s chat %s superseded just before %s, ending turn "
                        "and re-firing the replacement immediately",
                        agent_id, chat_id, call["name"],
                    )
                    await arm_debounce_now(agent_id, chat_id)
                    ended_status = "done"
                    return

                if config_mode_turn:
                    await _publish_agent_thinking(
                        owner_user_id, "tool_call", _TOOL_THINKING_LABELS.get(call["name"], "Working…")
                    )
                tool_result = await execute_tool_call(session, agent, call["name"], call["args"], chat_id=chat_id)
                await session.commit()
                if call["name"] == "no_reply_needed":
                    # ADR 0065: the model explicitly chose to end this
                    # config-mode turn without posting anything - stop right
                    # here instead of looping for another round-trip or
                    # falling through to the call-is-None/_post_config_reply
                    # path (which posts unconditionally).
                    ended_status = "done"
                    return
                # The reply just landed (new_message clears the indicator
                # client-side) - stop the loop right here instead of waiting
                # for the turn's finally, or a tick still in flight can
                # re-publish `typing` after the message already arrived and
                # leave a phantom indicator with nothing left to clear it
                # (the turn may keep going for more round-trips after this).
                if call["name"] in _MESSAGE_SENDING_TOOL_NAMES and peer_typing_task is not None:
                    peer_typing_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await peer_typing_task
                    peer_typing_task = None
                contents.append({"role": "user", "parts": [function_response_part(call["name"], tool_result)]})
            else:
                # Loop exhausted without an explicit return (shouldn't happen
                # given the round_trip cap branch above always returns on the
                # last iteration, but keep this from silently reading as an
                # error status if control ever reaches here).
                ended_status = "done"
        finally:
            if peer_typing_task is not None:
                peer_typing_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await peer_typing_task
            if owns_typing_indicator:
                # ADR 0075: only the owning turn tears the marker down - if
                # this turn was mid-turn superseded, the replacement it
                # triggers (via arm_debounce_now) claims a fresh marker of
                # its own rather than inheriting this one, so releasing here
                # is always safe (never races a still-running replacement's
                # ownership).
                await release_typing_indicator(chat_id)
            if config_mode_turn:
                await _publish_agent_thinking(owner_user_id, ended_status)


class AgentInvokeConsumer(BaseStreamConsumer):
    name = "agent-invoke-worker"
    group = settings.AGENT_INVOKE_STREAM_GROUP
    shard_count = 1  # single stream, not chat_id-sharded - see config/agent_settings.py
    default_batch = settings.AGENT_INVOKE_STREAM_BATCH
    block_ms = settings.AGENT_INVOKE_STREAM_BLOCK_MS
    claim_idle_ms = settings.AGENT_INVOKE_STREAM_CLAIM_IDLE_MS

    def __init__(self, consumer_name: str, semaphore: asyncio.Semaphore):
        self.consumer_name = consumer_name
        self._semaphore = semaphore

    async def ensure_group(self) -> None:
        try:
            await redis_client.xgroup_create(
                settings.AGENT_INVOKE_STREAM_KEY, self.group, id="0", mkstream=True
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    def stream_keys(self) -> list:
        return [settings.AGENT_INVOKE_STREAM_KEY]

    def stream_key_for_shard(self, shard: int) -> str:
        return settings.AGENT_INVOKE_STREAM_KEY

    async def process_entry(self, session: AsyncSession, fields: dict) -> None:
        agent_id = int(fields["agent_id"])
        kind = fields.get("kind", "message")

        async with self._semaphore:
            agent = await get_agent_by_id(session, agent_id)
            if agent is None or not agent.is_enabled:
                logger.info("agent_worker: skipping disabled/missing agent %s", agent_id)
                return

            if not await has_budget_remaining(agent_id):
                logger.info("agent_worker: agent %s over daily time budget, skipping", agent_id)
                await _notify_daily_budget_exhausted(session, agent)
                return

            if kind == "schedule":
                # ADR 0046 decision 3: re-load the live entry (defense in
                # depth for the enqueue-to-dequeue window, same pattern as
                # the is_enabled re-check above) - an edited/removed/
                # disabled entry since the poller enqueued it is a no-op.
                schedule_id = fields["schedule_id"]
                entry = next(
                    (e for e in agent.triggers.get("on_schedule", []) if e.get("id") == schedule_id),
                    None,
                )
                if entry is None or not entry.get("enabled", True):
                    logger.info("agent_worker: schedule entry %s gone/disabled, skipping", schedule_id)
                    return
                chat_id = int(entry["chat_id"]) if entry.get("chat_id") else None
                coro = _run_turn(
                    agent_id,
                    chat_id,
                    schedule_instruction=entry["instruction"],
                    scoped_system_prompt=entry.get("scoped_system_prompt"),
                )
                log_target = f"schedule {schedule_id}"
            else:
                chat_id = int(fields["chat_id"])
                message_id = int(fields["message_id"])
                coro = _run_turn(agent_id, chat_id, message_id)
                log_target = f"chat {chat_id}"

            # Per-(agent_id, chat_id) turn mutex (ADR 0063): a debounced fire
            # landing while a previous turn for the same pair is still
            # running (up to AGENT_TURN_TIMEOUT_SECONDS) must not start a
            # second concurrent turn - re-arm the debounce timer instead of
            # dropping the message, so it retries right after the current
            # turn finishes. chat_id=None (schedule-fired, no chat target)
            # never contends with anything.
            if not await acquire_turn_lock(agent_id, chat_id):
                logger.info(
                    "agent_worker: turn already running for agent %s %s, marking superseded "
                    "and re-arming debounce",
                    agent_id, log_target,
                )
                if chat_id is not None:
                    # ADR 00732: the in-flight turn for this pair checks this
                    # flag and ends without delivering its (now-stale) reply,
                    # instead of letting it reach the chat before the new
                    # message gets its own turn.
                    await mark_superseded(agent_id, chat_id)
                    await arm_debounce(agent_id, chat_id)
                return

            started = time.monotonic()
            try:
                await asyncio.wait_for(coro, timeout=settings.AGENT_TURN_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                logger.warning(
                    "agent_worker: turn for agent %s %s timed out after %ss",
                    agent_id, log_target, settings.AGENT_TURN_TIMEOUT_SECONDS,
                )
                # Generic, non-technical notice to the owner - always posted to
                # their dedicated agent chat regardless of which chat/schedule
                # entry triggered the turn (frontend error UX rule: no raw
                # timeout/technical detail surfaced to the user).
                await _post_config_reply(
                    session,
                    agent,
                    agent.owner_agent_chat_id,
                    "This took a bit too long to process. Please try again in a moment.",
                )
                await session.commit()
            finally:
                await record_active_seconds(agent_id, time.monotonic() - started)
                await release_turn_lock(agent_id, chat_id)


async def _fire_schedule_entry(session: AsyncSession, agent_id: int, schedule_id: str) -> None:
    """One due ZSET member: re-load the live Agent row (defense in depth,
    same pattern as process_entry's dequeue-time re-check), enqueue the turn
    onto agent_invoke_stream, then either reschedule (recurring) or flip
    enabled=false and drop the ZSET member (once) - ADR 0046 decision 3."""
    agent = await get_agent_by_id(session, agent_id)
    if agent is None or not agent.is_enabled:
        await remove_due_member(agent_id, schedule_id)
        return

    entries = agent.triggers.get("on_schedule", []) or []
    entry = next((e for e in entries if e.get("id") == schedule_id), None)
    if entry is None or not entry.get("enabled", True):
        await remove_due_member(agent_id, schedule_id)
        return

    await enqueue_schedule_fire(agent_id=agent_id, schedule_id=schedule_id)

    if entry.get("kind") == "recurring" and entry.get("time"):
        await reschedule_recurring(agent_id, schedule_id, entry["time"])
    else:
        # "once": mark fired (visible in the UI, not silently gone) and drop
        # the ZSET member - one small DB write, same as any other trigger
        # config change. Builds a fresh entry dict rather than mutating
        # `entry` in place before reassigning agent.triggers - `entries` is
        # the same list object already referenced from agent.triggers, so an
        # in-place mutation followed by `agent.triggers = {...}` makes the
        # "old" and "new" JSONB values compare equal (both already reflect
        # the mutation), and SQLAlchemy's plain `==` change detection on the
        # JSONB column then skips the UPDATE entirely - the enabled=false
        # flip silently never reaches Postgres.
        updated_entries = [
            {**e, "enabled": False} if e.get("id") == schedule_id else e for e in entries
        ]
        agent.triggers = {**agent.triggers, "on_schedule": updated_entries}
        await session.flush()
        await remove_due_member(agent_id, schedule_id)


async def _sweep_expired_ephemeral_tasks() -> None:
    """ADR 0061: closes out on_ephemeral_task entries whose expires_at
    lapsed with nobody having replied - the one completion path with no
    inbound message to piggyback on (every other completion happens inline
    in the Trigger Rule Engine as soon as a reply lands). Runs on the same
    cadence as the schedule poll loop rather than a separate timer - one
    more cheap check alongside it, not a new worker."""
    async with session_scope() as session:
        agents = await get_agents_with_ephemeral_tasks(session)
        for agent in agents:
            for task_id in sweep_expired_task_ids(agent):
                entry = agent.triggers.get("on_ephemeral_task", {}).get(task_id)
                if entry is None:
                    continue
                await fire_summary_and_complete(session, agent, task_id, entry)
        await session.commit()


async def _invoke_debounce_poll_loop(stop_event: asyncio.Event) -> None:
    """Tight poll (ADR 0063) alongside the schedule poll loop below - pops
    every (agent_id, chat_id) pair whose debounce window has elapsed and
    enqueues one agent_invoke_stream entry per pair, using the message_id
    stashed by the most recent arm_debounce call for that pair (the latest
    message in whatever burst got coalesced). A pair with no stashed
    message_id (arm_debounce failed, or the key already expired) is skipped
    - nothing to seed a turn from."""
    while not stop_event.is_set():
        try:
            for agent_id, chat_id in await due_pairs():
                message_id = await pop_latest_message_id(agent_id, chat_id)
                if message_id is None:
                    continue
                await enqueue_invocation(agent_id=agent_id, chat_id=chat_id, message_id=message_id)
        except Exception:
            logger.exception("agent_worker: invoke debounce poll iteration failed")

        try:
            await asyncio.wait_for(
                stop_event.wait(), timeout=settings.AGENT_INVOKE_DEBOUNCE_POLL_INTERVAL_SECONDS
            )
        except asyncio.TimeoutError:
            pass


async def _schedule_poll_loop(stop_event: asyncio.Event) -> None:
    """Lightweight loop inside this same agent_worker process (ADR 0046
    decision 3) - checks agent_schedule_due every
    AGENT_SCHEDULE_POLL_INTERVAL_SECONDS and fires whatever's due. Runs
    alongside the stream consumer, not as a replacement for it. Also sweeps
    expired ephemeral tasks (ADR 0061) on the same tick."""
    while not stop_event.is_set():
        try:
            members = await due_members()
            for member in members:
                agent_id_str, schedule_id = member.split(":", 1)
                async with session_scope() as session:
                    await _fire_schedule_entry(session, int(agent_id_str), schedule_id)
        except Exception:
            logger.exception("agent_worker: schedule poll iteration failed")

        try:
            await _sweep_expired_ephemeral_tasks()
        except Exception:
            logger.exception("agent_worker: ephemeral task sweep failed")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.AGENT_SCHEDULE_POLL_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def run_forever(stop_event: asyncio.Event | None = None) -> None:
    from config import SERVER_ID

    stop_event = stop_event or asyncio.Event()
    semaphore = asyncio.Semaphore(settings.AGENT_WORKER_CONCURRENCY)
    consumer = AgentInvokeConsumer(f"agent-worker-{SERVER_ID}", semaphore)

    await asyncio.gather(
        consumer.run_forever(stop_event),
        _schedule_poll_loop(stop_event),
        _invoke_debounce_poll_loop(stop_event),
    )
