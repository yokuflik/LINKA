"""Real-execution tests for `_run_turn`'s config-mode branch
(AGENT_RUN_TURN_TEST_PLAN.md Step 3). Config-mode turns run in the owner's
own agent chat (chat_id == agent.owner_agent_chat_id), skip the judge
entirely, and post plain-text replies directly via _post_config_reply
instead of a send_message tool. Only the Gemini HTTP layer
(invoke_turn_helpers.generate_turn) is mocked via _gemini_stub.mock_gemini_turn
- the rest of _run_turn runs for real against the ephemeral test DB
(ADR 0032) and real Redis.
"""
import pytest

from modules.agents.builder_flow import BuilderState
from modules.agents.invoke_worker import _run_turn
from modules.messaging.common import AGENT_REPLY_MESSAGE_TYPE
from modules.messaging.crud import get_chat_messages
from tests.modules.agents._factories import make_agent, make_chat, make_user
from tests.modules.agents._gemini_stub import function_call_result, mock_gemini_turn, text_result

pytestmark = pytest.mark.asyncio


async def test_supervisor_plain_text_reply_posted_to_owner_agent_chat(db_session, redis_db):
    owner = await make_user(db_session)
    owner_chat = await make_chat(db_session, owner)
    agent = await make_agent(db_session, owner, owner_chat)

    with mock_gemini_turn(text_result("hi there")):
        await _run_turn(agent.id, owner_chat)

    messages = await get_chat_messages(db_session, chat_id=owner_chat)
    assert len(messages) == 1
    reply = messages[0]
    assert reply.content == "hi there"
    assert reply.sender_agent_id == agent.id
    assert reply.type == AGENT_REPLY_MESSAGE_TYPE


async def test_empty_text_result_posts_fallback_notice(db_session, redis_db):
    # ADR 0089: a TurnResult whose content has no text parts (e.g. a
    # thought-signature-only response) used to silently no-op instead of
    # posting anything - always leave a trace now.
    owner = await make_user(db_session)
    owner_chat = await make_chat(db_session, owner)
    agent = await make_agent(db_session, owner, owner_chat)

    empty_result = text_result("")
    empty_result.content["parts"] = []

    with mock_gemini_turn(empty_result):
        await _run_turn(agent.id, owner_chat)

    messages = await get_chat_messages(db_session, chat_id=owner_chat)
    assert len(messages) == 1
    assert messages[0].content == "Sorry, something went wrong on my end. Could you say that again?"
    assert messages[0].sender_agent_id == agent.id


async def test_config_mode_tool_call_set_agent_persona_updates_row_and_posts_reply(db_session, redis_db):
    # set_agent_persona is only in the Builder state's tool set (CONFIG_TOOL_
    # HANDLERS, builder_handoff.py's BUILDER_STATE_HANDLERS) - unreachable
    # from Supervisor.
    owner = await make_user(db_session)
    owner_chat = await make_chat(db_session, owner)
    agent = await make_agent(db_session, owner, owner_chat, builder_state=BuilderState.BUILDER.value)

    with mock_gemini_turn(
        function_call_result("set_agent_persona", {"skill": "support_agent"}),
        text_result("done"),
    ):
        await _run_turn(agent.id, owner_chat)

    await db_session.refresh(agent)
    assert agent.active_skill == "support_agent"

    messages = await get_chat_messages(db_session, chat_id=owner_chat)
    assert len(messages) == 1
    assert messages[0].content == "done"
    assert messages[0].sender_agent_id == agent.id


async def test_no_reply_needed_posts_nothing(db_session, redis_db):
    owner = await make_user(db_session)
    owner_chat = await make_chat(db_session, owner)
    agent = await make_agent(db_session, owner, owner_chat)

    with mock_gemini_turn(function_call_result("no_reply_needed", {})):
        await _run_turn(agent.id, owner_chat)

    messages = await get_chat_messages(db_session, chat_id=owner_chat)
    assert len(messages) == 0


async def test_builder_to_supervisor_handoff_mid_turn_via_finish_building_agent(db_session, redis_db):
    owner = await make_user(db_session)
    owner_chat = await make_chat(db_session, owner)
    # is_enabled deliberately True even before the handoff: a not-yet-
    # finished Builder interview can still run turns (only
    # finish_building_agent flips is_enabled itself, per ADR 0049) - _run_turn
    # returns immediately for a disabled agent (invoke_worker.py's top-of-
    # function guard), which would make this test a no-op.
    agent = await make_agent(
        db_session, owner, owner_chat, is_enabled=True, builder_state=BuilderState.BUILDER.value
    )

    with mock_gemini_turn(
        function_call_result("finish_building_agent", {}),
        text_result("all set"),
    ):
        await _run_turn(agent.id, owner_chat)

    await db_session.refresh(agent)
    assert agent.builder_state == BuilderState.SUPERVISOR.value
    assert agent.is_enabled is True

    messages = await get_chat_messages(db_session, chat_id=owner_chat)
    assert len(messages) == 1
    assert messages[0].content == "all set"
    assert messages[0].sender_agent_id == agent.id
