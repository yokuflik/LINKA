"""Tool-outcome-mismatch judge (ADR 0096) - modules/agents/outcome_judge.py.

Runs against the real ephemeral Postgres (ADR 0032) and real Redis
(`redis_db`), since evaluate_tool_outcome reads/writes AgentOutcomeJudgeLog
and the rate limit is a real Redis fixed-window counter. What's mocked is
the TypeSafe `classify` call and the Gemini `generate_structured` call only
- no real HTTP call, no tokens spent.

Behavioral expectations encoded here (mirrors attachment_judge.py's own
contract - a SEPARATE dedicated gate, own rate bucket):

- A genuine mismatch/no-mismatch decision comes from jev's single
  `matches_goal` Noul question, thresholded by
  AGENT_OUTCOME_JUDGE_MISMATCH_THRESHOLD.
- Fails open to SILENCE (is_mismatch=False), not "approved" - unlike every
  action-gating judge, a broken judge call here must never itself produce a
  notification.
- The dedicated agent_outcome_judge_calls rate bucket being exceeded also
  fails open to silence, never sharing budget with judge.py's or
  attachment_judge.py's buckets.
- Empty goal text auto-returns no-mismatch without ever calling jev or
  consuming rate-limit budget, and is NOT logged (unlike
  attachment_judge.py's empty-caption case) - there is always seed text for
  a real turn, so this is a defensive branch, not a frequent real path.
- Every real verdict (mismatch or not) is logged to AgentOutcomeJudgeLog
  exactly once; fail-open verdicts (rate limit, jev error) are not logged.
- evaluate_tool_outcome never raises - hard fail-open by contract.
- notify_outcome_mismatch falls back to LOCAL_OUTCOME_EXPLANATION on a
  Gemini generation failure, and never raises even if send_system_message
  itself fails.
"""
import json
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from modules.agents.models import Agent, AgentOutcomeJudgeLog, DEFAULT_AGENT_RESTRICTIONS, DEFAULT_AGENT_TRIGGERS
from modules.agents.outcome_judge import (
    LOCAL_OUTCOME_EXPLANATION,
    evaluate_tool_outcome,
    notify_outcome_mismatch,
)
from modules.agents.gemini_client import GeminiChatError
from modules.agents.typesafe_client import TypeSafeError
from modules.chats.crud.crud_chat import create_chat
from modules.chats.crud.crud_participant import add_participant_to_chat
from modules.users.crud import create_user

pytestmark = pytest.mark.asyncio

_ID = 0


def _next_id() -> int:
    global _ID
    _ID += 1
    return 970_000_000 + _ID


async def _make_user(session: AsyncSession) -> int:
    user_id = _next_id()
    await create_user(session, user_id=user_id, phone_number=f"+1555{user_id}")
    return user_id


async def _make_private_chat(session: AsyncSession, user_a: int, user_b: int) -> int:
    chat_id = _next_id()
    await create_chat(session, chat_id=chat_id, is_group=False)
    await add_participant_to_chat(session, chat_id=chat_id, user_id=user_a)
    await add_participant_to_chat(session, chat_id=chat_id, user_id=user_b)
    return chat_id


async def _make_agent(session: AsyncSession, owner_user_id: int, owner_agent_chat_id: int) -> Agent:
    agent = Agent(
        id=_next_id(),
        owner_user_id=owner_user_id,
        owner_agent_chat_id=owner_agent_chat_id,
        is_enabled=True,
        active_skill="support_agent",
        triggers=json.loads(json.dumps(DEFAULT_AGENT_TRIGGERS)),
        restrictions=json.loads(json.dumps(DEFAULT_AGENT_RESTRICTIONS)),
    )
    session.add(agent)
    await session.flush()
    await session.commit()
    return agent


async def _log_rows(session: AsyncSession, agent_id: int) -> list[AgentOutcomeJudgeLog]:
    stmt = select(AgentOutcomeJudgeLog).where(AgentOutcomeJudgeLog.agent_id == agent_id)
    return (await session.execute(stmt)).scalars().all()


def _noul_answer(value: float) -> dict:
    return {"matches_goal": {"type": "noul", "noul": value}}


def _mock_classify(answers: dict | None = None, exc: Exception | None = None):
    mock = AsyncMock(side_effect=exc) if exc else AsyncMock(return_value=answers)
    return patch("modules.agents.outcome_judge.classify", mock)


def _mock_generate_structured(result: dict | None = None, exc: Exception | None = None):
    mock = AsyncMock(side_effect=exc) if exc else AsyncMock(return_value=result)
    return patch("modules.agents.outcome_judge.generate_structured", mock)


# --- Mismatch / no-mismatch -----------------------------------------------------


async def test_mismatch_flagged_when_noul_above_threshold(db_session, redis_db):
    owner = await _make_user(db_session)
    customer = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, customer)
    target_chat = await _make_private_chat(db_session, owner, customer)
    agent = await _make_agent(db_session, owner, owner_agent_chat)

    with _mock_classify(_noul_answer(0.9)) as classify_mock:
        verdict = await evaluate_tool_outcome(
            db_session, agent, target_chat,
            goal_text="send the invoice to the customer",
            tool_name="send_attached_file",
            tool_error="file not found",
        )
        await db_session.commit()

    classify_mock.assert_awaited_once()
    assert verdict.is_mismatch is True

    rows = await _log_rows(db_session, agent.id)
    assert len(rows) == 1
    assert rows[0].is_match is False
    assert rows[0].chat_id == target_chat
    assert rows[0].tool_name == "send_attached_file"


async def test_no_mismatch_when_noul_below_threshold(db_session, redis_db):
    owner = await _make_user(db_session)
    customer = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, customer)
    target_chat = await _make_private_chat(db_session, owner, customer)
    agent = await _make_agent(db_session, owner, owner_agent_chat)

    with _mock_classify(_noul_answer(0.1)):
        verdict = await evaluate_tool_outcome(
            db_session, agent, target_chat,
            goal_text="what's the weather like",
            tool_name="some_minor_tool",
            tool_error="cosmetic validation error",
        )
        await db_session.commit()

    assert verdict.is_mismatch is False

    rows = await _log_rows(db_session, agent.id)
    assert len(rows) == 1
    assert rows[0].is_match is True


# --- Fail-open paths (to silence) ------------------------------------------------


async def test_typesafe_error_fails_open_to_silence_and_not_logged(db_session, redis_db):
    owner = await _make_user(db_session)
    customer = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, customer)
    target_chat = await _make_private_chat(db_session, owner, customer)
    agent = await _make_agent(db_session, owner, owner_agent_chat)

    with _mock_classify(exc=TypeSafeError("boom")):
        verdict = await evaluate_tool_outcome(
            db_session, agent, target_chat,
            goal_text="book a table for tonight",
            tool_name="schedule_one_off_task",
            tool_error="boom",
        )
        await db_session.commit()

    assert verdict.is_mismatch is False
    assert "failed open" in verdict.reason

    rows = await _log_rows(db_session, agent.id)
    assert len(rows) == 0


@pytest.mark.parametrize("malformed", [{}, {"matches_goal": {}}, {"matches_goal": {"noul": "not-a-float"}}])
async def test_malformed_jev_response_fails_open_to_silence(db_session, redis_db, malformed):
    owner = await _make_user(db_session)
    customer = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, customer)
    target_chat = await _make_private_chat(db_session, owner, customer)
    agent = await _make_agent(db_session, owner, owner_agent_chat)

    with _mock_classify(malformed):
        verdict = await evaluate_tool_outcome(
            db_session, agent, target_chat,
            goal_text="book a table for tonight",
            tool_name="schedule_one_off_task",
            tool_error="weird failure",
        )
        await db_session.commit()

    assert verdict.is_mismatch is False


async def test_rate_limit_exceeded_fails_open_without_calling_jev(db_session, redis_db):
    owner = await _make_user(db_session)
    customer = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, customer)
    target_chat = await _make_private_chat(db_session, owner, customer)
    agent = await _make_agent(db_session, owner, owner_agent_chat)

    for i in range(settings.AGENT_OUTCOME_JUDGE_CALLS_PER_MINUTE):
        with _mock_classify(_noul_answer(0.1)):
            await evaluate_tool_outcome(
                db_session, agent, target_chat,
                goal_text=f"goal {i}", tool_name="tool", tool_error="err",
            )
        await db_session.commit()

    with _mock_classify(_noul_answer(0.9)) as mock:
        verdict = await evaluate_tool_outcome(
            db_session, agent, target_chat,
            goal_text="one too many", tool_name="tool", tool_error="err",
        )
        await db_session.commit()

    mock.assert_not_awaited()
    assert verdict.is_mismatch is False
    assert "rate limit" in verdict.reason

    rows = await _log_rows(db_session, agent.id)
    assert len(rows) == settings.AGENT_OUTCOME_JUDGE_CALLS_PER_MINUTE


async def test_empty_goal_text_returns_no_mismatch_without_calling_jev_or_rate_limit(db_session, redis_db):
    owner = await _make_user(db_session)
    customer = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, customer)
    target_chat = await _make_private_chat(db_session, owner, customer)
    agent = await _make_agent(db_session, owner, owner_agent_chat)

    with _mock_classify(_noul_answer(0.9)) as mock:
        verdict = await evaluate_tool_outcome(
            db_session, agent, target_chat,
            goal_text="   ", tool_name="tool", tool_error="err",
        )
        await db_session.commit()

    mock.assert_not_awaited()
    assert verdict.is_mismatch is False

    rows = await _log_rows(db_session, agent.id)
    assert len(rows) == 0


# --- Notification -----------------------------------------------------------------


async def test_notify_uses_generated_explanation(db_session, redis_db):
    owner = await _make_user(db_session)
    customer = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, customer)
    agent = await _make_agent(db_session, owner, owner_agent_chat)

    with _mock_generate_structured({"explanation": "I couldn't send the file you asked for."}):
        await notify_outcome_mismatch(
            db_session, agent,
            goal_text="send the invoice", tool_name="send_attached_file", tool_error="file not found",
        )
        await db_session.commit()

    from modules.messaging.read_api import get_message_history

    history = await get_message_history(db_session, owner, owner_agent_chat, limit=10)
    assert any("I couldn't send the file you asked for." in (m.content or "") for m in history)


async def test_notify_falls_back_to_local_template_on_generation_failure(db_session, redis_db):
    owner = await _make_user(db_session)
    customer = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, customer)
    agent = await _make_agent(db_session, owner, owner_agent_chat)

    with _mock_generate_structured(exc=GeminiChatError("boom")):
        await notify_outcome_mismatch(
            db_session, agent,
            goal_text="send the invoice", tool_name="send_attached_file", tool_error="file not found",
        )
        await db_session.commit()

    expected = LOCAL_OUTCOME_EXPLANATION.format(tool_name="send_attached_file", tool_error="file not found")

    from modules.messaging.read_api import get_message_history

    history = await get_message_history(db_session, owner, owner_agent_chat, limit=10)
    assert any(expected in (m.content or "") for m in history)
