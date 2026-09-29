"""Real-execution tests for `_run_turn`'s budget interplay (AGENT_RUN_TURN_TEST_PLAN.md
Step 4) - token-usage window exhaustion, the per-minute Gemini call budget
retry path, and the daily active-time budget accounting - the parts of
invoke_worker.py least likely to have ever been exercised end to end, since
every other test file mocks `_run_turn` itself. Only the Gemini HTTP layer
(`invoke_turn_helpers.generate_turn`) is mocked here via
`_gemini_stub.mock_gemini_turn`; token/time budget accounting runs for real
against real Redis.
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from config import agent_settings
from infra.redis.client import redis_client
from modules.agents.gemini_client import TurnResult, TurnUsage
from modules.agents.invoke_worker import AgentInvokeConsumer, _run_turn
from modules.agents.judge import JudgeVerdict
from modules.agents.time_budget import record_active_seconds
from modules.agents.token_budget import _key as _token_key
from modules.agents.token_budget import peek_usage
from modules.messaging import service as message_service
from modules.messaging.crud import get_chat_messages
from tests.modules.agents._factories import make_agent, make_chat, make_user
from tests.modules.agents._gemini_stub import mock_gemini_turn, text_result

pytestmark = pytest.mark.asyncio

_APPROVED_VERDICT = JudgeVerdict(True, "on-topic", is_follow_up=False)


def _mock_judge():
    return patch(
        "modules.agents.invoke_worker.evaluate_message",
        new=AsyncMock(return_value=_APPROVED_VERDICT),
    )


async def _make_execution_setup(db_session):
    owner = await make_user(db_session)
    owner_chat = await make_chat(db_session, owner)
    agent = await make_agent(db_session, owner, owner_chat)
    customer = await make_user(db_session)
    target_chat = await make_chat(db_session, owner, customer)
    triggering_message = await message_service.process_outgoing(
        db_session,
        sender_id=customer,
        chat_id=target_chat,
        client_message_id="customer-msg-1",
        content="Hi, are you open today?",
    )
    await db_session.commit()
    return agent, owner, target_chat, triggering_message.id


def _consumer() -> AgentInvokeConsumer:
    return AgentInvokeConsumer("test-consumer", asyncio.Semaphore(10))


# --- 1. Token window exhaustion notice --------------------------------------

async def test_token_window_exhaustion_posts_notice_once(db_session, redis_db):
    agent, _owner, target_chat, message_id = await _make_execution_setup(db_session)

    # Pre-seed the 5h window right up to the limit, one token short.
    await redis_client.set(_token_key(agent.id, "5h"), agent_settings.AGENT_TOKEN_BUDGET_5H - 1)

    with _mock_judge(), mock_gemini_turn(text_result("Sure, come on by!", usage=(1, 0))):
        await _run_turn(agent.id, target_chat, message_id)

    usage = await peek_usage(agent.id)
    assert usage["5h"].is_blocked

    owner_chat_messages = await get_chat_messages(db_session, chat_id=agent.owner_agent_chat_id)
    assert len(owner_chat_messages) == 1
    assert "token budget" in owner_chat_messages[0].content

    # A second turn that also tips the window must not double-notify (cooldown).
    with _mock_judge(), mock_gemini_turn(text_result("Still open!", usage=(1, 0))):
        await _run_turn(agent.id, target_chat, message_id)

    owner_chat_messages = await get_chat_messages(db_session, chat_id=agent.owner_agent_chat_id)
    assert len(owner_chat_messages) == 1


# --- 2. Per-minute Gemini call budget retry path ----------------------------

async def test_gemini_call_budget_retries_then_succeeds_once_freed(db_session, redis_db, monkeypatch):
    agent, _owner, target_chat, message_id = await _make_execution_setup(db_session)
    monkeypatch.setattr(agent_settings, "AGENT_GEMINI_BUDGET_RETRY_SECONDS", 0.01)
    monkeypatch.setattr(agent_settings, "AGENT_GEMINI_BUDGET_MAX_RETRIES", 3)

    # Exhaust the per-minute call budget up front.
    for _ in range(agent_settings.AGENT_GEMINI_CALLS_PER_MINUTE):
        await redis_client.incr(f"ratelimit:agent_gemini_calls:{agent.id}")

    async def _free_budget_after_first_retry():
        await redis_client.delete(f"ratelimit:agent_gemini_calls:{agent.id}")

    # Free the budget as soon as the first retry sleep happens, so the
    # in-place retry loop recovers before exhausting its retries.
    real_sleep = asyncio.sleep

    async def _sleep_and_free(seconds):
        await real_sleep(seconds)
        await _free_budget_after_first_retry()

    with _mock_judge(), mock_gemini_turn(text_result("Sure, come on by!")), \
         patch("modules.agents.invoke_turn_helpers.asyncio.sleep", new=AsyncMock(side_effect=_sleep_and_free)):
        await _run_turn(agent.id, target_chat, message_id)

    messages = await get_chat_messages(db_session, chat_id=target_chat)
    # No tool call scripted - a plain text response in execution mode is
    # never force-posted, so only the original customer message is present;
    # what matters here is that the turn proceeded (no "handling a lot of
    # requests" notice) rather than giving up.
    assert len(messages) == 1
    owner_chat_messages = await get_chat_messages(db_session, chat_id=agent.owner_agent_chat_id)
    assert len(owner_chat_messages) == 0


async def test_gemini_call_budget_exhausted_after_all_retries_posts_notice(db_session, redis_db, monkeypatch):
    agent, _owner, target_chat, message_id = await _make_execution_setup(db_session)
    monkeypatch.setattr(agent_settings, "AGENT_GEMINI_BUDGET_RETRY_SECONDS", 0.01)
    monkeypatch.setattr(agent_settings, "AGENT_GEMINI_BUDGET_MAX_RETRIES", 2)

    for _ in range(agent_settings.AGENT_GEMINI_CALLS_PER_MINUTE):
        await redis_client.incr(f"ratelimit:agent_gemini_calls:{agent.id}")

    with _mock_judge(), mock_gemini_turn(text_result("never reached")):
        await _run_turn(agent.id, target_chat, message_id)

    owner_chat_messages = await get_chat_messages(db_session, chat_id=agent.owner_agent_chat_id)
    assert len(owner_chat_messages) == 1
    assert owner_chat_messages[0].content == (
        "Your agent is handling a lot of requests right now. Please try again in a moment."
    )


# --- 3. Daily active-time budget accounted in `finally` ---------------------

async def test_process_entry_records_real_active_seconds_for_a_message_turn(db_session, redis_db):
    agent, _owner, target_chat, message_id = await _make_execution_setup(db_session)

    async def _slow_text_result(**_kwargs):
        await asyncio.sleep(0.05)
        return TurnResult(
            content={"role": "model", "parts": [{"text": "Sure, come on by!"}]},
            finish_reason="STOP",
            usage=TurnUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )

    with _mock_judge(), \
         patch("modules.agents.invoke_turn_helpers.generate_turn", new=AsyncMock(side_effect=_slow_text_result)), \
         patch("modules.agents.invoke_worker.record_active_seconds", new=AsyncMock(side_effect=record_active_seconds)) as mock_record:
        await _consumer().process_entry(
            db_session,
            {"agent_id": str(agent.id), "chat_id": str(target_chat), "message_id": str(message_id)},
        )

    mock_record.assert_awaited_once()
    called_agent_id, elapsed = mock_record.await_args.args
    assert called_agent_id == agent.id
    # Real elapsed wall time from the (mocked-Gemini, real-everything-else)
    # turn, not the ~instant duration a fully-mocked `_run_turn` would report.
    assert elapsed >= 0.05
