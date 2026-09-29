"""Attachment-relevance judge (ADR 0086) - modules/agents/attachment_judge.py.

Runs against the real ephemeral Postgres (ADR 0032) and real Redis
(`redis_db`), since evaluate_attachment_match reads/writes
AgentAttachmentJudgeLog and the rate limit is a real Redis fixed-window
counter. What's mocked is the TypeSafe `classify` call only - no real HTTP
call, no tokens spent.

Behavioral expectations encoded here (mirrors judge.py's own contract,
confirmed with the user this is a SEPARATE dedicated gate, not a reuse of
the message judge):

- A genuine match/no-match decision comes from jev's single `matches_request`
  Noul question, thresholded by ATTACHMENT_JUDGE_MATCH_THRESHOLD.
- Fail-open on any classify() technical failure (TypeSafeError, or a
  malformed response -> KeyError/TypeError/ValueError): is_approved=True.
- The dedicated attachment_judge_calls rate bucket being exceeded also fails
  open, and never shares budget with the message judge's agent_judge_calls
  bucket.
- No requester text to judge against (empty string) auto-approves without
  ever calling jev or consuming rate-limit budget.
- Every verdict (approved, rejected, or any fail-open variant) is logged to
  AgentAttachmentJudgeLog exactly once per call.
- evaluate_attachment_match never raises - hard fail-open by contract.
"""
import json
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from modules.agents.attachment_judge import evaluate_attachment_match
from modules.agents.models import Agent, AgentAttachmentJudgeLog, DEFAULT_AGENT_RESTRICTIONS, DEFAULT_AGENT_TRIGGERS
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


async def _log_rows(session: AsyncSession, agent_id: int) -> list[AgentAttachmentJudgeLog]:
    stmt = select(AgentAttachmentJudgeLog).where(AgentAttachmentJudgeLog.agent_id == agent_id)
    return (await session.execute(stmt)).scalars().all()


def _noul_answer(value: float) -> dict:
    return {"matches_request": {"type": "noul", "noul": value}}


def _mock_classify(answers: dict | None = None, exc: Exception | None = None):
    """Patches modules.agents.attachment_judge.classify (the name this module
    imported into its own namespace) - never the real typesafe_client
    function, so no HTTP call, no tokens, ever."""
    mock = AsyncMock(side_effect=exc) if exc else AsyncMock(return_value=answers)
    return patch("modules.agents.attachment_judge.classify", mock)


# --- Approval / rejection ------------------------------------------------------


async def test_match_approved_when_noul_above_threshold(db_session, redis_db):
    owner = await _make_user(db_session)
    customer = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, customer)
    target_chat = await _make_private_chat(db_session, owner, customer)
    agent = await _make_agent(db_session, owner, owner_agent_chat)

    with _mock_classify(_noul_answer(0.9)) as classify_mock:
        verdict = await evaluate_attachment_match(
            db_session, agent, target_chat, 111,
            requester_message="can you send me the price list?",
            caption="our current price list",
            filename="pricelist.pdf",
        )
        await db_session.commit()

    classify_mock.assert_awaited_once()
    assert verdict.is_approved is True

    rows = await _log_rows(db_session, agent.id)
    assert len(rows) == 1
    assert rows[0].is_approved is True
    assert rows[0].chat_id == target_chat
    assert rows[0].file_id == 111


async def test_match_rejected_when_noul_below_threshold(db_session, redis_db):
    owner = await _make_user(db_session)
    customer = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, customer)
    target_chat = await _make_private_chat(db_session, owner, customer)
    agent = await _make_agent(db_session, owner, owner_agent_chat)

    with _mock_classify(_noul_answer(0.1)):
        verdict = await evaluate_attachment_match(
            db_session, agent, target_chat, 222,
            requester_message="can you send me the price list?",
            caption="a photo of our office party",
            filename="IMG_2026.jpg",
        )
        await db_session.commit()

    assert verdict.is_approved is False
    assert "does not appear to match" in verdict.reason

    rows = await _log_rows(db_session, agent.id)
    assert len(rows) == 1
    assert rows[0].is_approved is False


# --- Fail-open paths ------------------------------------------------------------


async def test_typesafe_error_fails_open(db_session, redis_db):
    owner = await _make_user(db_session)
    customer = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, customer)
    target_chat = await _make_private_chat(db_session, owner, customer)
    agent = await _make_agent(db_session, owner, owner_agent_chat)

    with _mock_classify(exc=TypeSafeError("boom")):
        verdict = await evaluate_attachment_match(
            db_session, agent, target_chat, 333,
            requester_message="send me the brochure",
            caption="brochure",
            filename="brochure.pdf",
        )
        await db_session.commit()

    assert verdict.is_approved is True
    assert "failed open" in verdict.reason

    rows = await _log_rows(db_session, agent.id)
    assert len(rows) == 1
    assert rows[0].is_approved is True


@pytest.mark.parametrize("malformed", [{}, {"matches_request": {}}, {"matches_request": {"noul": "not-a-float"}}])
async def test_malformed_jev_response_fails_open(db_session, redis_db, malformed):
    owner = await _make_user(db_session)
    customer = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, customer)
    target_chat = await _make_private_chat(db_session, owner, customer)
    agent = await _make_agent(db_session, owner, owner_agent_chat)

    with _mock_classify(malformed):
        verdict = await evaluate_attachment_match(
            db_session, agent, target_chat, 444,
            requester_message="send me the brochure",
            caption="brochure",
            filename="brochure.pdf",
        )
        await db_session.commit()

    assert verdict.is_approved is True


async def test_rate_limit_exceeded_fails_open_without_calling_jev(db_session, redis_db):
    owner = await _make_user(db_session)
    customer = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, customer)
    target_chat = await _make_private_chat(db_session, owner, customer)
    agent = await _make_agent(db_session, owner, owner_agent_chat)

    for i in range(settings.ATTACHMENT_JUDGE_CALLS_PER_MINUTE):
        with _mock_classify(_noul_answer(0.9)):
            await evaluate_attachment_match(
                db_session, agent, target_chat, i,
                requester_message="send me the brochure",
                caption="brochure",
                filename="brochure.pdf",
            )
        await db_session.commit()

    with _mock_classify(_noul_answer(0.9)) as mock:
        verdict = await evaluate_attachment_match(
            db_session, agent, target_chat, 999,
            requester_message="one too many",
            caption="brochure",
            filename="brochure.pdf",
        )
        await db_session.commit()

    mock.assert_not_awaited()
    assert verdict.is_approved is True
    assert "rate limit" in verdict.reason

    rows = await _log_rows(db_session, agent.id)
    assert len(rows) == settings.ATTACHMENT_JUDGE_CALLS_PER_MINUTE + 1


async def test_empty_requester_text_auto_approves_without_calling_jev_or_rate_limit(db_session, redis_db):
    owner = await _make_user(db_session)
    customer = await _make_user(db_session)
    owner_agent_chat = await _make_private_chat(db_session, owner, customer)
    target_chat = await _make_private_chat(db_session, owner, customer)
    agent = await _make_agent(db_session, owner, owner_agent_chat)

    # Burn the whole rate-limit window first - if the empty-content path
    # still consumed budget, this would tip it into the fail-open-on-rate-
    # limit branch instead, whose reason string differs.
    for i in range(settings.ATTACHMENT_JUDGE_CALLS_PER_MINUTE):
        with _mock_classify(_noul_answer(0.9)):
            await evaluate_attachment_match(
                db_session, agent, target_chat, i,
                requester_message="send me the brochure",
                caption="brochure",
                filename="brochure.pdf",
            )
        await db_session.commit()

    with _mock_classify(_noul_answer(0.9)) as mock:
        verdict = await evaluate_attachment_match(
            db_session, agent, target_chat, 1000,
            requester_message="   ",
            caption="brochure",
            filename="brochure.pdf",
        )
        await db_session.commit()

    mock.assert_not_awaited()
    assert verdict.is_approved is True
    assert "no requester text" in verdict.reason
