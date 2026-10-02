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

from redis.exceptions import ResponseError
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from infra.db.connection import session_scope
from infra.redis.client import redis_client
from modules.agents.builder_flow import BuilderState, get_builder_state_prompt
from modules.agents.clarify import generate_clarify_question
from modules.agents.crud import get_agent_by_id, get_agents_with_ephemeral_tasks, update_agent_config
from modules.agents.ephemeral_tasks import fire_summary_and_complete, sweep_expired_task_ids
from modules.agents.goal_tasks import (
    CONVERSE_MODE,
    TERMINAL_TOOL_NAMES,
    begin_goal_turn,
    build_goal_prompt,
    close_goal_task,
    find_goal_task_for_chat,
    record_turn_outcome,
)
from modules.agents.invoke_debounce import (
    acquire_turn_lock,
    arm_debounce,
    arm_debounce_now,
    claim_typing_indicator,
    due_pairs,
    is_superseded,
    mark_superseded,
    pop_latest_message_id,
    release_turn_lock,
    release_typing_indicator,
)
from modules.agents.invoke_notify import (
    _MESSAGE_SENDING_TOOL_NAMES,
    _ROUND_TRIP_CAP_NOTICE,
    _notify_daily_budget_exhausted,
    _notify_token_budget_exhausted,
    _publish_agent_thinking,
    _publish_peer_typing_loop,
)
from modules.agents.invoke_queue import enqueue_invocation, enqueue_schedule_fire
from modules.agents.gemini_client import (
    GeminiChatError,
    extract_function_call,
    extract_text,
    function_response_part,
)
from modules.agents.invoke_turn_helpers import (
    _ROUTED_STATE_THINKING_LABELS,
    _TOOL_THINKING_LABELS,
    _TurnSuperseded,
    _build_initial_contents,
    _build_knowledge_contents,
    _build_schedule_contents,
    _check_gemini_call_budget,
    _format_history_transcript,
    _generate_turn_or_supersede,
    _pending_confirmation_note,
    _post_config_reply,
)
from modules.agents.judge import evaluate_message, local_redirect_text
from modules.agents.outcome_judge import evaluate_tool_outcome, notify_outcome_mismatch
from modules.agents.owner_chat_router import route_owner_turn
from modules.agents.tools.common import escalate_chat
from modules.agents.personas import get_persona_system_prompt
from modules.agents.schedule import due_members, remove_due_member, reschedule_recurring
from modules.agents.time_budget import has_budget_remaining, record_active_seconds
from modules.agents.token_budget import peek_usage, record_tokens
from modules.agents.tools import execute_tool_call, get_tool_schemas_for_chat, is_config_mode
from modules.messaging import service as message_service
from modules.messaging.common import AGENT_REPLY_MESSAGE_TYPE
from modules.messaging.crud import get_message_by_id
from modules.messaging.read_api import get_message_history
from realtime.fanout.base_worker import BaseStreamConsumer, touch_app_liveness

logger = logging.getLogger(__name__)


async def _check_outcome_mismatch(
    session: AsyncSession,
    agent,
    chat_id: int | None,
    *,
    goal_text: str,
    tool_name: str | None,
    tool_error: str | None,
) -> None:
    """ADR 0096: runs only at a turn's actual end points, only when the turn
    is ending right on top of an unresolved tool failure. No-ops when there
    is nothing to check (no failure, or no chat to notify into - a
    schedule-fired turn with chat_id=None has no owner_agent_chat_id
    concept distinct from chat_id itself, but agent.owner_agent_chat_id is
    always set regardless, so this only needs tool_name/tool_error to be
    present). Never raises - a broken notify path must not crash the turn
    that already successfully ended."""
    if tool_name is None or tool_error is None:
        return
    try:
        verdict = await evaluate_tool_outcome(
            session, agent, chat_id if chat_id is not None else agent.owner_agent_chat_id,
            goal_text=goal_text, tool_name=tool_name, tool_error=tool_error,
        )
        await session.commit()
        if verdict.is_mismatch:
            await notify_outcome_mismatch(
                session, agent, goal_text=goal_text, tool_name=tool_name, tool_error=tool_error,
            )
            await session.commit()
    except Exception:
        logger.exception(
            "agent_worker: outcome-mismatch check failed for agent %s chat %s tool %s",
            agent.id, chat_id, tool_name,
        )


def _format_router_recent_turns(history, exclude_message_id: int) -> list[str]:
    """Renders an owner-agent chat history window into the recent_turns list
    route_owner_turn expects - reuses _format_history_transcript's per-line
    'Agent: .../Customer: ...' rendering (ADR 0093's router needs the same
    short, cheap context, not a second formatting convention), dropping the
    triggering message itself (passed to route_owner_turn separately as
    new_message) and returning one transcript line per list entry rather
    than a single joined block, since RouterDecision's recent_turns is
    already a list."""
    filtered = [m for m in history if m.id != exclude_message_id]
    transcript = _format_history_transcript(filtered)
    return transcript.split("\n") if transcript else []


async def _run_turn(
    agent_id: int,
    chat_id: int | None,
    message_id: int | None = None,
    schedule_instruction: str | None = None,
    knowledge_instruction: str | None = None,
    scoped_system_prompt: str | None = None,
) -> None:
    """Runs one Gemini + tool-calling turn for `agent_id`. Message-fired
    (`message_id` set) seeds from chat history; schedule-fired
    (`schedule_instruction` set, ADR 0046 decision 3) seeds from the entry's
    free-text instruction, optionally joined with `chat_id` history;
    knowledge-notice-fired (`knowledge_instruction` set, ADR 0085) seeds from
    a fully-formed instruction with no chat history - always config-mode
    (chat_id is always agent.owner_agent_chat_id). Opens
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
                # ADR 0080: mark the triggering message read as soon as the
                # agent starts processing it, mirroring a human reading the
                # chat before replying. Unconditional - ADR 0003 privacy
                # (owner's own privacy.read_receipts, 1:1-only) is already
                # enforced downstream by the receipt_log worker.
                await message_service.mark_as_read(session, agent.owner_user_id, chat_id, message_id)
                verdict = await evaluate_message(
                    session, agent, chat_id, message_id, message.content if message else None, message,
                    goal_task_active=find_goal_task_for_chat(agent.triggers, chat_id) is not None,
                )
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
                    # ADR 0088: a media-only message (photo/video/voice
                    # note/file) the agent can't view, still unanswered by
                    # the customer - the judge decided it plausibly needs a
                    # human to actually look at it.
                    elif verdict.needs_human_review:
                        logger.info(
                            "agent_worker: judge flagged unseeable media needing review, "
                            "agent %s chat %s message %s: %s",
                            agent_id, chat_id, message_id, verdict.reason,
                        )
                        await escalate_chat(
                            session, agent, chat_id, verdict.reason, notice_prefix="📎"
                        )
                        await session.commit()
                    ended_status = "done"
                    return

            # ADR 0093 Phase 3: the jev router is the sole way builder_state
            # changes now - runs once per config-mode owner-chat turn (a real
            # message, not a schedule/knowledge-notice turn, which have no
            # owner utterance to classify and keep whatever builder_state is
            # already set), before deciding CLARIFY vs. the normal
            # round-trip loop below. Fail-frozen by construction:
            # route_owner_turn always returns *some* RouterDecision (the
            # agent's current builder_state, unchanged, on any internal
            # failure) - so this unconditionally persists whatever it
            # returns rather than branching on success/failure itself.
            if config_mode_turn and message_id is not None:
                router_message = await get_message_by_id(session, chat_id, message_id)
                history = await get_message_history(
                    session, agent.owner_user_id, chat_id, limit=settings.AGENT_ROUTER_CONTEXT_TURNS
                )
                recent_turns = _format_router_recent_turns(history, exclude_message_id=message_id)
                decision = await route_owner_turn(
                    session,
                    agent,
                    chat_id,
                    recent_turns,
                    router_message.content if router_message else "",
                )
                if decision.state != BuilderState(agent.builder_state):
                    agent = await update_agent_config(session, agent, {"builder_state": decision.state.value})
                await session.commit()
                await _publish_agent_thinking(
                    owner_user_id,
                    "routed",
                    _ROUTED_STATE_THINKING_LABELS.get(agent.builder_state, "Thinking…"),
                )

            # ADR 0093: BuilderState.CLARIFY is a deliberate exception to the
            # normal per-round-trip Gemini turn every other builder_state
            # uses - a dedicated minimal call (no chat history, no tool
            # schemas, its own cheap model tier), mirroring judge.py's
            # redirect-text call.
            if config_mode_turn and message_id is not None and BuilderState(agent.builder_state) == BuilderState.CLARIFY:
                clarify_message = await get_message_by_id(session, chat_id, message_id)
                question = await generate_clarify_question(
                    agent, clarify_message.content if clarify_message else ""
                )
                await _post_config_reply(session, agent, chat_id, question)
                await session.commit()
                ended_status = "done"
                return

            if knowledge_instruction is not None:
                contents = await _build_knowledge_contents(session, agent, knowledge_instruction)
                goal_text = knowledge_instruction
            elif schedule_instruction is not None:
                contents = await _build_schedule_contents(session, agent, schedule_instruction, chat_id)
                goal_text = schedule_instruction
            else:
                contents = await _build_initial_contents(session, agent, chat_id)
                # ADR 0096: the outcome-mismatch judge needs the triggering
                # message's own text, not the full history transcript
                # _build_initial_contents seeds the turn with - re-fetched
                # here rather than threaded through that helper, since only
                # this one caller (the outcome check at the end of the turn)
                # needs it standalone.
                goal_text = ""
                if message_id is not None:
                    goal_message = await get_message_by_id(session, chat_id, message_id)
                    goal_text = (goal_message.content if goal_message else "") or ""

            # Last tool result the turn produced, tracked past the loop body
            # so the two turn-ending branches below can tell whether the
            # turn is ending right on top of an unresolved failure (ADR
            # 0096) - never inspected mid-loop, only at those two exit
            # points, so a failure a later round-trip recovers from never
            # reaches it.
            last_tool_error: str | None = None
            last_tool_name: str | None = None

            # ADR 0099: an active goal task targeting this chat - counts the
            # turn, and force-closes if its turn budget was already spent.
            goal_turn = None
            goal_acted = False
            if chat_id is not None and not config_mode_turn:
                goal_turn = await begin_goal_turn(session, agent, chat_id)
                if goal_turn is not None:
                    await session.commit()
                    if goal_turn.closed:
                        ended_status = "done"
                        return

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

                if not await _check_gemini_call_budget(agent_id):
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
                        if await _check_gemini_call_budget(agent_id):
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
                # agent.builder_state is now decided once by
                # owner_chat_router.py::route_owner_turn before this loop
                # starts (ADR 0093 Phase 3) and no tool changes it mid-turn
                # anymore, but this still reads it fresh each round-trip
                # rather than caching a local copy, matching the same
                # "chat_id + current DB state decide the tool set" discipline
                # ADR 0047 decisions 3+4 established - tool set is decided
                # purely by chat_id (+ builder_state for config mode), never
                # by active_skill/system_prompt/anything model-controlled.
                tool_schemas = get_tool_schemas_for_chat(agent, chat_id)
                if goal_turn is not None:
                    # ADR 0099: goal-task turns run under a per-turn goal prompt
                    # (goal / done_when / turns left) - never the persona.
                    system_prompt = build_goal_prompt(goal_turn.entry)
                elif scoped_system_prompt:
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
                    persona_prompt = get_persona_system_prompt(agent.active_skill, agent)
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
                # capacity.)
                max_output_tokens = settings.AGENT_MAX_OUTPUT_TOKENS_CEILING

                gemini_call_made = True
                try:
                    result = await _generate_turn_or_supersede(
                        agent_id,
                        chat_id,
                        system_prompt=system_prompt,
                        contents=contents,
                        tool_schemas=tool_schemas,
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
                        if not text:
                            # ADR 0089: a config-mode turn ending with no
                            # extractable text (e.g. a thought-signature-only
                            # response, or an unusual finish reason) used to
                            # silently no-op here (_post_config_reply returns
                            # early on empty text) - the owner saw
                            # agent_thinking "started" and then nothing ever
                            # arrived, indistinguishable from the agent simply
                            # giving up. Always leave a trace instead.
                            logger.warning(
                                "agent_worker: agent %s config-mode turn ended with empty text, "
                                "posting fallback notice instead of nothing",
                                agent_id,
                            )
                            await _post_config_reply(
                                session,
                                agent,
                                chat_id,
                                "Sorry, something went wrong on my end. Could you say that again?",
                            )
                        else:
                            await _post_config_reply(session, agent, chat_id, text)
                        await session.commit()
                    if goal_turn is not None:
                        # ADR 0099: a goal turn ending without a terminal call -
                        # enforces the idle-turn and last-turn caps.
                        await record_turn_outcome(session, agent, goal_turn.task_id, acted=goal_acted)
                        await session.commit()
                    await _check_outcome_mismatch(
                        session, agent, chat_id,
                        goal_text=goal_text, tool_name=last_tool_name, tool_error=last_tool_error,
                    )
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
                    if goal_turn is not None:
                        await record_turn_outcome(session, agent, goal_turn.task_id, acted=goal_acted)
                        await session.commit()
                    # ADR 0096: the cap itself is the failure here - the call
                    # that hit it never got to run, regardless of whether the
                    # last *dispatched* tool succeeded.
                    await _check_outcome_mismatch(
                        session, agent, chat_id,
                        goal_text=goal_text, tool_name=call["name"],
                        tool_error="tool round-trip limit reached for this turn",
                    )
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
                # ADR 0096: remember only the LAST tool outcome - a success
                # here clears any earlier failure, since the turn is no
                # longer "ending on top of" it. Checked only at the two
                # turn-ending branches below, never mid-loop.
                if isinstance(tool_result, dict) and "error" in tool_result:
                    last_tool_error = str(tool_result["error"])
                    last_tool_name = call["name"]
                else:
                    last_tool_error = None
                    last_tool_name = None
                if goal_turn is not None:
                    # ADR 0099: a successful terminal tool closed the task (and
                    # queued the owner summary) - the turn is over.
                    tool_ok = not (isinstance(tool_result, dict) and "error" in tool_result)
                    if tool_ok and call["name"] in TERMINAL_TOOL_NAMES:
                        ended_status = "done"
                        return
                    if tool_ok and call["name"] in _MESSAGE_SENDING_TOOL_NAMES:
                        goal_acted = True
                if call["name"] == "no_reply_needed":
                    # ADR 0065: the model explicitly chose to end this
                    # config-mode turn without posting anything - stop right
                    # here instead of looping for another round-trip or
                    # falling through to the call-is-None/_post_config_reply
                    # path (which posts unconditionally).
                    if knowledge_instruction is not None or schedule_instruction is not None:
                        # ADR 0089: a turn seeded to report an already-
                        # completed action (knowledge ingestion, schedule
                        # firing) has something the owner hasn't seen yet by
                        # construction - no_reply_needed here means that
                        # report was never delivered. The prompt (STYLE_RULES)
                        # already tells the model not to do this; log so a
                        # recurrence is visible instead of only surfacing as
                        # another silent-turn user report.
                        logger.warning(
                            "agent_worker: agent %s called no_reply_needed on a "
                            "knowledge/schedule-fired turn - an unreported outcome "
                            "may have been dropped",
                            agent_id,
                        )
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

    async def run_forever(self, stop_event: asyncio.Event | None = None) -> None:
        """ADR 0094: overrides BaseStreamConsumer.run_forever entirely - the
        default _run_shard loop `await`s drain_once (one xreadgroup batch,
        fully processed) before looping back to read again, so even a
        gather()-based concurrent _drain_shard override still can't pick up
        an entry that lands in Redis *after* the current batch was read but
        *before* the current batch finishes (found via direct testing: two
        messages ~7s apart landed in separate xreadgroup batches and the
        second never started until the first's full ~90s-capped turn ended -
        the read loop itself was the thing blocking, not the semaphore).

        Fix: decouple "read entries from the stream" from "wait for them to
        finish processing" entirely. One tight pump loop keeps calling
        xreadgroup/XAUTOCLAIM and fire-and-forget dispatches a task per
        entry; a separate bounded set of in-flight tasks (capped at
        AGENT_WORKER_CONCURRENCY via self._semaphore, acquired inside each
        task before it does any real work) is all that limits how many
        turns run at once - the pump itself never awaits a turn's own
        completion before reading the next batch."""
        try:
            await self.ensure_group()
        except Exception:
            logger.exception("%s: ensure_group failed, will retry", self.name)

        stop_event = stop_event or asyncio.Event()
        in_flight: set[asyncio.Task] = set()
        stream_key = settings.AGENT_INVOKE_STREAM_KEY

        async def _process_one(entry_id, fields: dict) -> None:
            async with self._semaphore:
                async with session_scope() as entry_session:
                    try:
                        await self.process_entry(entry_session, fields)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception(
                            "%s: entry %s failed transiently, will be reclaimed",
                            self.name, entry_id,
                        )
                        await entry_session.rollback()
                        return
            await self._ack_with_retry(stream_key, [entry_id])

        try:
            while not stop_event.is_set():
                try:
                    response = await redis_client.xreadgroup(
                        self.group,
                        self.consumer_name,
                        {stream_key: ">"},
                        count=self.default_batch,
                        block=self.block_ms or None,
                    )
                    entries: list = list(response[0][1]) if response else []

                    if len(entries) < self.default_batch:
                        entries.extend(
                            await self._claim_stale(stream_key, self.default_batch - len(entries))
                        )

                    for entry_id, fields in entries:
                        task = asyncio.create_task(_process_one(entry_id, fields))
                        in_flight.add(task)
                        task.add_done_callback(in_flight.discard)

                    await touch_app_liveness()
                    if not entries:
                        await asyncio.sleep(0)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("%s: drain iteration failed", self.name)
                    await asyncio.sleep(1.0)
        except asyncio.CancelledError:
            for task in in_flight:
                task.cancel()
            for task in list(in_flight):
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            raise

        if in_flight:
            await asyncio.gather(*in_flight, return_exceptions=True)

    async def process_entry(self, session: AsyncSession, fields: dict) -> None:
        # ADR 0094: concurrency is now bounded by _drain_shard's own
        # semaphore acquisition (one per dispatched entry, around this whole
        # call) rather than here - process_entry itself no longer touches
        # self._semaphore.
        agent_id = int(fields["agent_id"])
        kind = fields.get("kind", "message")

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
        elif kind == "knowledge":
            # ADR 0085: always targets the owner-agent chat - config-mode
            # by construction (is_config_mode), no chat history to seed
            # from, just the caller-built instruction string.
            chat_id = int(fields["chat_id"])
            coro = _run_turn(agent_id, chat_id, knowledge_instruction=fields["instruction"])
            log_target = f"knowledge notice, chat {chat_id}"
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
        except Exception:
            # ADR 0089: any other unhandled failure inside _run_turn used
            # to propagate to _drain_shard's catch-all, which only logs
            # and leaves the entry unacked for reclaim - completely
            # silent from the owner's side (agent_thinking flips to
            # "error" in _run_turn's own finally, but that pub/sub event
            # has no replay and the frontend currently renders "error"
            # identically to "done"). Post the same fixed, non-technical
            # notice the timeout path above already uses, then re-raise
            # so the entry is still left unacked for reclaim/retry
            # exactly as before - this only adds an owner-facing trace,
            # it does not change delivery/retry semantics.
            logger.exception(
                "agent_worker: turn for agent %s %s failed", agent_id, log_target
            )
            await _post_config_reply(
                session,
                agent,
                agent.owner_agent_chat_id,
                "Sorry, something went wrong on my end. Please try again in a moment.",
            )
            await session.commit()
            raise
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
                if entry.get("mode") == CONVERSE_MODE:
                    # ADR 0099: goal tasks close via the shared owner-notice path.
                    await close_goal_task(
                        session, agent, task_id, status="timed_out",
                        summary="No resolution before the deadline.",
                    )
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
