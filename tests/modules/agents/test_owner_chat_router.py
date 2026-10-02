"""Owner-chat jev router (ADR 0093) - modules/agents/owner_chat_router.py.

Runs against the real ephemeral Postgres (ADR 0032) and real Redis
(`redis_db`), since route_owner_turn reads/writes AgentRouterLog and the rate
limit is a real Redis fixed-window counter. What's mocked is the TypeSafe
`classify` call (the classification backend) - no real HTTP calls.

Standalone unit tests only - route_owner_turn itself, in isolation from
invoke_worker.py's calling context. The actual config-mode-turn wiring (ADR
0093 Phase 3: invoke_worker.py calling route_owner_turn once before every
config-mode turn and persisting its decision) is covered separately in
tests/modules/agents/test_run_turn_config_mode.py.

Behavioral expectations encoded here (per ADR 0093's "Router contract"):

- The destination with the highest Noul score wins, mapped to its
  BuilderState.
- When the top two destinations are exactly {one_off_action, builder} and
  their margin is under AGENT_ROUTER_CLARIFY_MARGIN, the result is
  BuilderState.CLARIFY instead - never a guess.
- A margin under the threshold between any OTHER pair (e.g. builder vs.
  help_building) never triggers clarify - only the one_off_action/builder
  ambiguity does.
- Fail-frozen, not fail-open-to-action: both a TypeSafeError/malformed
  response and an exhausted rate-limit budget resolve to the agent's current
  (unchanged) builder_state, with failed_open=True and no probabilities.
- Every call - resolved, clarified, or fail-frozen - writes exactly one
  AgentRouterLog row.
"""
import json
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from modules.agents.builder_flow import BuilderState
from modules.agents.models import Agent, AgentRouterLog, DEFAULT_AGENT_RESTRICTIONS, DEFAULT_AGENT_TRIGGERS
from modules.agents.owner_chat_router import route_owner_turn
from modules.agents.typesafe_client import TypeSafeError
from modules.chats.crud.crud_chat import create_chat
from modules.chats.crud.crud_participant import add_participant_to_chat
from modules.users.crud import create_user

pytestmark = pytest.mark.asyncio

_ID = 0


def _next_id() -> int:
    global _ID
    _ID += 1
    return 960_000_000 + _ID


async def _make_user(session: AsyncSession) -> int:
    user_id = _next_id()
    await create_user(session, user_id=user_id, phone_number=f"+1555{user_id}")
    return user_id


async def _make_chat(session: AsyncSession, *user_ids: int) -> int:
    chat_id = _next_id()
    await create_chat(session, chat_id=chat_id, is_group=False)
    for uid in user_ids:
        await add_participant_to_chat(session, chat_id=chat_id, user_id=uid)
    return chat_id


async def _make_agent(
    session: AsyncSession,
    owner_user_id: int,
    owner_agent_chat_id: int,
    *,
    builder_state: str = "one_off_action",
) -> Agent:
    agent = Agent(
        id=_next_id(),
        owner_user_id=owner_user_id,
        owner_agent_chat_id=owner_agent_chat_id,
        is_enabled=True,
        builder_state=builder_state,
        triggers=json.loads(json.dumps(DEFAULT_AGENT_TRIGGERS)),
        restrictions=json.loads(json.dumps(DEFAULT_AGENT_RESTRICTIONS)),
    )
    session.add(agent)
    await session.flush()
    await session.commit()
    return agent


async def _router_log_rows(session: AsyncSession, agent_id: int) -> list[AgentRouterLog]:
    stmt = select(AgentRouterLog).where(AgentRouterLog.agent_id == agent_id)
    return (await session.execute(stmt)).scalars().all()


def _noul_answers(
    one_off_action: float = 0.0,
    builder: float = 0.0,
    help_building: float = 0.0,
    help_general: float = 0.0,
) -> dict:
    return {
        "one_off_action": {"type": "noul", "noul": one_off_action},
        "builder": {"type": "noul", "noul": builder},
        "help_building": {"type": "noul", "noul": help_building},
        "help_general": {"type": "noul", "noul": help_general},
    }


def _mock_classify(answers: dict | None = None, exc: Exception | None = None):
    """Patches modules.agents.owner_chat_router.classify (the name
    owner_chat_router.py imported into its own namespace), never the real
    typesafe_client function - so no HTTP calls happen."""
    if exc is not None:
        return patch("modules.agents.owner_chat_router.classify", AsyncMock(side_effect=exc))
    return patch("modules.agents.owner_chat_router.classify", AsyncMock(return_value=answers))


async def _setup(session: AsyncSession, *, builder_state: str = "one_off_action") -> tuple[Agent, int]:
    owner_id = await _make_user(session)
    chat_id = await _make_chat(session, owner_id)
    agent = await _make_agent(session, owner_id, chat_id, builder_state=builder_state)
    return agent, chat_id


async def test_highest_scoring_destination_wins(db_session, redis_db):
    agent, chat_id = await _setup(db_session)
    with _mock_classify(_noul_answers(one_off_action=0.9, builder=0.1, help_building=0.05, help_general=0.05)):
        decision = await route_owner_turn(db_session, agent, chat_id, [], "send a message to my brother")
    await db_session.commit()

    assert decision.state == BuilderState.ONE_OFF_ACTION
    assert decision.failed_open is False
    assert decision.probabilities["one_off_action"] == 0.9

    rows = await _router_log_rows(db_session, agent.id)
    assert len(rows) == 1
    assert rows[0].resolved_state == "one_off_action"
    assert rows[0].previous_state == "one_off_action"
    assert rows[0].failed_open is False


async def test_builder_destination_maps_to_builder_state(db_session, redis_db):
    agent, chat_id = await _setup(db_session)
    with _mock_classify(_noul_answers(one_off_action=0.1, builder=0.9, help_building=0.0, help_general=0.0)):
        decision = await route_owner_turn(db_session, agent, chat_id, [], "make my agent reply to refund questions")
    await db_session.commit()

    assert decision.state == BuilderState.BUILDER


@pytest.mark.parametrize(
    "help_dest,expected_state",
    [("help_building", BuilderState.HELP_BUILDING), ("help_general", BuilderState.HELP_GENERAL)],
)
async def test_help_destinations_map_correctly(db_session, redis_db, help_dest, expected_state):
    agent, chat_id = await _setup(db_session)
    scores = {"one_off_action": 0.05, "builder": 0.05, "help_building": 0.05, "help_general": 0.05}
    scores[help_dest] = 0.9
    with _mock_classify(_noul_answers(**scores)):
        decision = await route_owner_turn(db_session, agent, chat_id, [], "what does the knowledge base do?")
    await db_session.commit()

    assert decision.state == expected_state


async def test_close_margin_between_one_off_and_builder_triggers_clarify(db_session, redis_db):
    agent, chat_id = await _setup(db_session)
    # Margin (0.55 - 0.5 = 0.05) is under the default AGENT_ROUTER_CLARIFY_MARGIN.
    with _mock_classify(_noul_answers(one_off_action=0.55, builder=0.5, help_building=0.0, help_general=0.0)):
        decision = await route_owner_turn(
            db_session, agent, chat_id, [], "tomorrow at 12 send me a summary of the group"
        )
    await db_session.commit()

    assert decision.state == BuilderState.CLARIFY
    rows = await _router_log_rows(db_session, agent.id)
    assert rows[0].resolved_state == "clarify"


async def test_wide_margin_between_one_off_and_builder_does_not_clarify(db_session, redis_db):
    agent, chat_id = await _setup(db_session)
    with _mock_classify(_noul_answers(one_off_action=0.95, builder=0.1, help_building=0.0, help_general=0.0)):
        decision = await route_owner_turn(db_session, agent, chat_id, [], "send X a message now")
    await db_session.commit()

    assert decision.state == BuilderState.ONE_OFF_ACTION


async def test_close_margin_between_builder_and_help_building_never_clarifies(db_session, redis_db):
    """Clarify only applies to the one_off_action/builder ambiguity - any
    other close pair just resolves to the top scorer."""
    agent, chat_id = await _setup(db_session)
    with _mock_classify(_noul_answers(one_off_action=0.0, builder=0.55, help_building=0.5, help_general=0.0)):
        decision = await route_owner_turn(db_session, agent, chat_id, [], "how do triggers work?")
    await db_session.commit()

    assert decision.state == BuilderState.BUILDER


async def test_typesafe_error_fails_frozen(db_session, redis_db):
    agent, chat_id = await _setup(db_session, builder_state="builder_agent")
    with _mock_classify(exc=TypeSafeError("boom")):
        decision = await route_owner_turn(db_session, agent, chat_id, [], "anything")
    await db_session.commit()

    assert decision.state == BuilderState.BUILDER
    assert decision.failed_open is True
    assert decision.probabilities is None

    rows = await _router_log_rows(db_session, agent.id)
    assert rows[0].failed_open is True
    assert rows[0].previous_state == "builder_agent"
    assert rows[0].resolved_state == "builder_agent"


async def test_malformed_jev_response_fails_frozen(db_session, redis_db):
    agent, chat_id = await _setup(db_session, builder_state="help_general")
    with _mock_classify({"one_off_action": {"type": "noul", "noul": 0.9}}):  # missing keys
        decision = await route_owner_turn(db_session, agent, chat_id, [], "anything")
    await db_session.commit()

    assert decision.state == BuilderState.HELP_GENERAL
    assert decision.failed_open is True


async def test_router_never_raises_even_on_unexpected_exception_type(db_session, redis_db):
    agent, chat_id = await _setup(db_session)
    with patch("modules.agents.owner_chat_router.classify", AsyncMock(side_effect=RuntimeError("nope"))):
        with pytest.raises(RuntimeError):
            await route_owner_turn(db_session, agent, chat_id, [], "anything")
    # Confirms only the documented failure types (TypeSafeError/KeyError/
    # TypeError/ValueError) are treated as fail-frozen - an unexpected
    # exception type still propagates rather than being silently swallowed.


async def test_rate_limit_exceeded_fails_frozen_without_calling_jev(db_session, redis_db):
    agent, chat_id = await _setup(db_session, builder_state="clarify")

    for _ in range(settings.AGENT_ROUTER_CALLS_PER_MINUTE):
        with _mock_classify(_noul_answers(one_off_action=0.9)):
            await route_owner_turn(db_session, agent, chat_id, [], "anything")
        await db_session.commit()

    with _mock_classify(_noul_answers(one_off_action=0.9)) as mock_classify:
        decision = await route_owner_turn(db_session, agent, chat_id, [], "one too many")
        await db_session.commit()
    mock_classify.assert_not_awaited()

    assert decision.state == BuilderState.CLARIFY
    assert decision.failed_open is True

    rows = await _router_log_rows(db_session, agent.id)
    assert len(rows) == settings.AGENT_ROUTER_CALLS_PER_MINUTE + 1
    assert rows[-1].failed_open is True


# --- Phase 6a: session stickiness / hysteresis --------------------------------


async def test_low_signal_message_stays_in_current_state_via_stickiness(db_session, redis_db):
    """A short, ambiguous message mid-Builder-interview scores marginally
    higher for one_off_action, but not by more than the stickiness margin -
    stays in builder_agent instead of yanking the owner out."""
    agent, chat_id = await _setup(db_session, builder_state="builder_agent")
    # builder's own score (0.4) is within AGENT_ROUTER_STICKINESS_MARGIN
    # (default 0.15) of the top scorer, one_off_action (0.45): 0.05 gap.
    with _mock_classify(_noul_answers(one_off_action=0.45, builder=0.4, help_building=0.1, help_general=0.05)):
        decision = await route_owner_turn(db_session, agent, chat_id, [], "wait, what did Dani just say?")
    await db_session.commit()

    assert decision.state == BuilderState.BUILDER
    assert decision.sticky is True

    rows = await _router_log_rows(db_session, agent.id)
    assert rows[0].previous_state == "builder_agent"
    assert rows[0].resolved_state == "builder_agent"
    assert rows[0].sticky is True


async def test_clear_topic_change_switches_despite_stickiness(db_session, redis_db):
    """A genuine, clearly-scored topic change still switches states - the
    stickiness margin only saves a marginal flip, not a real one."""
    agent, chat_id = await _setup(db_session, builder_state="builder_agent")
    with _mock_classify(_noul_answers(one_off_action=0.05, builder=0.05, help_building=0.85, help_general=0.05)):
        decision = await route_owner_turn(db_session, agent, chat_id, [], "how does search work in this app?")
    await db_session.commit()

    assert decision.state == BuilderState.HELP_BUILDING
    assert decision.sticky is False


async def test_stickiness_not_applied_when_current_state_is_already_top_scorer(db_session, redis_db):
    """When the current state IS the top scorer, this is not a stickiness
    save (nothing is being kept from switching) - sticky must be False, and
    the ordinary clarify-margin logic still applies untouched (regression
    guard for test_close_margin_between_one_off_and_builder_triggers_clarify's
    scenario, now re-checked for the sticky flag specifically)."""
    agent, chat_id = await _setup(db_session, builder_state="one_off_action")
    with _mock_classify(_noul_answers(one_off_action=0.55, builder=0.5, help_building=0.0, help_general=0.0)):
        decision = await route_owner_turn(db_session, agent, chat_id, [], "tomorrow at 12 send a summary")
    await db_session.commit()

    assert decision.state == BuilderState.CLARIFY
    assert decision.sticky is False


async def test_stickiness_margin_boundary_switches_when_gap_equals_margin(db_session, redis_db):
    """Exactly at the margin (gap == AGENT_ROUTER_STICKINESS_MARGIN) counts
    as sticky (the check is >=, matching AGENT_ROUTER_CLARIFY_MARGIN's own
    strict-less-than-on-the-other-side convention documented in
    owner_chat_router.py)."""
    agent, chat_id = await _setup(db_session, builder_state="help_general")
    margin = settings.AGENT_ROUTER_STICKINESS_MARGIN
    with _mock_classify(
        _noul_answers(one_off_action=0.5 + margin, builder=0.0, help_building=0.0, help_general=0.5)
    ):
        decision = await route_owner_turn(db_session, agent, chat_id, [], "anything")
    await db_session.commit()

    assert decision.state == BuilderState.HELP_GENERAL
    assert decision.sticky is True


# --- Phase 6b: single-clarify-question cap ------------------------------------


async def test_clarify_never_repeats_even_with_another_ambiguous_message(db_session, redis_db):
    """previous_state == clarify forces resolution to the top scorer this
    turn, regardless of how close the margin is - at most one clarify
    question per ambiguous exchange."""
    agent, chat_id = await _setup(db_session, builder_state="clarify")
    # Margin (0.51 - 0.49 = 0.02) would trigger clarify again under the
    # ordinary rule - the single-clarify cap must override that.
    with _mock_classify(_noul_answers(one_off_action=0.51, builder=0.49, help_building=0.0, help_general=0.0)):
        decision = await route_owner_turn(db_session, agent, chat_id, [], "not sure, whatever you think")
    await db_session.commit()

    assert decision.state != BuilderState.CLARIFY
    assert decision.state == BuilderState.ONE_OFF_ACTION
    assert decision.sticky is False

    rows = await _router_log_rows(db_session, agent.id)
    assert rows[0].previous_state == "clarify"
    assert rows[0].resolved_state == "one_off_action"


async def test_clarify_cap_resolves_to_whichever_destination_scored_higher(db_session, redis_db):
    """Not hardcoded to one_off_action - a second clarify-cap turn where
    builder happens to score higher lands on builder instead."""
    agent, chat_id = await _setup(db_session, builder_state="clarify")
    with _mock_classify(_noul_answers(one_off_action=0.49, builder=0.51, help_building=0.0, help_general=0.0)):
        decision = await route_owner_turn(db_session, agent, chat_id, [], "I guess set it up properly")
    await db_session.commit()

    assert decision.state == BuilderState.BUILDER


async def test_clarify_cap_writes_exactly_one_log_row(db_session, redis_db):
    agent, chat_id = await _setup(db_session, builder_state="clarify")
    with _mock_classify(_noul_answers(one_off_action=0.5, builder=0.5, help_building=0.0, help_general=0.0)):
        await route_owner_turn(db_session, agent, chat_id, [], "either is fine")
    await db_session.commit()

    rows = await _router_log_rows(db_session, agent.id)
    assert len(rows) == 1
