"""`_run_turn` pre-loop phases (split out of invoke_worker.py by ADR 0100):
Judge gate, owner-chat router, CLARIFY special case, contents seeding and
goal-task turn start. Each gate returns True when it ended the turn (the
caller returns immediately), mirroring the early `return`s of the former
single-function `_run_turn`."""
import logging
import uuid

from config import settings
from modules.agents.builder_flow import BuilderState
from modules.agents.clarify import generate_clarify_question
from modules.agents.crud import update_agent_config
from modules.agents.goal_tasks import begin_goal_turn, find_goal_task_for_chat
from modules.agents.invoke_notify import _publish_agent_thinking
from modules.agents.invoke_turn_ctx import TurnCtx
from modules.agents.invoke_turn_helpers import (
    _ROUTED_STATE_THINKING_LABELS,
    _build_initial_contents,
    _build_knowledge_contents,
    _build_schedule_contents,
    _format_history_transcript,
    _post_config_reply,
)
from modules.agents.judge import evaluate_message, local_redirect_text
from modules.agents.owner_chat_router import route_owner_turn
from modules.agents.tools.common import escalate_chat
from modules.messaging import service as message_service
from modules.messaging.common import AGENT_REPLY_MESSAGE_TYPE
from modules.messaging.crud import get_message_by_id
from modules.messaging.read_api import get_message_history

logger = logging.getLogger(__name__)


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


async def run_judge_gate(ctx: TurnCtx) -> bool:
    """LLM Judge gate (ADR 0053) - execution-mode, message-fired turns
    only (on_specific_chats/on_unknown_sender/on_any_message alike,
    no trigger-type carve-out). Never runs for config-mode turns
    (the owner's own drawer chat - not an untrusted party) or
    schedule-fired turns (chat_id is None, the "message" is the
    agent's own instruction, not external input). A rejected
    message skips the main model entirely and gets a short, locally
    -templated redirect instead - no second Gemini call."""
    if ctx.message_id is None or ctx.config_mode_turn:
        return False
    session, agent, chat_id, message_id = ctx.session, ctx.agent, ctx.chat_id, ctx.message_id
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
    if verdict.is_approved:
        return False

    logger.info(
        "agent_worker: judge rejected agent %s chat %s message %s: %s",
        ctx.agent_id, chat_id, message_id, verdict.reason,
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
            ctx.agent_id, chat_id, message_id, verdict.reason,
        )
        await escalate_chat(session, agent, chat_id, verdict.reason, notice_prefix="⚠️")
        await session.commit()
    # ADR 0088: a media-only message (photo/video/voice
    # note/file) the agent can't view, still unanswered by
    # the customer - the judge decided it plausibly needs a
    # human to actually look at it.
    elif verdict.needs_human_review:
        logger.info(
            "agent_worker: judge flagged unseeable media needing review, "
            "agent %s chat %s message %s: %s",
            ctx.agent_id, chat_id, message_id, verdict.reason,
        )
        await escalate_chat(session, agent, chat_id, verdict.reason, notice_prefix="📎")
        await session.commit()
    ctx.ended_status = "done"
    return True


async def route_owner_message(ctx: TurnCtx) -> None:
    """ADR 0093 Phase 3: the jev router is the sole way builder_state
    changes now - runs once per config-mode owner-chat turn (a real
    message, not a schedule/knowledge-notice turn, which have no
    owner utterance to classify and keep whatever builder_state is
    already set), before deciding CLARIFY vs. the normal
    round-trip loop. Fail-frozen by construction:
    route_owner_turn always returns *some* RouterDecision (the
    agent's current builder_state, unchanged, on any internal
    failure) - so this unconditionally persists whatever it
    returns rather than branching on success/failure itself."""
    if not (ctx.config_mode_turn and ctx.message_id is not None):
        return
    session, agent, chat_id = ctx.session, ctx.agent, ctx.chat_id
    router_message = await get_message_by_id(session, chat_id, ctx.message_id)
    history = await get_message_history(
        session, agent.owner_user_id, chat_id, limit=settings.AGENT_ROUTER_CONTEXT_TURNS
    )
    recent_turns = _format_router_recent_turns(history, exclude_message_id=ctx.message_id)
    decision = await route_owner_turn(
        session,
        agent,
        chat_id,
        recent_turns,
        router_message.content if router_message else "",
    )
    if decision.state != BuilderState(agent.builder_state):
        agent = await update_agent_config(session, agent, {"builder_state": decision.state.value})
        ctx.agent = agent
    await session.commit()
    await _publish_agent_thinking(
        ctx.owner_user_id,
        "routed",
        _ROUTED_STATE_THINKING_LABELS.get(agent.builder_state, "Thinking…"),
    )


async def run_clarify_gate(ctx: TurnCtx) -> bool:
    """ADR 0093: BuilderState.CLARIFY is a deliberate exception to the
    normal per-round-trip Gemini turn every other builder_state
    uses - a dedicated minimal call (no chat history, no tool
    schemas, its own cheap model tier), mirroring judge.py's
    redirect-text call."""
    agent = ctx.agent
    if not (
        ctx.config_mode_turn
        and ctx.message_id is not None
        and BuilderState(agent.builder_state) == BuilderState.CLARIFY
    ):
        return False
    clarify_message = await get_message_by_id(ctx.session, ctx.chat_id, ctx.message_id)
    question = await generate_clarify_question(
        agent, clarify_message.content if clarify_message else ""
    )
    await _post_config_reply(ctx.session, agent, ctx.chat_id, question)
    await ctx.session.commit()
    ctx.ended_status = "done"
    return True


async def seed_contents(ctx: TurnCtx) -> None:
    """Builds the turn's initial Gemini contents + the goal text for the
    ADR 0096 outcome check (knowledge-notice / schedule / message seeds)."""
    session, agent, chat_id = ctx.session, ctx.agent, ctx.chat_id
    if ctx.knowledge_instruction is not None:
        ctx.contents = await _build_knowledge_contents(session, agent, ctx.knowledge_instruction)
        ctx.goal_text = ctx.knowledge_instruction
    elif ctx.schedule_instruction is not None:
        ctx.contents = await _build_schedule_contents(session, agent, ctx.schedule_instruction, chat_id)
        ctx.goal_text = ctx.schedule_instruction
    else:
        ctx.contents = await _build_initial_contents(session, agent, chat_id)
        # ADR 0096: the outcome-mismatch judge needs the triggering
        # message's own text, not the full history transcript
        # _build_initial_contents seeds the turn with - re-fetched
        # here rather than threaded through that helper, since only
        # this one caller (the outcome check at the end of the turn)
        # needs it standalone.
        ctx.goal_text = ""
        if ctx.message_id is not None:
            goal_message = await get_message_by_id(session, chat_id, ctx.message_id)
            ctx.goal_text = (goal_message.content if goal_message else "") or ""


async def begin_goal(ctx: TurnCtx) -> bool:
    """ADR 0099: an active goal task targeting this chat - counts the
    turn, and force-closes if its turn budget was already spent."""
    if ctx.chat_id is None or ctx.config_mode_turn:
        return False
    ctx.goal_turn = await begin_goal_turn(ctx.session, ctx.agent, ctx.chat_id)
    if ctx.goal_turn is None:
        return False
    await ctx.session.commit()
    if ctx.goal_turn.closed:
        ctx.ended_status = "done"
        return True
    return False
