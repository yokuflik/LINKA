"""Helpers `_run_turn` (invoke_worker.py) calls to build its Gemini
conversation, gate its per-minute call budget, and post config-mode replies
(split out of invoke_worker.py by ADR 0082).
"""
import asyncio
import contextlib
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from infra.ratelimit.service import check_and_increment
from modules.agents.gemini_client import TurnResult, generate_turn
from modules.agents.invoke_debounce import is_superseded
from modules.agents.models import Agent
from modules.messaging import service as message_service
from modules.messaging.common import AGENT_REPLY_MESSAGE_TYPE
from modules.messaging.read_api import get_message_history

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
# Owner-chat router decision -> "thinking" label shown right after routing
# (keyed by BuilderState value; unknown states fall back to a generic label).
_ROUTED_STATE_THINKING_LABELS = {
    "one_off_action": "Routed to One-off action agent…",
    "clarify": "Routed to Clarification agent…",
    "builder_agent": "Routed to Agent builder…",
    "help_general": "Routed to General help agent…",
    "help_agent_building": "Routed to Agent-building help agent…",
}

_TOOL_THINKING_LABELS = {
    "send_message": "Sending a message…",
    "reply_message": "Sending a reply…",
    "create_chat": "Starting a new chat…",
    "leave_group": "Leaving a group…",
    "read_history": "Reading chat history…",
    "update_own_triggers": "Updating its own triggers…",
    "delete_own_trigger": "Removing a trigger…",
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
    "start_goal_task": "Starting a goal task…",
    "cancel_goal_task": "Cancelling a goal task…",
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


def _format_history_transcript(history) -> str | None:
    """Formats a message history window into a single 'Agent: ...' /
    'Customer: ...' transcript block (oldest first) - the explicit role
    label (rather than a raw sender_id) reads far better to Gemini than a
    numeric id, and matches how a human would paste a chat log. Truncated to
    AGENT_HISTORY_TRANSCRIPT_MAX_CHARS in whole messages (oldest dropped first,
    never cut mid-message); each message is also capped individually
    (head+tail kept) so one long message can't crowd out the rest.

    Customer/Owner lines followed later in the transcript by an Agent line
    are marked '[already handled]' (ADR 0071) - the agent would not have
    replied without having acted on them, so re-seeing them unmarked on a
    later, unrelated turn otherwise reads to Gemini as still-pending and can
    trigger a re-execution of whatever tool call handled them the first time.
    Only the trailing run of Customer/Owner lines with no Agent reply after
    them - the genuinely unanswered tail - is left unmarked.

    A media message (image/video/audio/file) is included even with no
    caption, appending '[attached file: <name>]' - filename only, so the
    model knows an attachment exists (and can be referenced later) without
    the bytes ever being fetched or analyzed."""
    ordered = [m for m in reversed(list(history)) if m.content or m.media_name]
    is_agent = [m.type == AGENT_REPLY_MESSAGE_TYPE for m in ordered]
    handled = [any(is_agent[i + 1:]) for i in range(len(ordered))]

    last = len(ordered) - 1

    def _clip(text: str, cap: int) -> str:
        if len(text) <= cap:
            return text
        keep_head = cap // 2
        keep_tail = cap - keep_head
        omitted = len(text) - cap
        return (
            f"{text[:keep_head]} …[truncated, {omitted} chars omitted; "
            f"full text via read_history]… {text[-keep_tail:]}"
        )

    def _line(i: int, m) -> str:
        parts = []
        if m.content:
            # The newest message, when it is the customer's, is the one the
            # agent must answer - it gets a higher per-message cap.
            cap = (
                settings.AGENT_HISTORY_LATEST_MESSAGE_MAX_CHARS
                if i == last and not is_agent[i]
                else settings.AGENT_HISTORY_MESSAGE_MAX_CHARS
            )
            parts.append(_clip(m.content, cap))
        if m.type in settings.MEDIA_MESSAGE_TYPES and m.media_name:
            parts.append(f"[attached file: {m.media_name}]")
        return " ".join(parts)

    lines = [
        f'{"Agent" if is_agent[i] else "Customer"}: {_line(i, m)}'
        + ("" if is_agent[i] or not handled[i] else " [already handled]")
        for i, m in enumerate(ordered)
    ]
    if not lines:
        return None
    # Whole-message budget: walk newest -> oldest and stop at a message
    # boundary, so a line is never cut mid-way (its role label and
    # [already handled] marker always survive). The newest line is always kept.
    budget = settings.AGENT_HISTORY_TRANSCRIPT_MAX_CHARS
    kept_from = len(lines) - 1
    used = len(lines[kept_from])
    for i in range(len(lines) - 2, -1, -1):
        used += len(lines[i]) + 1
        if used > budget:
            break
        kept_from = i
    transcript = "\n".join(lines[kept_from:])
    if kept_from > 0:
        transcript = "…(earlier messages truncated)…\n" + transcript
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


async def _build_knowledge_contents(session: AsyncSession, agent: Agent, instruction: str) -> list[dict]:
    """Seeds a knowledge-ingestion notice turn (ADR 0085) from a fully-formed
    free-text instruction built by the caller (router.py), joined with the
    owner-agent chat's own recent history (ADR 0089) - without it, the only
    language signal in the whole turn was the ingested document/failure
    reason text, which made the model reply in the document's language
    instead of the owner's own chat language."""
    history = await get_message_history(session, agent.owner_user_id, agent.owner_agent_chat_id, limit=20)
    transcript = _format_history_transcript(history)
    parts = [f"Knowledge base update: {instruction}"]
    if transcript:
        parts.append(f"Recent history in this chat (oldest first):\n{transcript}")
    parts.append("Tell the owner what happened, in your own words.")
    return [{"role": "user", "parts": [{"text": "\n\n".join(parts)}]}]


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
