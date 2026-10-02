"""One agent turn (`_run_turn`) - setup, typing/thinking indicators and
teardown (split out of invoke_worker.py by ADR 0100). The phases it drives:
invoke_turn_pre.py (gates, routing, seeding) -> invoke_turn_loop.py (Gemini
round-trips + tool dispatch) -> invoke_turn_steps.py (sub-steps/turn endings).
State is carried in invoke_turn_ctx.TurnCtx."""
import asyncio
import contextlib
import logging

from infra.db.connection import session_scope
from modules.agents.crud import get_agent_by_id
from modules.agents.invoke_debounce import claim_typing_indicator, release_typing_indicator
from modules.agents.invoke_notify import _publish_agent_thinking, _publish_peer_typing_loop
from modules.agents.invoke_turn_ctx import TurnCtx
from modules.agents.invoke_turn_loop import run_round_trips
from modules.agents.invoke_turn_pre import begin_goal, route_owner_message, run_clarify_gate, run_judge_gate, seed_contents
from modules.agents.tools import is_config_mode

logger = logging.getLogger(__name__)


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
        # Must agree with the real tool-mode gate (dispatch.is_config_mode),
        # which returns False for chat_id=None - a schedule/ephemeral-task
        # turn with no chat target dispatches execution-mode tools, so it must
        # not be flagged as config-mode here either.
        config_mode_turn = is_config_mode(agent, chat_id)
        ctx = TurnCtx(
            session=session,
            agent=agent,
            agent_id=agent_id,
            chat_id=chat_id,
            message_id=message_id,
            schedule_instruction=schedule_instruction,
            knowledge_instruction=knowledge_instruction,
            scoped_system_prompt=scoped_system_prompt,
            owner_user_id=agent.owner_user_id,
            config_mode_turn=config_mode_turn,
        )
        if config_mode_turn:
            await _publish_agent_thinking(ctx.owner_user_id, "started")
        # Real peer-visible "typing" indicator (not the owner-only
        # agent_thinking above) - only for execution-mode turns against an
        # actual chat with the agent, never the owner's own config-mode
        # drawer chat (that already gets agent_thinking) and never a
        # schedule-fired turn with chat_id=None.
        owns_typing_indicator = False
        if chat_id is not None and not config_mode_turn:
            # ADR 0075: claim_typing_indicator is a SET NX - only the first
            # turn touching this chat while activity is unanswered actually
            # owns (refreshes/releases) the shared marker; a turn that starts
            # while a prior turn for the same chat already owns it (this
            # turn is itself the replacement for a mid-turn supersede) still
            # runs its own publish loop, just without touching the marker's
            # lifecycle - see _publish_peer_typing_loop's owns_indicator arg.
            owns_typing_indicator = await claim_typing_indicator(chat_id)
            ctx.peer_typing_task = asyncio.create_task(
                _publish_peer_typing_loop(chat_id, ctx.owner_user_id, owns_indicator=owns_typing_indicator)
            )
        try:
            if await run_judge_gate(ctx):
                return
            await route_owner_message(ctx)
            if await run_clarify_gate(ctx):
                return
            await seed_contents(ctx)
            if await begin_goal(ctx):
                return
            await run_round_trips(ctx)
        finally:
            if ctx.peer_typing_task is not None:
                ctx.peer_typing_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await ctx.peer_typing_task
            if owns_typing_indicator:
                # ADR 0075: only the owning turn tears the marker down - if
                # this turn was mid-turn superseded, the replacement it
                # triggers (via arm_debounce_now) claims a fresh marker of
                # its own rather than inheriting this one, so releasing here
                # is always safe (never races a still-running replacement's
                # ownership).
                await release_typing_indicator(chat_id)
            if config_mode_turn:
                await _publish_agent_thinking(ctx.owner_user_id, ctx.ended_status)
