"""Tool-outcome-mismatch judge (ADR 0096) - a dedicated jev classification
gate run only at the two points a turn can end right after an unresolved
tool failure, deciding whether that failure plausibly means the owner's
underlying goal was NOT accomplished.

Different proposition entirely from every other judge: not "is this message
on-topic/malicious" (judge.py) or "does this file match the request"
(attachment_judge.py), but "does this specific tool failure, read against
what the turn was originally asked to do, plausibly mean the request went
unfulfilled." One Noul question, one jev call, own rate bucket
(AGENT_OUTCOME_JUDGE_CALLS_PER_MINUTE) - never shares judge.py's or
attachment_judge.py's buckets.

Fails open to SILENCE, not to "approved" - unlike every other judge here,
this one gates a notification, not an action, so the safe failure direction
on a broken jev call is "say nothing" rather than risk nagging the owner on
a technical hiccup unrelated to their actual request.
"""
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from infra.ids.client import next_id
from infra.ratelimit.service import check_and_increment
from modules.agents.gemini_client import GeminiChatError, generate_structured
from modules.agents.models import Agent, AgentOutcomeJudgeLog
from modules.agents.typesafe_client import TypeSafeError, classify, noul_value

logger = logging.getLogger(__name__)

_OUTCOME_NOTICE_GLYPH = "❗"  # exclamation mark - distinct from 🤝/⚠️/📎

_EXPLANATION_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"explanation": {"type": "string"}},
    "required": ["explanation"],
}

LOCAL_OUTCOME_EXPLANATION = (
    "Heads up - I ran into a problem while working on your last request "
    "({tool_name}: {tool_error}) and wasn't able to finish it. You may want "
    "to try again or check on it yourself."
)


class OutcomeVerdict:
    __slots__ = ("is_mismatch", "reason")

    def __init__(self, is_mismatch: bool, reason: str):
        self.is_mismatch = is_mismatch
        self.reason = reason


async def evaluate_tool_outcome(
    session: AsyncSession,
    agent: Agent,
    chat_id: int,
    *,
    goal_text: str,
    tool_name: str,
    tool_error: str,
) -> OutcomeVerdict:
    """Runs the outcome-mismatch gate for one turn-ending unresolved tool
    error. Always returns an OutcomeVerdict - never raises; a technical
    failure is logged at ERROR and reported as no-mismatch (fail-open to
    silence). Every real verdict (mismatch or not) is logged to
    AgentOutcomeJudgeLog for tuning; a fail-open verdict is not logged -
    there is nothing to tune from a jev outage."""
    if not goal_text.strip():
        # No original instruction to compare the failure against (shouldn't
        # normally happen - every turn kind seeds *some* goal text - but
        # stay silent rather than guess).
        return OutcomeVerdict(False, "no goal text to evaluate against")

    if not await check_and_increment(
        agent.id,
        "agent_outcome_judge_calls",
        settings.AGENT_OUTCOME_JUDGE_CALLS_PER_MINUTE,
        settings.AGENT_OUTCOME_JUDGE_CALLS_WINDOW_SECONDS,
    ):
        logger.warning("outcome_judge: agent %s over judge call budget, failing open to silence", agent.id)
        return OutcomeVerdict(False, "outcome judge rate limit exceeded - failed open to silence")

    questions = {
        "matches_goal": {
            "type": "noul",
            "instructions": {
                "question": (
                    "The tool failure described below plausibly means the person's "
                    "original request was NOT accomplished - a real, unresolved "
                    "problem the person would want to know about, not a step the "
                    "agent could reasonably be expected to recover from on its own "
                    "or that doesn't matter to the outcome they actually wanted. "
                    "Be conservative: only answer true when the failure plausibly "
                    "blocked the actual goal, not for a minor/cosmetic error."
                ),
                "original_goal": goal_text,
                "failed_tool": tool_name,
                "failure_reason": tool_error,
            },
            "criteria": {
                "true": "The failure plausibly left the person's request unfulfilled",
                "false": "The failure is minor, unrelated to the actual goal, or the goal was still reached",
            },
        }
    }

    try:
        answers = await classify(state=goal_text, questions=questions)
        is_mismatch = noul_value(answers, "matches_goal") >= settings.AGENT_OUTCOME_JUDGE_MISMATCH_THRESHOLD
        reason = (
            "tool failure plausibly left the request unfulfilled"
            if is_mismatch
            else "tool failure does not appear to have blocked the actual goal"
        )
        verdict = OutcomeVerdict(is_mismatch, reason)
    except (TypeSafeError, KeyError, TypeError, ValueError) as exc:
        logger.error(
            "outcome_judge: jev classification failed for agent %s chat %s tool %s, failing open to silence: %s",
            agent.id, chat_id, tool_name, exc,
        )
        return OutcomeVerdict(False, f"judge call failed - failed open to silence: {exc}")

    await _log_verdict(session, agent.id, chat_id, tool_name, verdict)
    return verdict


async def _log_verdict(
    session: AsyncSession, agent_id: int, chat_id: int, tool_name: str, verdict: OutcomeVerdict
) -> None:
    session.add(
        AgentOutcomeJudgeLog(
            id=await next_id(),
            agent_id=agent_id,
            chat_id=chat_id,
            tool_name=tool_name,
            is_match=not verdict.is_mismatch,
            reason=verdict.reason,
        )
    )


async def _generate_outcome_explanation(agent: Agent, goal_text: str, tool_name: str, tool_error: str) -> str:
    """Minimal, tool-free, history-free Gemini call (same cheap-model
    pattern as judge.py's redirect-text call and clarify.py's question
    call) authoring a short, owner-facing explanation of what went wrong -
    jev already decided there's a mismatch, it cannot author free text
    itself. Failure here falls back to LOCAL_OUTCOME_EXPLANATION, never
    raises."""
    system_prompt = (
        "You are this user's own AI agent, writing them a short private "
        "notice in their own chat with you. While working on their request "
        "below, one of your actions failed in a way that plausibly means "
        "you could not finish what they asked. Write ONE short, plain, "
        "natural one- or two-sentence message, in the SAME language as "
        "their request below, telling them what you were trying to do and "
        "that it didn't go through - so they know to follow up or try "
        "again. Do not mention tool names, error codes, or any internal "
        "technical detail - describe what happened in plain, everyday "
        "words, the way a person would explain it."
    )
    user_text = f"Their request: {goal_text}\n\nWhat failed: {tool_name} - {tool_error}"
    try:
        result = await generate_structured(
            model=settings.AGENT_JUDGE_REDIRECT_MODEL,
            system_prompt=system_prompt,
            user_text=user_text,
            response_schema=_EXPLANATION_RESPONSE_SCHEMA,
        )
        explanation = str(result.get("explanation", "")).strip()
        return explanation or LOCAL_OUTCOME_EXPLANATION.format(tool_name=tool_name, tool_error=tool_error)
    except (GeminiChatError, KeyError, TypeError) as exc:
        logger.warning("outcome_judge: explanation generation failed, falling back to template: %s", exc)
        return LOCAL_OUTCOME_EXPLANATION.format(tool_name=tool_name, tool_error=tool_error)


async def notify_outcome_mismatch(
    session: AsyncSession, agent: Agent, *, goal_text: str, tool_name: str, tool_error: str
) -> None:
    """Posts the owner-facing mismatch notice into the owner's own agent
    chat - informational only, never pauses/freezes the chat (unlike
    escalate_chat, ADR 0074), so uses send_system_message directly rather
    than escalate_chat's freeze-and-notify body. Independently
    try/excepted - a failure here must never raise out of _run_turn."""
    from modules.messaging.send import send_system_message

    explanation = await _generate_outcome_explanation(agent, goal_text, tool_name, tool_error)
    notice = f"{_OUTCOME_NOTICE_GLYPH} {explanation}"
    try:
        await send_system_message(session, agent.owner_agent_chat_id, notice)
    except Exception:
        logger.exception("agent %s: failed to post outcome-mismatch notice", agent.id)
