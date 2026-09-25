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
"""
import asyncio
import contextlib
import logging
import time
import uuid

from redis.exceptions import ResponseError
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from infra.db.connection import session_scope
from infra.ratelimit.service import check_and_increment
from infra.redis.client import redis_client
from modules.agents.builder_flow import BuilderState, get_builder_state_prompt
from modules.agents.crud import get_agent_by_id
from modules.agents.invoke_queue import enqueue_schedule_fire
from modules.agents.gemini_client import (
    GeminiChatError,
    extract_function_call,
    extract_text,
    function_response_part,
    generate_turn,
)
from modules.agents.judge import evaluate_message, local_redirect_text
from modules.agents.models import Agent
from modules.agents.personas import get_persona_system_prompt
from modules.agents.schedule import due_members, remove_due_member, reschedule_recurring
from modules.agents.time_budget import has_budget_remaining, record_active_seconds
from modules.agents.tools import execute_tool_call, get_tool_schemas_for_chat, is_config_mode
from modules.messaging import service as message_service
from modules.messaging.common import AGENT_REPLY_MESSAGE_TYPE
from modules.messaging.crud import get_message_by_id
from modules.messaging.read_api import get_message_history
from realtime import realtime_service
from realtime.fanout.base_worker import BaseStreamConsumer

logger = logging.getLogger(__name__)

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
    "transfer_to_builder": "Bringing in the builder…",
    "transfer_to_help": "Bringing in help…",
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


async def _publish_peer_typing_loop(chat_id: int, sender_id: int) -> None:
    """Real `typing` event fanned out to the chat's other participants (same
    `publish_event` a genuine user's WS `typing` frame goes through) - runs
    for the lifetime of an execution-mode turn targeting a real chat, so
    whoever the agent is about to message sees an ordinary "typing…"
    indicator instead of nothing, until the reply itself lands. Distinct from
    `_publish_agent_thinking`, which is a private, owner-only signal for the
    agent drawer and is never seen by other chat members."""
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
            await asyncio.sleep(_PEER_TYPING_REFRESH_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("agent_worker: failed to publish peer typing for chat %s", chat_id)


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
    AGENT_HISTORY_TRANSCRIPT_MAX_CHARS from the start (oldest lines dropped
    first) so the most recent context always survives a long/verbose chat."""
    lines = [
        f'{"Agent" if m.type == AGENT_REPLY_MESSAGE_TYPE else "Customer"}: {m.content}'
        for m in reversed(list(history))
        if m.content
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
) -> None:
    """Runs one Gemini + tool-calling turn for `agent_id`. Message-fired
    (`message_id` set) seeds from chat history; schedule-fired
    (`schedule_instruction` set, ADR 0046 decision 3) seeds from the entry's
    free-text instruction, optionally joined with `chat_id` history. Opens
    its own DB session (this consumer's caller session is per-batch and
    shouldn't be held across a slow Gemini call). Every failure is logged and
    swallowed - a bad turn must never crash the worker loop; the daily time
    budget still gets accounted by the caller's `finally`."""
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
        config_mode_turn = chat_id is None or is_config_mode(agent, chat_id)
        if config_mode_turn:
            await _publish_agent_thinking(owner_user_id, "started")
        # Real peer-visible "typing" indicator (not the owner-only
        # agent_thinking above) - only for execution-mode turns against an
        # actual chat with the agent, never the owner's own config-mode
        # drawer chat (that already gets agent_thinking) and never a
        # schedule-fired turn with chat_id=None.
        peer_typing_task: asyncio.Task | None = None
        if chat_id is not None and not is_config_mode(agent, chat_id):
            peer_typing_task = asyncio.create_task(_publish_peer_typing_loop(chat_id, owner_user_id))
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

            for round_trip in range(settings.AGENT_TURN_MAX_TOOL_ROUNDTRIPS + 1):
                if api_key is None and not await _check_gemini_call_budget(agent_id):
                    logger.info("agent_worker: agent %s over Gemini call budget, ending turn", agent_id)
                    return

                # Re-derived every round-trip, not just once before the loop:
                # a config-mode handoff tool (transfer_to_builder/
                # transfer_to_help/finish_building_agent, ADR 0049) flips
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
                if is_config_mode(agent, chat_id):
                    persona_prompt = get_builder_state_prompt(BuilderState(agent.builder_state))
                else:
                    persona_prompt = get_persona_system_prompt(agent.active_skill)
                system_prompt = f"{persona_prompt}\n\n{agent.system_prompt}" if agent.system_prompt else persona_prompt

                try:
                    content = await generate_turn(
                        system_prompt=system_prompt,
                        contents=contents,
                        tool_schemas=tool_schemas,
                        api_key=api_key,
                    )
                except GeminiChatError:
                    logger.exception("agent_worker: Gemini call failed for agent %s", agent_id)
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
                    ended_status = "done"
                    return

                if config_mode_turn:
                    await _publish_agent_thinking(
                        owner_user_id, "tool_call", _TOOL_THINKING_LABELS.get(call["name"], "Working…")
                    )
                tool_result = await execute_tool_call(session, agent, call["name"], call["args"], chat_id=chat_id)
                await session.commit()
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
                coro = _run_turn(agent_id, chat_id, schedule_instruction=entry["instruction"])
                log_target = f"schedule {schedule_id}"
            else:
                chat_id = int(fields["chat_id"])
                message_id = int(fields["message_id"])
                coro = _run_turn(agent_id, chat_id, message_id)
                log_target = f"chat {chat_id}"

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
        # config change.
        entry["enabled"] = False
        agent.triggers = {**agent.triggers, "on_schedule": entries}
        await session.flush()
        await remove_due_member(agent_id, schedule_id)


async def _schedule_poll_loop(stop_event: asyncio.Event) -> None:
    """Lightweight loop inside this same agent_worker process (ADR 0046
    decision 3) - checks agent_schedule_due every
    AGENT_SCHEDULE_POLL_INTERVAL_SECONDS and fires whatever's due. Runs
    alongside the stream consumer, not as a replacement for it."""
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
    )
