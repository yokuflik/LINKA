"""Real-execution tests for `_run_turn`'s supersede/mid-turn-interruption
logic (ADR 0063 / 00732 / 0075), AGENT_RUN_TURN_TEST_PLAN.md Step 5.

`_run_turn` has three separate supersede checkpoints: pre-call (top of the
round-trip loop, case A "merge for free" when no Gemini call has happened
yet), mid-call (inside `_generate_turn_or_supersede`'s 0.5s poll loop, case
B), and pre-send (right before a send_message/reply_message tool call
dispatches). None of the existing tests run these against a real loop -
`test_invoke_debounce.py` only unit-tests the Redis primitives
(`mark_superseded`/`is_superseded`/`arm_debounce_now`) in isolation.

As with every other `_run_turn` real-execution test file, only the Gemini
HTTP layer (`invoke_turn_helpers.generate_turn`) is mocked
(`_gemini_stub.mock_gemini_turn`) - DB session, tool dispatch, Redis
supersede/debounce state, and token accounting all run for real.

`_run_turn` itself never acquires/releases the turn lock (that's
`process_entry`'s job, in invoke_worker.py's stream consumer) - these tests
call `_run_turn` directly, same as every other file in this suite, and drive
the supersede flag directly via `mark_superseded`/`is_turn_running` rather
than going through a second concurrent `process_entry` call.
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from config import agent_settings
from infra.redis.client import redis_client
from modules.agents.invoke_debounce import mark_superseded
from modules.agents.invoke_worker import _run_turn
from modules.agents.judge import JudgeVerdict
from modules.agents.token_budget import peek_usage
from modules.messaging import service as message_service
from modules.messaging.common import AGENT_REPLY_MESSAGE_TYPE
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


async def _make_execution_setup(db_session):
    """Same shape as test_run_turn_execution_mode.py's helper - a real 1:1
    chat between the owner and a third-party customer, triggered by the
    customer's own message, so the turn runs in execution mode."""
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


async def test_pre_call_supersede_merges_and_still_replies(db_session, redis_db):
    """Case A (ADR 0075): superseded before any Gemini call has been made for
    this turn - the loop must not abort, it rebuilds contents from fresh
    history and keeps running the same turn, still delivering a real reply."""
    agent, _owner, target_chat, message_id = await _make_execution_setup(db_session)
    await mark_superseded(agent.id, target_chat)

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
    assert reply.type == AGENT_REPLY_MESSAGE_TYPE


async def test_mid_call_supersede_ends_turn_with_no_message_and_penalty(db_session, redis_db, monkeypatch):
    """Case B: the flag gets set while a Gemini call is genuinely in flight
    (mocked to sleep long enough for the test to mark it superseded mid-way),
    relying on _generate_turn_or_supersede's real poll loop to detect it and
    cancel. Turn must end via _TurnSuperseded: no message sent, the debounce
    ZSET re-armed to fire immediately, and the flat penalty charged since no
    real usageMetadata ever comes back from a cancelled call."""
    import modules.agents.invoke_turn_helpers as helpers_module

    monkeypatch.setattr(helpers_module, "_SUPERSEDE_POLL_SECONDS", 0.05)

    agent, _owner, target_chat, message_id = await _make_execution_setup(db_session)

    async def _slow_call(**kwargs):
        await asyncio.sleep(1.0)
        return text_result("too late")

    async def _mark_it_superseded_soon():
        await asyncio.sleep(0.15)
        await mark_superseded(agent.id, target_chat)

    with _mock_judge(), patch(
        "modules.agents.invoke_turn_helpers.generate_turn",
        new=AsyncMock(side_effect=_slow_call),
    ):
        await asyncio.gather(
            _run_turn(agent.id, target_chat, message_id),
            _mark_it_superseded_soon(),
        )

    messages = await get_chat_messages(db_session, chat_id=target_chat)
    assert len(messages) == 1
    assert messages[0].id == message_id  # no reply was ever sent

    score = await redis_client.zscore(
        agent_settings.AGENT_INVOKE_DEBOUNCE_ZSET_KEY, f"{agent.id}:{target_chat}"
    )
    assert score is not None  # arm_debounce_now re-armed the replacement fire

    usage = await peek_usage(agent.id)
    assert usage["5h"].used == agent_settings.AGENT_SUPERSEDED_CALL_TOKEN_PENALTY
    assert usage["7d"].used == agent_settings.AGENT_SUPERSEDED_CALL_TOKEN_PENALTY


async def test_pre_send_supersede_blocks_send_message_and_rearms(db_session, redis_db):
    """Right before send_message/reply_message dispatches, per ADR 00732's
    last checkpoint - marking superseded between the Gemini call resolving
    (function_call_result) and the tool actually executing must stop the
    send from ever reaching the target chat, and re-arm the debounce instead
    of falling through to a second round-trip."""
    agent, _owner, target_chat, message_id = await _make_execution_setup(db_session)

    call_result = function_call_result("send_message", {"chat_id": str(target_chat), "content": "should never send"})

    async def _return_then_mark_superseded(**kwargs):
        # Runs as the (fast, already-resolved) Gemini call inside
        # _generate_turn_or_supersede - marking it here lands the flag
        # before _run_turn's pre-send check on the very next await point.
        await mark_superseded(agent.id, target_chat)
        return call_result

    with _mock_judge(), patch(
        "modules.agents.invoke_turn_helpers.generate_turn",
        new=AsyncMock(side_effect=_return_then_mark_superseded),
    ):
        await _run_turn(agent.id, target_chat, message_id)

    messages = await get_chat_messages(db_session, chat_id=target_chat)
    assert len(messages) == 1
    assert messages[0].id == message_id  # send_message's handler never ran

    score = await redis_client.zscore(
        agent_settings.AGENT_INVOKE_DEBOUNCE_ZSET_KEY, f"{agent.id}:{target_chat}"
    )
    assert score is not None
