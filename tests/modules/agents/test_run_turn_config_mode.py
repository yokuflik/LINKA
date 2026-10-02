"""Real-execution tests for `_run_turn`'s config-mode branch
(AGENT_RUN_TURN_TEST_PLAN.md Step 3). Config-mode turns run in the owner's
own agent chat (chat_id == agent.owner_agent_chat_id), skip the judge
entirely, and post plain-text replies directly via _post_config_reply
instead of a send_message tool. Only the Gemini HTTP layer
(invoke_turn_helpers.generate_turn) is mocked via _gemini_stub.mock_gemini_turn
- the rest of _run_turn runs for real against the ephemeral test DB
(ADR 0032) and real Redis.
"""
from unittest.mock import AsyncMock, patch

import pytest

from modules.agents.builder_flow import BuilderState
from modules.agents.invoke_worker import _run_turn
from modules.agents.owner_chat_router import RouterDecision
from modules.messaging import service as message_service
from modules.messaging.common import AGENT_REPLY_MESSAGE_TYPE
from modules.messaging.crud import get_chat_messages
from tests.modules.agents._factories import make_agent, make_chat, make_user
from tests.modules.agents._gemini_stub import function_call_result, mock_gemini_turn, text_result

pytestmark = pytest.mark.asyncio


async def test_one_off_action_plain_text_reply_posted_to_owner_agent_chat(db_session, redis_db):
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
    # from one_off_action.
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


async def test_finish_building_agent_enables_without_touching_builder_state(db_session, redis_db):
    owner = await make_user(db_session)
    owner_chat = await make_chat(db_session, owner)
    # is_enabled deliberately True even before finishing: a not-yet-finished
    # Builder interview can still run turns (only finish_building_agent flips
    # is_enabled itself, per ADR 0049) - _run_turn returns immediately for a
    # disabled agent (invoke_worker.py's top-of-function guard), which would
    # make this test a no-op.
    agent = await make_agent(
        db_session, owner, owner_chat, is_enabled=True, builder_state=BuilderState.BUILDER.value
    )

    with mock_gemini_turn(
        function_call_result("finish_building_agent", {}),
        text_result("all set"),
    ):
        await _run_turn(agent.id, owner_chat)

    await db_session.refresh(agent)
    # ADR 0093 Phase 3 final shape: finish_building_agent no longer writes
    # builder_state itself (no message_id on this call, so the router never
    # runs either) - it stays whatever it already was.
    assert agent.builder_state == BuilderState.BUILDER.value
    assert agent.is_enabled is True

    messages = await get_chat_messages(db_session, chat_id=owner_chat)
    assert len(messages) == 1
    assert messages[0].content == "all set"
    assert messages[0].sender_agent_id == agent.id


# --- BuilderState.CLARIFY (ADR 0093 Phase 1) --------------------------------
# Unreachable via any real handoff tool yet (nothing sets builder_state to
# "clarify" until Phase 3's router lands) - these tests construct the agent
# directly in that state to exercise invoke_worker.py's special-case branch.
# It must never touch the normal generate_turn tool-calling path at all -
# mock_gemini_turn is deliberately NOT used here, so a real call would fail
# loudly (missing GEMINI_API_KEY / unmocked HTTP) rather than silently pass.


async def test_clarify_state_posts_the_generated_question_bypassing_the_normal_turn(db_session, redis_db):
    owner = await make_user(db_session)
    owner_chat = await make_chat(db_session, owner)
    agent = await make_agent(db_session, owner, owner_chat, builder_state=BuilderState.CLARIFY.value)
    triggering_message = await message_service.process_outgoing(
        db_session,
        sender_id=owner,
        chat_id=owner_chat,
        client_message_id="owner-msg-1",
        content="remind me every day at 9am to check stock",
    )
    await db_session.commit()

    with patch(
        "modules.agents.invoke_turn_pre.generate_clarify_question",
        new=AsyncMock(return_value="Just this once, or every day going forward?"),
    ) as mock_generate:
        await _run_turn(agent.id, owner_chat, triggering_message.id)

    mock_generate.assert_awaited_once()
    called_agent, called_text = mock_generate.await_args.args
    assert called_agent.id == agent.id
    assert called_text == "remind me every day at 9am to check stock"

    # The owner's own triggering message stays in the chat (unlike execution
    # mode's customer-sent message, this is the owner talking to their own
    # agent) plus the generated clarify reply - 2 messages total.
    messages = await get_chat_messages(db_session, chat_id=owner_chat)
    assert len(messages) == 2
    reply = next(m for m in messages if m.sender_agent_id == agent.id)
    assert reply.content == "Just this once, or every day going forward?"


async def test_clarify_state_does_not_change_builder_state(db_session, redis_db):
    owner = await make_user(db_session)
    owner_chat = await make_chat(db_session, owner)
    agent = await make_agent(db_session, owner, owner_chat, builder_state=BuilderState.CLARIFY.value)
    triggering_message = await message_service.process_outgoing(
        db_session,
        sender_id=owner,
        chat_id=owner_chat,
        client_message_id="owner-msg-1",
        content="send the update",
    )
    await db_session.commit()

    with patch(
        "modules.agents.invoke_turn_pre.generate_clarify_question",
        new=AsyncMock(return_value="Once, or ongoing?"),
    ):
        await _run_turn(agent.id, owner_chat, triggering_message.id)

    await db_session.refresh(agent)
    assert agent.builder_state == BuilderState.CLARIFY.value


# --- ADR 0093 Phase 3: route_owner_turn wiring -------------------------------
# route_owner_turn itself is unit-tested standalone in
# test_owner_chat_router.py; these tests cover only the wiring - that
# _run_turn actually calls it once per config-mode message-fired turn and
# persists whatever builder_state it returns before doing anything else.


async def test_router_decision_is_persisted_before_the_tool_calling_loop_runs(db_session, redis_db):
    owner = await make_user(db_session)
    owner_chat = await make_chat(db_session, owner)
    agent = await make_agent(db_session, owner, owner_chat, builder_state=BuilderState.ONE_OFF_ACTION.value)
    triggering_message = await message_service.process_outgoing(
        db_session,
        sender_id=owner,
        chat_id=owner_chat,
        client_message_id="owner-msg-1",
        content="I want to set up an agent that sells iPhones",
    )
    await db_session.commit()

    with (
        patch(
            "modules.agents.invoke_turn_pre.route_owner_turn",
            new=AsyncMock(return_value=RouterDecision(BuilderState.BUILDER, {"builder": 0.9}, 0.8)),
        ) as mock_route,
        mock_gemini_turn(text_result("let's set it up")),
    ):
        await _run_turn(agent.id, owner_chat, triggering_message.id)

    mock_route.assert_awaited_once()
    called_agent, called_chat_id = mock_route.await_args.args[1], mock_route.await_args.args[2]
    assert called_agent.id == agent.id
    assert called_chat_id == owner_chat
    assert mock_route.await_args.args[4] == "I want to set up an agent that sells iPhones"

    await db_session.refresh(agent)
    assert agent.builder_state == BuilderState.BUILDER.value

    messages = await get_chat_messages(db_session, chat_id=owner_chat)
    reply = next(m for m in messages if m.sender_agent_id == agent.id)
    assert reply.content == "let's set it up"


async def test_router_decision_of_clarify_bypasses_the_tool_calling_loop_in_the_same_turn(db_session, redis_db):
    """A router decision that lands on CLARIFY must take effect within the
    same turn - the pre-existing CLARIFY special-case branch (checked right
    after the router call) must see the freshly-updated builder_state, not
    the state the agent had when the turn started."""
    owner = await make_user(db_session)
    owner_chat = await make_chat(db_session, owner)
    agent = await make_agent(db_session, owner, owner_chat, builder_state=BuilderState.ONE_OFF_ACTION.value)
    triggering_message = await message_service.process_outgoing(
        db_session,
        sender_id=owner,
        chat_id=owner_chat,
        client_message_id="owner-msg-1",
        content="tomorrow at 12 send me a summary of the group",
    )
    await db_session.commit()

    with (
        patch(
            "modules.agents.invoke_turn_pre.route_owner_turn",
            new=AsyncMock(return_value=RouterDecision(BuilderState.CLARIFY, {"one_off_action": 0.55, "builder": 0.5}, 0.05)),
        ),
        patch(
            "modules.agents.invoke_turn_pre.generate_clarify_question",
            new=AsyncMock(return_value="Just this once, or every day going forward?"),
        ) as mock_generate,
    ):
        # mock_gemini_turn is deliberately NOT used here - if the router's
        # CLARIFY decision didn't take effect in-turn, the normal tool-calling
        # path would try a real (unmocked) Gemini call and fail loudly.
        await _run_turn(agent.id, owner_chat, triggering_message.id)

    mock_generate.assert_awaited_once()

    await db_session.refresh(agent)
    assert agent.builder_state == BuilderState.CLARIFY.value

    messages = await get_chat_messages(db_session, chat_id=owner_chat)
    reply = next(m for m in messages if m.sender_agent_id == agent.id)
    assert reply.content == "Just this once, or every day going forward?"


async def test_router_is_not_called_for_a_schedule_fired_turn(db_session, redis_db):
    """A schedule/knowledge-notice-fired turn has no message_id (no owner
    utterance to classify) and must keep whatever builder_state is already
    set rather than invoking the router at all."""
    owner = await make_user(db_session)
    owner_chat = await make_chat(db_session, owner)
    agent = await make_agent(db_session, owner, owner_chat, builder_state=BuilderState.ONE_OFF_ACTION.value)

    with (
        patch("modules.agents.invoke_turn_pre.route_owner_turn", new=AsyncMock()) as mock_route,
        mock_gemini_turn(text_result("done")),
    ):
        await _run_turn(agent.id, owner_chat, schedule_instruction="say hi")

    mock_route.assert_not_awaited()

    await db_session.refresh(agent)
    assert agent.builder_state == BuilderState.ONE_OFF_ACTION.value


async def test_max_tokens_partial_is_continued_up_to_cap(db_session, redis_db):
    # ADR 0102: a truncated config-mode reply is posted, then the model is
    # asked to continue; the final (STOP) part is posted as its own message.
    owner = await make_user(db_session)
    owner_chat = await make_chat(db_session, owner)
    agent = await make_agent(db_session, owner, owner_chat)

    with mock_gemini_turn(
        text_result("part one", finish_reason="MAX_TOKENS"),
        text_result("part two"),
    ):
        await _run_turn(agent.id, owner_chat)

    messages = await get_chat_messages(db_session, chat_id=owner_chat)
    assert sorted(m.content for m in messages) == ["part one", "part two"]


async def test_max_tokens_continuation_stops_at_cap(db_session, redis_db):
    from config import settings

    owner = await make_user(db_session)
    owner_chat = await make_chat(db_session, owner)
    agent = await make_agent(db_session, owner, owner_chat)

    cap = settings.AGENT_MAX_CONTINUATION_MESSAGES
    # cap continuations + the original = cap + 1 truncated results; the turn
    # must end after the last one instead of asking for another.
    results = [text_result(f"p{i}", finish_reason="MAX_TOKENS") for i in range(cap + 1)]
    with mock_gemini_turn(*results):
        await _run_turn(agent.id, owner_chat)

    messages = await get_chat_messages(db_session, chat_id=owner_chat)
    assert len(messages) == cap + 1
