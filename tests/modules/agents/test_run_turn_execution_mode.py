"""Real-execution tests for `_run_turn` (invoke_worker.py) - the tool-calling
loop itself, not just its callers (AGENT_RUN_TURN_TEST_PLAN.md). Every other
existing test file mocks `_run_turn` wholesale with `AsyncMock()`; here only
the Gemini HTTP layer (`invoke_turn_helpers.generate_turn`) is mocked via
`_gemini_stub.mock_gemini_turn`, so the rest of `_run_turn` - DB session,
tool dispatch, message persistence - runs for real against the ephemeral
test DB (ADR 0032) and real Redis.

Step 1 (this file) is a single smoke test proving the patch target/mechanics
actually work end to end. Step 2 (below) extends it with execution-mode
text-reply and tool-call turns - a real 1:1 chat where the agent is
triggered by an incoming message from a non-owner user (message_id set,
config_mode_turn=False). The judge (a real, separate Gemini-shaped call via
modules.agents.invoke_worker.evaluate_message) is mocked to an approved
verdict throughout Step 2 - real judge behavior is already covered by
test_judge.py and is out of scope here.
"""
from unittest.mock import AsyncMock, patch

import pytest

from config import agent_settings
from modules.agents.gemini_client import GeminiChatError
from modules.agents.invoke_notify import _ROUND_TRIP_CAP_NOTICE
from modules.agents.invoke_worker import _run_turn
from modules.agents.judge import JudgeVerdict
from modules.agents.token_budget import peek_usage
from modules.messaging import service as message_service
from modules.messaging.common import AGENT_REPLY_MESSAGE_TYPE
from modules.messaging.crud import get_chat_messages
from tests.modules.agents._factories import make_agent, make_chat, make_user
from tests.modules.agents._gemini_stub import function_call_result, mock_gemini_turn, text_result

pytestmark = pytest.mark.asyncio


async def test_config_mode_text_reply_is_posted_to_owner_agent_chat(db_session, redis_db):
    owner = await make_user(db_session)
    owner_chat = await make_chat(db_session, owner)
    agent = await make_agent(db_session, owner, owner_chat)

    with mock_gemini_turn(text_result("Hello owner")):
        await _run_turn(agent.id, owner_chat)

    messages = await get_chat_messages(db_session, chat_id=owner_chat)
    assert len(messages) == 1
    reply = messages[0]
    assert reply.content == "Hello owner"
    assert reply.sender_agent_id == agent.id
    assert reply.type == AGENT_REPLY_MESSAGE_TYPE


# --- Step 2: execution-mode text-reply and tool-call turns ------------------

_APPROVED_VERDICT = JudgeVerdict(True, "on-topic", is_follow_up=False)


def _mock_judge():
    return patch(
        "modules.agents.invoke_worker.evaluate_message",
        new=AsyncMock(return_value=_APPROVED_VERDICT),
    )


async def _make_execution_setup(db_session):
    """A real 1:1 chat between the owner and a third-party customer, plus the
    triggering message the customer sent - separate from the owner's own
    agent chat, so the turn runs in execution mode (is_config_mode returns
    False for any chat_id other than agent.owner_agent_chat_id)."""
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


async def test_execution_mode_plain_text_reply_posts_nothing(db_session, redis_db):
    agent, _owner, target_chat, message_id = await _make_execution_setup(db_session)

    with _mock_judge(), mock_gemini_turn(text_result("Sure, come on by!")):
        await _run_turn(agent.id, target_chat, message_id)

    messages = await get_chat_messages(db_session, chat_id=target_chat)
    # Execution-mode personas are expected to use send_message/reply_message
    # themselves - a plain text response with no tool call must not be
    # force-posted (invoke_worker.py only calls _post_config_reply for
    # config-mode turns).
    assert len(messages) == 1
    assert messages[0].id == message_id


async def test_execution_mode_single_send_message_tool_round_trip(db_session, redis_db):
    agent, owner, target_chat, message_id = await _make_execution_setup(db_session)

    with _mock_judge(), mock_gemini_turn(
        function_call_result("send_message", {"chat_id": str(target_chat), "content": "We're open until 6pm!"}),
        text_result("done"),
    ):
        await _run_turn(agent.id, target_chat, message_id)

    messages = await get_chat_messages(db_session, chat_id=target_chat)
    assert len(messages) == 2
    reply = messages[0]  # newest-first
    assert reply.content == "We're open until 6pm!"
    assert reply.sender_agent_id == agent.id
    assert reply.sender_id == owner
    assert reply.type == AGENT_REPLY_MESSAGE_TYPE


async def test_execution_mode_multi_round_trip_tool_chain_runs_tools_in_order(db_session, redis_db):
    agent, owner, target_chat, message_id = await _make_execution_setup(db_session)

    with _mock_judge(), mock_gemini_turn(
        function_call_result("read_history", {"chat_id": str(target_chat)}),
        function_call_result("send_message", {"chat_id": str(target_chat), "content": "Got it, thanks!"}),
        text_result("done"),
    ):
        await _run_turn(agent.id, target_chat, message_id)

    messages = await get_chat_messages(db_session, chat_id=target_chat)
    # Original customer message + the one send_message reply - read_history
    # has no side effect on the chat itself, only send_message does. A 3rd
    # Gemini call never happens because the final text_result ends the turn.
    assert len(messages) == 2
    reply = messages[0]  # newest-first
    assert reply.content == "Got it, thanks!"
    assert reply.sender_agent_id == agent.id


async def test_execution_mode_round_trip_cap_posts_notice_to_owner_chat(db_session, redis_db, monkeypatch):
    agent, owner, target_chat, message_id = await _make_execution_setup(db_session)
    monkeypatch.setattr(agent_settings, "AGENT_TURN_MAX_TOOL_ROUNDTRIPS", 1)

    always_call = function_call_result("read_history", {"chat_id": str(target_chat)})
    with _mock_judge(), mock_gemini_turn(always_call, always_call, always_call):
        await _run_turn(agent.id, target_chat, message_id)

    owner_chat_messages = await get_chat_messages(db_session, chat_id=agent.owner_agent_chat_id)
    assert len(owner_chat_messages) == 1
    notice = owner_chat_messages[0]
    assert notice.content == _ROUND_TRIP_CAP_NOTICE
    assert notice.sender_agent_id == agent.id

    # The cap notice goes to the owner's own agent chat, never the execution
    # chat the turn was actually serving.
    target_chat_messages = await get_chat_messages(db_session, chat_id=target_chat)
    assert len(target_chat_messages) == 1
    assert target_chat_messages[0].id == message_id


async def test_execution_mode_max_tokens_ends_turn_with_no_partial_text_but_records_usage(db_session, redis_db):
    agent, _owner, target_chat, message_id = await _make_execution_setup(db_session)

    with _mock_judge(), mock_gemini_turn(
        text_result("This got cut o", finish_reason="MAX_TOKENS", usage=(123, 456))
    ):
        await _run_turn(agent.id, target_chat, message_id)

    # Execution-mode turns never forward partial text to the real chat - only
    # config-mode turns show partial text on MAX_TOKENS.
    messages = await get_chat_messages(db_session, chat_id=target_chat)
    assert len(messages) == 1
    assert messages[0].id == message_id

    usage = await peek_usage(agent.id)
    assert usage["5h"].used == 123 + 456
    assert usage["7d"].used == 123 + 456


async def test_execution_mode_gemini_chat_error_posts_notice_and_returns_cleanly(db_session, redis_db):
    agent, _owner, target_chat, message_id = await _make_execution_setup(db_session)

    with _mock_judge(), mock_gemini_turn(GeminiChatError("boom")):
        await _run_turn(agent.id, target_chat, message_id)  # must not raise

    owner_chat_messages = await get_chat_messages(db_session, chat_id=agent.owner_agent_chat_id)
    assert len(owner_chat_messages) == 1
    assert owner_chat_messages[0].content == (
        "This took a bit too long to process. Please try again in a moment."
    )
    assert owner_chat_messages[0].sender_agent_id == agent.id
