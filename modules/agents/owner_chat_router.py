"""Owner-chat jev router (ADR 0093 Phase 2, wired in by Phase 3).

Phase 6a/6b add two refinements on top of the base classification: session
stickiness (a low-signal message doesn't yank the owner out of the current
state unless it clears the current state's own score by
AGENT_ROUTER_STICKINESS_MARGIN) and a single-clarify-question cap (a
BuilderState.CLARIFY turn always resolves to a real destination on its very
next turn, never chains into a second clarify question).

Runs once per config-mode owner-chat turn and decides, deterministically,
which BuilderState should handle the turn - replacing the model-self-directed
`transfer_to_*` handoff tools (deleted in Phase 3) with a router that never
lets the model itself pick its own next state.

Uses the same TypeSafe `jev` classification backend as the LLM Judge
(modules/agents/judge.py, ADR 0076) via typesafe_client.classify() - one
independent Noul (0-1 confidence) question per destination, in a single call,
same proven pattern judge.py already uses for its four flags (rather than a
`choice`/`score` question type, whose exact response shape has no precedent
elsewhere in this codebase to build against). The four Noul scores are then
ranked to pick the top destination and compute the ADR 0093 clarify margin.

Fail-open policy here is "fail-frozen", NOT "fail-open-to-action" (ADR 0093):
on any classification failure (rate limit, HTTP/shape error), route_owner_turn
returns the agent's current builder_state unchanged rather than guessing - a
routing failure must never silently grant one_off_action's execution tools by
default.
"""
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from infra.ids.client import next_id
from infra.ratelimit.service import check_and_increment
from modules.agents.builder_flow import BuilderState
from modules.agents.models import Agent, AgentRouterLog
from modules.agents.typesafe_client import TypeSafeError, classify, noul_value

logger = logging.getLogger(__name__)

# jev destination labels - deliberately NOT the same strings as
# BuilderState.value ("builder_agent"/"help_agent_building") so the
# classification question can use short, unambiguous names; _DESTINATION_TO_STATE
# below maps back to the real BuilderState the rest of the system understands.
_DESTINATIONS = ("one_off_action", "builder", "help_building", "help_general")

_DESTINATION_TO_STATE = {
    "one_off_action": BuilderState.ONE_OFF_ACTION,
    "builder": BuilderState.BUILDER,
    "help_building": BuilderState.HELP_BUILDING,
    "help_general": BuilderState.HELP_GENERAL,
}

# ADR 0093 Phase 6a: reverse of _DESTINATION_TO_STATE, used to look up the
# current builder_state's own jev probability. Deliberately excludes
# BuilderState.CLARIFY - it has no corresponding jev destination/probability,
# and is never a "previous state" the stickiness check runs against (see
# route_owner_turn: the CLARIFY single-question cap is checked first and
# returns before stickiness would ever see it).
_STATE_TO_DESTINATION = {state: destination for destination, state in _DESTINATION_TO_STATE.items()}

# The two destinations the ADR 0093 clarify margin applies between - a
# one-off request ("send this now") vs. a persistent-behavior request ("set
# this up going forward") is the one ambiguity worth pausing on; either Help
# destination is never ambiguous enough with one_off_action/builder to
# warrant clarifying (asking "do you want help, or do you want to do this
# once?" is not a useful question to put to the owner).
_CLARIFY_PAIR = frozenset({"one_off_action", "builder"})


class RouterDecision:
    __slots__ = ("state", "probabilities", "margin", "failed_open", "sticky")

    def __init__(
        self,
        state: BuilderState,
        probabilities: dict[str, float] | None,
        margin: float | None,
        failed_open: bool = False,
        sticky: bool = False,
    ):
        self.state = state
        self.probabilities = probabilities
        self.margin = margin
        self.failed_open = failed_open
        self.sticky = sticky


def _format_recent_turns(recent_turns: list[str]) -> str:
    if not recent_turns:
        return "(no prior turns in this conversation)"
    return "\n".join(recent_turns)


_DESTINATION_DEFINITIONS = {
    "one_off_action": (
        "the owner wants something done right now, or at a specific future "
        "time (send a message, look something up, create a chat, leave a "
        "group, ask someone something and report back) - a single action, "
        "not a standing behavior."
    ),
    "builder": (
        "the owner wants to create, configure, or change how their agent "
        "behaves going forward - its persona, rules, triggers, restrictions, "
        "identity, or any other persistent setting."
    ),
    "help_building": (
        "the owner is asking how to build/configure an agent, or what a "
        "setting/feature of the agent system means, without yet wanting to "
        "start building."
    ),
    "help_general": (
        "the owner is asking about using Linka itself (chats, search, "
        "groups, media, receipts, scheduling, profile, storage) - not about "
        "their own agent at all."
    ),
}


def _build_jev_questions(recent_turns: list[str]) -> dict[str, dict]:
    """Four independent Noul (0-1 confidence) questions in one call, one per
    destination - same proven pattern judge.py already uses for its four
    flags (on_topic/prompt_injection/info_extraction/code_execution), rather
    than a single compound decision. `state` (passed separately to
    classify()) carries the new message; each question's instructions carry
    that one destination's definition, the other three for contrast, plus the
    short recent-turn context."""
    context = _format_recent_turns(recent_turns)
    shared_preamble = (
        "This is the owner's own private chat with their own AI agent - not a "
        "conversation with a third party. If the owner's newest message below "
        "is a short reply/acknowledgement/continuation of an ongoing exchange, "
        "classify it the same way that exchange was already going, rather than "
        f"in isolation.\n\nRecent turns:\n{context}"
    )
    questions: dict[str, dict] = {}
    for destination, definition in _DESTINATION_DEFINITIONS.items():
        others = "\n".join(
            f"- {other}: {other_definition}"
            for other, other_definition in _DESTINATION_DEFINITIONS.items()
            if other != destination
        )
        questions[destination] = {
            "type": "noul",
            "instructions": (
                f"The owner's newest message is best classified as: {definition}\n\n"
                f"For contrast, the other possible classifications are:\n{others}\n\n"
                f"{shared_preamble}"
            ),
        }
    return questions


def _resolve_margin(probabilities: dict[str, float]) -> tuple[str, str, float]:
    """Returns (top_destination, second_destination, margin) from a
    probabilities dict - callers only act on the margin when the top two are
    exactly the _CLARIFY_PAIR."""
    ranked = sorted(probabilities.items(), key=lambda item: item[1], reverse=True)
    top_dest, top_prob = ranked[0]
    second_dest, second_prob = ranked[1] if len(ranked) > 1 else (None, 0.0)
    return top_dest, second_dest, top_prob - second_prob


async def route_owner_turn(
    session: AsyncSession,
    agent: Agent,
    chat_id: int,
    recent_turns: list[str],
    new_message: str,
) -> RouterDecision:
    """Classifies one config-mode owner-chat turn into the BuilderState that
    should handle it. Always returns a RouterDecision - never raises; a
    technical failure is logged at ERROR and reported as a fail-frozen
    decision (agent's current builder_state, unchanged). Every decision
    (resolved, clarified, or fail-frozen) is logged to AgentRouterLog for
    Phase 5 margin tuning.

    Not called from invoke_worker.py yet (Phase 3 wires this in) - this
    module is unit-tested standalone in this phase.
    """
    previous_state = BuilderState(agent.builder_state)

    if not await check_and_increment(
        agent.id,
        "agent_router_calls",
        settings.AGENT_ROUTER_CALLS_PER_MINUTE,
        settings.AGENT_ROUTER_CALLS_WINDOW_SECONDS,
    ):
        logger.warning("owner_chat_router: agent %s over router call budget, failing frozen", agent.id)
        decision = RouterDecision(previous_state, None, None, failed_open=True)
        await _log_decision(session, agent.id, chat_id, previous_state, decision)
        return decision

    questions = _build_jev_questions(recent_turns)

    try:
        answers = await classify(state=new_message, questions=questions)
        probabilities = {
            destination: noul_value(answers, destination) for destination in _DESTINATIONS
        }
    except (TypeSafeError, KeyError, TypeError, ValueError) as exc:
        logger.error(
            "owner_chat_router: jev classification failed for agent %s chat %s, failing frozen: %s",
            agent.id, chat_id, exc,
        )
        decision = RouterDecision(previous_state, None, None, failed_open=True)
        await _log_decision(session, agent.id, chat_id, previous_state, decision)
        return decision

    top_dest, second_dest, margin = _resolve_margin(probabilities)
    top_state = _DESTINATION_TO_STATE[top_dest]
    sticky = False

    if previous_state == BuilderState.CLARIFY:
        # ADR 0093 Phase 6b: single-clarify-question cap - a clarify question
        # gets at most one follow-up turn before resolution is forced, so an
        # owner giving a still-ambiguous answer ("do whatever you think")
        # never loops back into a second clarify question. Deliberately not
        # hardcoded to one_off_action - stays driven by the classifier's own
        # (possibly weak) signal, which in the specific near-50/50 case that
        # produced the original clarify is one_off_action/builder either way
        # (both are low-permanent-damage outcomes for a single mis-route).
        resolved_state = top_state
    elif (
        top_state != previous_state
        and previous_state in _STATE_TO_DESTINATION
        and probabilities[_STATE_TO_DESTINATION[previous_state]]
        >= probabilities[top_dest] - settings.AGENT_ROUTER_STICKINESS_MARGIN
    ):
        # ADR 0093 Phase 6a: session stickiness - the top destination doesn't
        # clear the current state's own score by more than the stickiness
        # margin, so stay rather than switch on a low-signal message. Only
        # reachable when top_state != previous_state (nothing to stick to
        # otherwise) - the clarify-pair check below never runs in this case,
        # since nothing is actually being left.
        resolved_state = previous_state
        sticky = True
    elif {top_dest, second_dest} == _CLARIFY_PAIR and margin < settings.AGENT_ROUTER_CLARIFY_MARGIN:
        resolved_state = BuilderState.CLARIFY
    else:
        resolved_state = top_state

    decision = RouterDecision(resolved_state, probabilities, margin, sticky=sticky)
    await _log_decision(session, agent.id, chat_id, previous_state, decision)
    return decision


async def _log_decision(
    session: AsyncSession,
    agent_id: int,
    chat_id: int,
    previous_state: BuilderState,
    decision: RouterDecision,
) -> None:
    session.add(
        AgentRouterLog(
            id=await next_id(),
            agent_id=agent_id,
            chat_id=chat_id,
            previous_state=previous_state.value,
            resolved_state=decision.state.value,
            probabilities=decision.probabilities,
            margin=decision.margin,
            failed_open=decision.failed_open,
            sticky=decision.sticky,
        )
    )
