"""ADR 0096 integration coverage for the outcome-mismatch hook in `_run_turn`
(invoke_worker.py) - the two turn-ending branches (no-function-call,
round-trip cap) that call modules.agents.outcome_judge.evaluate_tool_outcome
when the turn ends right on top of an unresolved tool failure.

Only `modules.agents.invoke_worker.evaluate_tool_outcome` and
`modules.agents.invoke_worker.notify_outcome_mismatch` are mocked here (real
jev/Gemini behavior for the judge itself is covered by
test_outcome_judge.py) - everything else (DB session, tool dispatch, message
persistence) runs for real against the ephemeral test DB (ADR 0032), same
style as test_run_turn_execution_mode.py.
"""
from unittest.mock import AsyncMock, patch

import pytest

from modules.agents.invoke_worker import _run_turn
from modules.agents.judge import JudgeVerdict
from modules.agents.outcome_judge import OutcomeVerdict
from modules.messaging import service as message_service
from modules.messaging.crud import get_chat_messages
from tests.modules.agents._factories import make_agent, make_chat, make_user
from tests.modules.agents._gemini_stub import function_call_result, mock_gemini_turn, text_result

pytestmark = pytest.mark.asyncio

_APPROVED_VERDICT = JudgeVerdict(True, "on-topic", is_follow_up=False)


def _mock_judge():
    return patch(
        "modules.agents.invoke_worker.evaluate_message",
        new=AsyncMock(return_value=_APPROVED_VERDICT),
    )


def _mock_outcome_judge(is_mismatch: bool):
    return patch(
        "modules.agents.invoke_worker.evaluate_tool_outcome",
        new=AsyncMock(return_value=OutcomeVerdict(is_mismatch, "stubbed verdict")),
    )


def _mock_notify():
    return patch("modules.agents.invoke_worker.notify_outcome_mismatch", new=AsyncMock())


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
        content="Please send me the invoice",
    )
    await db_session.commit()
    return agent, owner, target_chat, triggering_message.id


# A send_message call guaranteed to fail with ToolDeniedError - targeting the
# agent's own config chat is rejected unconditionally (tools/execution.py).
def _failing_send_message(agent):
    return function_call_result(
        "send_message", {"chat_id": str(agent.owner_agent_chat_id), "content": "whoops"}
    )


async def test_turn_ending_on_unresolved_tool_error_triggers_mismatch_notice(db_session, redis_db):
    agent, _owner, target_chat, message_id = await _make_execution_setup(db_session)

    with _mock_judge(), _mock_outcome_judge(is_mismatch=True), _mock_notify() as notify_mock, mock_gemini_turn(
        _failing_send_message(agent),
        text_result("Sorry, I couldn't do that."),
    ):
        await _run_turn(agent.id, target_chat, message_id)

    notify_mock.assert_awaited_once()
    kwargs = notify_mock.await_args.kwargs
    assert kwargs["tool_name"] == "send_message"
    assert "cannot use send_message" in kwargs["tool_error"]
    assert kwargs["goal_text"] == "Please send me the invoice"


async def test_turn_ending_with_no_mismatch_verdict_does_not_notify(db_session, redis_db):
    agent, _owner, target_chat, message_id = await _make_execution_setup(db_session)

    with _mock_judge(), _mock_outcome_judge(is_mismatch=False), _mock_notify() as notify_mock, mock_gemini_turn(
        _failing_send_message(agent),
        text_result("Sorry, I couldn't do that."),
    ):
        await _run_turn(agent.id, target_chat, message_id)

    notify_mock.assert_not_awaited()


async def test_recovered_tool_error_never_reaches_outcome_check(db_session, redis_db):
    """A failure followed by a later SUCCESSFUL tool call in the same turn
    must never trigger the outcome check - the turn is not ending on top of
    an unresolved failure, it recovered."""
    agent, owner, target_chat, message_id = await _make_execution_setup(db_session)

    with _mock_judge(), _mock_outcome_judge(is_mismatch=True) as judge_mock, _mock_notify() as notify_mock, mock_gemini_turn(
        _failing_send_message(agent),
        function_call_result("send_message", {"chat_id": str(target_chat), "content": "Here you go!"}),
        text_result("done"),
    ):
        await _run_turn(agent.id, target_chat, message_id)

    judge_mock.assert_not_awaited()
    notify_mock.assert_not_awaited()

    messages = await get_chat_messages(db_session, chat_id=target_chat)
    assert any(m.content == "Here you go!" and m.sender_agent_id == agent.id for m in messages)


async def test_turn_with_no_tool_failure_never_calls_outcome_judge(db_session, redis_db):
    agent, _owner, target_chat, message_id = await _make_execution_setup(db_session)

    with _mock_judge(), _mock_outcome_judge(is_mismatch=True) as judge_mock, _mock_notify() as notify_mock, mock_gemini_turn(
        function_call_result("send_message", {"chat_id": str(target_chat), "content": "All good"}),
        text_result("done"),
    ):
        await _run_turn(agent.id, target_chat, message_id)

    judge_mock.assert_not_awaited()
    notify_mock.assert_not_awaited()


async def test_round_trip_cap_on_failing_call_triggers_mismatch_check(db_session, redis_db, monkeypatch):
    from config import agent_settings

    agent, _owner, target_chat, message_id = await _make_execution_setup(db_session)
    monkeypatch.setattr(agent_settings, "AGENT_TURN_MAX_TOOL_ROUNDTRIPS", 0)

    with _mock_judge(), _mock_outcome_judge(is_mismatch=True) as judge_mock, _mock_notify() as notify_mock, mock_gemini_turn(
        _failing_send_message(agent),
    ):
        await _run_turn(agent.id, target_chat, message_id)

    judge_mock.assert_awaited_once()
    kwargs = judge_mock.await_args.kwargs
    assert kwargs["tool_name"] == "send_message"
    assert kwargs["tool_error"] == "tool round-trip limit reached for this turn"
    notify_mock.assert_awaited_once()
