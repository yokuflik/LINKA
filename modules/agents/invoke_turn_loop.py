"""`_run_turn`'s Gemini + tool-calling round-trip loop (split out of
invoke_worker.py by ADR 0100). `run_round_trip` is one loop iteration and
returns True when the turn is over; sub-steps live in invoke_turn_steps.py."""
import asyncio
import contextlib
import logging

from config import settings
from modules.agents.gemini_client import (
    GeminiChatError,
    extract_function_call,
    function_response_part,
)
from modules.agents.goal_tasks import TERMINAL_TOOL_NAMES
from modules.agents.invoke_debounce import arm_debounce_now, is_superseded
from modules.agents.invoke_notify import _MESSAGE_SENDING_TOOL_NAMES, _publish_agent_thinking
from modules.agents.invoke_turn_ctx import TurnCtx
from modules.agents.invoke_turn_helpers import (
    _TOOL_THINKING_LABELS,
    _TurnSuperseded,
    _generate_turn_or_supersede,
    _post_config_reply,
)
from modules.agents.invoke_turn_steps import (
    _record_goal_outcome,
    build_system_prompt,
    finish_at_round_trip_cap,
    finish_max_tokens,
    finish_without_call,
    handle_pre_call_supersede,
    record_usage,
    wait_for_gemini_budget,
)
from modules.agents.token_budget import record_tokens
from modules.agents.tools import execute_tool_call, get_tool_schemas_for_chat

logger = logging.getLogger(__name__)


async def run_round_trips(ctx: TurnCtx) -> None:
    for round_trip in range(settings.AGENT_TURN_MAX_TOOL_ROUNDTRIPS + 1):
        if await run_round_trip(ctx, round_trip):
            return
    # Loop exhausted without an explicit return (shouldn't happen
    # given the round_trip cap branch always returns on the
    # last iteration, but keep this from silently reading as an
    # error status if control ever reaches here).
    ctx.ended_status = "done"


async def run_round_trip(ctx: TurnCtx, round_trip: int) -> bool:
    """One Gemini call + (at most) one tool dispatch. True = turn finished."""
    agent_id, chat_id, session, agent = ctx.agent_id, ctx.chat_id, ctx.session, ctx.agent

    supersede = await handle_pre_call_supersede(ctx)
    if supersede == "ended":
        return True
    if supersede == "merged":
        return False

    budget = await wait_for_gemini_budget(ctx)
    if budget == "superseded":
        return False
    if budget == "exhausted":
        return True

    tool_schemas = get_tool_schemas_for_chat(agent, chat_id)
    system_prompt = build_system_prompt(ctx)

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

    ctx.gemini_call_made = True
    try:
        result = await _generate_turn_or_supersede(
            agent_id,
            chat_id,
            system_prompt=system_prompt,
            contents=ctx.contents,
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
        # it via the normal path - charge a flat estimate
        # instead, since real input+output tokens were plausibly
        # still spent on Google's side (no real cancellation
        # exists, per ADR 00732's Context).
        await record_tokens(agent_id, settings.AGENT_SUPERSEDED_CALL_TOKEN_PENALTY)
        await arm_debounce_now(agent_id, chat_id)
        ctx.ended_status = "done"
        return True
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
        return True  # ended_status stays "error", as before

    content = result.content
    await record_usage(ctx, result)

    if result.finish_reason == "MAX_TOKENS":
        # ADR 0102: True = a config-mode partial was posted and a
        # continuation was requested - keep going instead of ending.
        return not await finish_max_tokens(ctx, content)

    call = extract_function_call(content)
    if call is None:
        await finish_without_call(ctx, content)
        return True

    # Append Gemini's own returned content dict verbatim (not a
    # hand-rebuilt {name, args} part) - gemini-flash-latest is a
    # "thinking" model that attaches an opaque thoughtSignature to
    # functionCall parts, which it then requires echoed back on
    # the next call's contents; reconstructing the part from just
    # call["name"]/call["args"] silently drops it and Gemini 400s
    # on the following round-trip ("Function call is missing a
    # thought_signature in functionCall parts").
    content.setdefault("role", "model")
    ctx.contents.append(content)

    if round_trip == settings.AGENT_TURN_MAX_TOOL_ROUNDTRIPS:
        await finish_at_round_trip_cap(ctx, call)
        return True

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
        ctx.ended_status = "done"
        return True

    return await dispatch_tool_call(ctx, call)


async def dispatch_tool_call(ctx: TurnCtx, call: dict) -> bool:
    """Runs the model's tool call and appends its response. True = turn finished."""
    agent_id, chat_id, session = ctx.agent_id, ctx.chat_id, ctx.session

    if ctx.config_mode_turn:
        await _publish_agent_thinking(
            ctx.owner_user_id, "tool_call", _TOOL_THINKING_LABELS.get(call["name"], "Working…")
        )
    if ctx.goal_turn is not None and ctx.goal_acted and call["name"] in _MESSAGE_SENDING_TOOL_NAMES:
        # ADR 0099: one message per goal turn - a further round-trip let the
        # model re-send the same opener, so a second send is refused unexecuted.
        ctx.contents.append({"role": "user", "parts": [function_response_part(
            call["name"],
            {"error": "you already sent your message this turn - call complete_task/fail_task if the goal is settled, otherwise end the turn"},
        )]})
        return False
    if call["name"] == "continue_message" and ctx.continuations_used >= settings.AGENT_MAX_CONTINUATION_MESSAGES:
        # ADR 0102: hard cap, enforced here (never by the prompt) - the call
        # is refused unexecuted, so the model must finish with send_message.
        ctx.contents.append({"role": "user", "parts": [function_response_part(
            call["name"],
            {"error": "continuation limit reached - send the rest with send_message, trimmed to fit"},
        )]})
        return False
    tool_result = await execute_tool_call(session, ctx.agent, call["name"], call["args"], chat_id=chat_id)
    await session.commit()
    # ADR 0096: remember only the LAST tool outcome - a success
    # here clears any earlier failure, since the turn is no
    # longer "ending on top of" it. Checked only at the two
    # turn-ending branches, never mid-loop.
    tool_failed = isinstance(tool_result, dict) and "error" in tool_result
    if call["name"] == "continue_message" and not tool_failed:
        ctx.continuations_used += 1
        tool_result["continuations_remaining"] = (
            settings.AGENT_MAX_CONTINUATION_MESSAGES - ctx.continuations_used
        )
    if tool_failed:
        ctx.last_tool_error = str(tool_result["error"])
        ctx.last_tool_name = call["name"]
    else:
        ctx.last_tool_error = None
        ctx.last_tool_name = None
    if ctx.goal_turn is not None:
        # ADR 0099: a successful terminal tool closed the task (and
        # queued the owner summary) - the turn is over.
        if not tool_failed and call["name"] in TERMINAL_TOOL_NAMES:
            ctx.ended_status = "done"
            return True
        if not tool_failed and call["name"] in _MESSAGE_SENDING_TOOL_NAMES:
            # One message per goal turn (a second send is refused above), but
            # the turn stays open so the model can still call complete_task /
            # fail_task right after a closing message ("deal!"). If it doesn't,
            # finish_without_call records the outcome (idle/turn caps).
            ctx.goal_acted = True
    if call["name"] == "no_reply_needed":
        # ADR 0065: the model explicitly chose to end this
        # config-mode turn without posting anything - stop right
        # here instead of looping for another round-trip or
        # falling through to the call-is-None/_post_config_reply
        # path (which posts unconditionally).
        if ctx.knowledge_instruction is not None or ctx.schedule_instruction is not None:
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
        ctx.ended_status = "done"
        return True
    # The reply just landed (new_message clears the indicator
    # client-side) - stop the loop right here instead of waiting
    # for the turn's finally, or a tick still in flight can
    # re-publish `typing` after the message already arrived and
    # leave a phantom indicator with nothing left to clear it
    # (the turn may keep going for more round-trips after this).
    if call["name"] in _MESSAGE_SENDING_TOOL_NAMES and ctx.peer_typing_task is not None:
        ctx.peer_typing_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ctx.peer_typing_task
        ctx.peer_typing_task = None
    ctx.contents.append({"role": "user", "parts": [function_response_part(call["name"], tool_result)]})
    return False
