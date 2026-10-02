"""`_run_turn` round-trip sub-steps (split out of invoke_worker.py by ADR 0100):
supersede handling, Gemini call budget wait, system-prompt selection, token
accounting and the three turn-ending branches. Called by invoke_turn_loop.py;
every `finish_*` helper ends the turn (the caller returns True)."""
import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from modules.agents.builder_flow import BuilderState, get_builder_state_prompt
from modules.agents.gemini_client import extract_text, function_response_part
from modules.agents.goal_tasks import build_goal_prompt, record_turn_outcome
from modules.agents.invoke_debounce import arm_debounce_now, is_superseded, mark_superseded
from modules.agents.invoke_notify import _ROUND_TRIP_CAP_NOTICE, _notify_token_budget_exhausted
from modules.agents.invoke_turn_ctx import TurnCtx
from modules.agents.invoke_turn_helpers import (
    _build_initial_contents,
    _check_gemini_call_budget,
    _pending_confirmation_note,
    _post_config_reply,
)
from modules.agents.outcome_judge import evaluate_tool_outcome, notify_outcome_mismatch
from modules.agents.personas import get_persona_system_prompt
from modules.agents.token_budget import peek_usage, record_tokens
from modules.agents.tools import is_config_mode

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


async def handle_pre_call_supersede(ctx: TurnCtx) -> str | None:
    """ADR 00732/0075: a new message matched a trigger for this same
    (agent_id, chat_id) while this turn was still running. Returns None (not
    superseded), "merged" (keep running this turn with fresh history) or
    "ended" (turn over, replacement re-fired)."""
    agent_id, chat_id = ctx.agent_id, ctx.chat_id
    if not (chat_id is not None and await is_superseded(agent_id, chat_id)):
        return None
    if not ctx.gemini_call_made and ctx.message_id is not None and ctx.schedule_instruction is None:
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
        ctx.contents = await _build_initial_contents(ctx.session, ctx.agent, chat_id)
        return "merged"
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
    ctx.ended_status = "done"
    return "ended"


async def wait_for_gemini_budget(ctx: TurnCtx) -> str:
    """Returns "ok" (budget available / freed), "superseded" (a newer message
    took over while waiting - caller goes back to the top-of-loop supersede
    handling) or "exhausted" (notice posted, turn over).

    The per-minute call budget is a fixed window that resets
    within a minute on its own - a turn hitting it mid-flight
    is a transient stall, not a real failure, so back off and
    retry in place a few times before giving up (previously
    this ended the turn immediately with no retry and no
    notice - user-reported: messages in the owner's own
    agent chat went completely unanswered with no error
    shown anywhere). A sub-loop rather than `continue` on the
    outer round-trip loop - `continue` would consume one of
    its slots for a rate-limit backoff, an unrelated budget."""
    agent_id, chat_id = ctx.agent_id, ctx.chat_id
    if await _check_gemini_call_budget(agent_id):
        return "ok"
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
            return "superseded"
        if await _check_gemini_call_budget(agent_id):
            return "ok"
    logger.warning(
        "agent_worker: agent %s still over Gemini call budget after %d retries, "
        "ending turn",
        agent_id, settings.AGENT_GEMINI_BUDGET_MAX_RETRIES,
    )
    await _post_config_reply(
        ctx.session,
        ctx.agent,
        ctx.agent.owner_agent_chat_id,
        "Your agent is handling a lot of requests right now. "
        "Please try again in a moment.",
    )
    await ctx.session.commit()
    ctx.ended_status = "done"
    return "exhausted"


def build_system_prompt(ctx: TurnCtx) -> str:
    """Re-derived every round-trip, not just once before the loop:
    agent.builder_state is decided once by
    owner_chat_router.py::route_owner_turn before the loop
    starts (ADR 0093 Phase 3) and no tool changes it mid-turn
    anymore, but this still reads it fresh each round-trip
    rather than caching a local copy, matching the same
    "chat_id + current DB state decide the tool set" discipline
    ADR 0047 decisions 3+4 established - tool set is decided
    purely by chat_id (+ builder_state for config mode), never
    by active_skill/system_prompt/anything model-controlled."""
    agent = ctx.agent
    if ctx.goal_turn is not None:
        # ADR 0099: goal-task turns run under a per-turn goal prompt
        # (goal / done_when / turns left) - never the persona.
        return build_goal_prompt(ctx.goal_turn.entry)
    if ctx.scoped_system_prompt:
        # ADR 0061: a scoped one-off/ephemeral turn runs under its
        # own short-lived prompt instead of the agent's persistent
        # persona/system_prompt/builder_state prompt - deliberately
        # bypasses all three so the task stays limited to exactly
        # what it was told to do, regardless of the agent's normal
        # configuration.
        return ctx.scoped_system_prompt
    if is_config_mode(agent, ctx.chat_id):
        persona_prompt = get_builder_state_prompt(BuilderState(agent.builder_state))
        system_prompt = f"{persona_prompt}\n\n{agent.system_prompt}" if agent.system_prompt else persona_prompt
        return system_prompt + _pending_confirmation_note(agent)
    persona_prompt = get_persona_system_prompt(agent.active_skill, agent)
    return f"{persona_prompt}\n\n{agent.system_prompt}" if agent.system_prompt else persona_prompt


async def record_usage(ctx: TurnCtx, result) -> None:
    """Notify as soon as a window is exhausted, regardless of
    which chat this call was serving or whether this
    particular call itself got truncated - a call that
    tips used>=limit but still finishes with a normal
    STOP (rather than MAX_TOKENS) previously left the
    owner with no notice at all (2026-09-26, user-
    reported: ran out mid-conversation with someone else
    and got nothing). Same once-per-exhaustion cooldown as
    the MAX_TOKENS path, so this and that path never
    double-send for the same exhaustion event."""
    if result.usage is None:
        return
    await record_tokens(ctx.agent_id, result.usage.total_tokens)
    usage = await peek_usage(ctx.agent_id)
    for window, window_usage in usage.items():
        if window_usage.is_blocked:
            await _notify_token_budget_exhausted(ctx.session, ctx.agent, window)


CONTINUATION_PROMPT = (
    "[system] Your previous message was cut off by the output length limit and was "
    "already delivered as-is. Continue exactly where it stopped, without repeating "
    "anything already sent and without any preamble."
)


async def finish_max_tokens(ctx: TurnCtx, content: dict) -> bool:
    """ADR 0059: stop the turn immediately, no further
    round-trips - the response is necessarily truncated.
    Execution-mode turns (a real chat with someone else)
    never forward the partial text to that chat - only the
    owner is told, via the fixed notice, in their own agent
    chat. Config-mode turns are the owner's own
    conversation with their own agent, so the partial text
    itself is useful to show them (same _post_config_reply
    path a normal config-mode text reply already uses).

    ADR 0102: a config-mode partial is followed by a continuation
    request (up to AGENT_MAX_CONTINUATION_MESSAGES, shared with the
    `continue_message` counter) - returns True when the caller should
    keep the turn going instead of ending it."""
    logger.info("agent_worker: agent %s hit MAX_TOKENS, ending turn", ctx.agent_id)
    if ctx.chat_id is not None and is_config_mode(ctx.agent, ctx.chat_id):
        partial_text = extract_text(content)
        if partial_text:
            await _post_config_reply(ctx.session, ctx.agent, ctx.chat_id, partial_text)
            await ctx.session.commit()
            if ctx.continuations_used < settings.AGENT_MAX_CONTINUATION_MESSAGES:
                ctx.continuations_used += 1
                ctx.contents.append({"role": "model", "parts": [{"text": partial_text}]})
                ctx.contents.append({"role": "user", "parts": [{"text": CONTINUATION_PROMPT}]})
                return True
    # Notification for the exhausted window(s) already fired
    # from record_usage's same record_tokens check.
    ctx.ended_status = "done"
    return False


async def _record_goal_outcome(ctx: TurnCtx) -> None:
    if ctx.goal_turn is not None:
        await record_turn_outcome(ctx.session, ctx.agent, ctx.goal_turn.task_id, acted=ctx.goal_acted)
        await ctx.session.commit()


async def finish_without_call(ctx: TurnCtx, content: dict) -> None:
    """The model returned plain text (no function call) - the turn's natural end."""
    agent_id, chat_id, session, agent = ctx.agent_id, ctx.chat_id, ctx.session, ctx.agent
    text = extract_text(content)
    logger.info("agent_worker: agent %s turn ended with text response: %r", agent_id, text)
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
                session, agent, chat_id,
                "Sorry, something went wrong on my end. Could you say that again?",
            )
        else:
            await _post_config_reply(session, agent, chat_id, text)
        await session.commit()
    # ADR 0099: a goal turn ending without a terminal call -
    # enforces the idle-turn and last-turn caps.
    await _record_goal_outcome(ctx)
    await _check_outcome_mismatch(
        session, agent, chat_id,
        goal_text=ctx.goal_text, tool_name=ctx.last_tool_name, tool_error=ctx.last_tool_error,
    )
    ctx.ended_status = "done"


async def finish_at_round_trip_cap(ctx: TurnCtx, call: dict) -> None:
    """Budget exhausted - report this back so the model doesn't just silently
    stop, then end the turn regardless of what it replies (no more
    round-trips left). The caller has already appended the model's content."""
    ctx.contents.append(
        {"role": "user", "parts": [function_response_part(call["name"], {"error": "tool round-trip limit reached for this turn"})]}
    )
    logger.info(
        "agent_worker: agent %s hit the %d round-trip cap",
        ctx.agent_id, settings.AGENT_TURN_MAX_TOOL_ROUNDTRIPS,
    )
    # Same owner-facing UX as the MAX_TOKENS/Gemini-error paths
    # (frontend error UX rule: never leave a turn
    # silently dead with no notice) - always into the owner's
    # own agent chat, never into whatever third-party chat an
    # execution-mode turn was actually serving (found
    # 2026-09-26: a turn hitting this cap previously ended
    # with zero message anywhere).
    await _post_config_reply(ctx.session, ctx.agent, ctx.agent.owner_agent_chat_id, _ROUND_TRIP_CAP_NOTICE)
    await ctx.session.commit()
    await _record_goal_outcome(ctx)
    # ADR 0096: the cap itself is the failure here - the call
    # that hit it never got to run, regardless of whether the
    # last *dispatched* tool succeeded.
    await _check_outcome_mismatch(
        ctx.session, ctx.agent, ctx.chat_id,
        goal_text=ctx.goal_text, tool_name=call["name"],
        tool_error="tool round-trip limit reached for this turn",
    )
    ctx.ended_status = "done"
