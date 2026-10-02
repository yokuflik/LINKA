"""ADR 0099: goal-driven conversational tasks (start_goal_task).

Real Postgres (ephemeral DB, ADR 0032) + real Redis (`redis_db`); Gemini is
never involved here - these cover the deterministic parts: spawn/conflict
rules, the code-level tool gate, the six termination paths, trigger wake-up.
"""
import time
from datetime import datetime, timedelta, timezone

import pytest

from config import settings
from infra.redis.client import redis_client
from modules.agents.goal_tasks import (
    GoalTaskConflictError,
    GoalTaskError,
    begin_goal_turn,
    build_goal_prompt,
    close_goal_task,
    find_goal_task_for_chat,
    record_turn_outcome,
    spawn_goal_task,
)
from modules.agents.invoke_debounce import due_pairs, pop_latest_message_id
from modules.agents.invoke_queue import enqueue_invocation
from modules.agents.tools import execute_tool_call, get_tool_schemas_for_chat
from modules.agents.tools.common import ToolDeniedError
from modules.agents.tools.goal_task_tools import _tool_cancel_goal_task, _tool_start_goal_task
from modules.agents.trigger_engine import evaluate_triggers
from modules.messaging.crud import create_message
from tests.modules.agents._factories import make_agent, make_chat, make_user, next_id

pytestmark = pytest.mark.asyncio


async def _setup(db_session):
    owner = await make_user(db_session)
    other = await make_user(db_session)
    owner_chat = await make_chat(db_session, owner)
    target_chat = await make_chat(db_session, owner, other)
    agent = await make_agent(db_session, owner, owner_chat)
    return agent, owner, other, owner_chat, target_chat


async def _spawn(db_session, agent, chat, **kw):
    return await spawn_goal_task(
        db_session, agent, chat_id=str(chat), goal=kw.pop("goal", "buy a chai auto"),
        done_when=kw.pop("done_when", "seller confirms price and pickup"), **kw,
    )


def _entry(agent, task_id):
    return agent.triggers["on_ephemeral_task"].get(task_id)


def _summary_entries(agent):
    return [e for e in agent.triggers.get("on_schedule", []) if str(e.get("id", "")).startswith("goal-summary-")]


# --- spawn -------------------------------------------------------------------

async def test_spawn_registers_entry_and_schedules_opener(db_session, redis_db):
    agent, _, _, _, target = await _setup(db_session)
    result = await _spawn(db_session, agent, target)

    entry = _entry(agent, result["task_id"])
    assert entry["mode"] == "converse" and entry["target_chat_id"] == str(target)
    assert entry["may_commit"] is False and entry["turns_used"] == 0
    openers = [e for e in agent.triggers["on_schedule"] if e["chat_id"] == str(target)]
    assert len(openers) == 1 and "scoped_system_prompt" not in openers[0]


async def test_spawn_rejects_second_task_on_same_chat(db_session, redis_db):
    agent, _, _, _, target = await _setup(db_session)
    await _spawn(db_session, agent, target)
    with pytest.raises(GoalTaskConflictError):
        await _spawn(db_session, agent, target)


async def test_spawn_rejects_owner_chat_and_empty_fields(db_session, redis_db):
    agent, _, _, owner_chat, target = await _setup(db_session)
    with pytest.raises(GoalTaskError):
        await _spawn(db_session, agent, owner_chat)
    with pytest.raises(GoalTaskError):
        await _spawn(db_session, agent, target, done_when="  ")


async def test_spawn_clamps_max_turns(db_session, redis_db):
    agent, _, _, _, target = await _setup(db_session)
    result = await _spawn(db_session, agent, target, max_turns=10_000)
    assert result["max_turns"] == settings.AGENT_GOAL_TASK_MAX_TURNS


async def test_expired_task_is_not_active(db_session, redis_db):
    agent, _, _, _, target = await _setup(db_session)
    result = await _spawn(db_session, agent, target)
    entry = _entry(agent, result["task_id"])
    entry["expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    assert find_goal_task_for_chat(agent.triggers, target) is None


# --- prompt: the model must know when to finish -----------------------------

async def test_prompt_states_goal_stop_condition_and_terminal_tools(db_session, redis_db):
    agent, _, _, _, target = await _setup(db_session)
    result = await _spawn(db_session, agent, target, constraints="max 500 NIS")
    prompt = build_goal_prompt({**result, "turns_used": 1})
    for needle in ("buy a chai auto", "seller confirms price and pickup", "max 500 NIS",
                   "complete_task", "fail_task", "turn 1 of", "ready_for_owner_confirmation"):
        assert needle in prompt


async def test_prompt_demands_finalization_on_last_turn_and_allows_commit_when_set(db_session, redis_db):
    agent, _, _, _, target = await _setup(db_session)
    result = await _spawn(db_session, agent, target, may_commit=True, max_turns=3)
    last = build_goal_prompt({**result, "turns_used": 3})
    assert "LAST turn" in last and "MUST call complete_task or fail_task" in last
    assert "explicitly allowed you to commit" in last


# --- tool gate ---------------------------------------------------------------

async def test_goal_turn_schemas_are_restricted(db_session, redis_db):
    agent, _, _, _, target = await _setup(db_session)
    await _spawn(db_session, agent, target)
    names = {s["name"] for s in get_tool_schemas_for_chat(agent, target)}
    assert names == {"send_message", "reply_message", "read_history", "complete_task", "fail_task"}


async def test_goal_turn_denies_other_tools_and_other_chats(db_session, redis_db):
    agent, _, _, _, target = await _setup(db_session)
    other_chat = await make_chat(db_session, agent.owner_user_id)
    await _spawn(db_session, agent, target)

    denied = await execute_tool_call(db_session, agent, "create_chat", {"target_user_id": 1}, chat_id=target)
    assert "not available" in denied["error"]

    off_target = await execute_tool_call(
        db_session, agent, "send_message", {"chat_id": str(other_chat), "content": "hi"}, chat_id=target
    )
    assert off_target["error"] == "this task may only act in its own chat"


async def test_goal_turn_can_send_in_its_own_chat(db_session, redis_db):
    agent, _, _, _, target = await _setup(db_session)
    await _spawn(db_session, agent, target)
    result = await execute_tool_call(
        db_session, agent, "send_message", {"chat_id": str(target), "content": "hi, is it available?"}, chat_id=target
    )
    assert "message_id" in result


async def test_no_goal_task_leaves_normal_execution_toolset(db_session, redis_db):
    agent, _, _, _, target = await _setup(db_session)
    names = {s["name"] for s in get_tool_schemas_for_chat(agent, target)}
    assert "create_chat" in names and "complete_task" not in names
    denied = await execute_tool_call(db_session, agent, "complete_task", {"outcome": "achieved", "summary": "x"}, chat_id=target)
    assert "not available" in denied["error"]


# --- termination paths -------------------------------------------------------

async def test_complete_task_closes_and_queues_one_owner_summary(db_session, redis_db):
    agent, _, _, owner_chat, target = await _setup(db_session)
    result = await _spawn(db_session, agent, target)
    out = await execute_tool_call(
        db_session, agent, "complete_task",
        {"outcome": "ready_for_owner_confirmation", "summary": "450 NIS, pickup Sunday"}, chat_id=target,
    )
    assert out == {"status": "task_closed"}
    assert _entry(agent, result["task_id"]) is None
    assert find_goal_task_for_chat(agent.triggers, target) is None
    summaries = _summary_entries(agent)
    assert len(summaries) == 1
    assert summaries[0]["chat_id"] == str(owner_chat)
    assert "450 NIS, pickup Sunday" in summaries[0]["scoped_system_prompt"]
    assert "ready_for_owner_confirmation" in summaries[0]["scoped_system_prompt"]


async def test_complete_task_rejects_bad_outcome_and_keeps_task(db_session, redis_db):
    agent, _, _, _, target = await _setup(db_session)
    result = await _spawn(db_session, agent, target)
    out = await execute_tool_call(db_session, agent, "complete_task", {"outcome": "whatever", "summary": ""}, chat_id=target)
    assert "outcome must be one of" in out["error"]
    assert _entry(agent, result["task_id"]) is not None


async def test_fail_task_closes_with_reason(db_session, redis_db):
    agent, _, _, _, target = await _setup(db_session)
    result = await _spawn(db_session, agent, target)
    await execute_tool_call(db_session, agent, "fail_task", {"reason": "cannot_achieve", "summary": "sold out"}, chat_id=target)
    assert _entry(agent, result["task_id"]) is None
    assert "cannot_achieve" in _summary_entries(agent)[0]["scoped_system_prompt"]


async def test_closing_twice_notifies_once(db_session, redis_db):
    agent, _, _, _, target = await _setup(db_session)
    result = await _spawn(db_session, agent, target)
    await close_goal_task(db_session, agent, result["task_id"], status="timed_out")
    await close_goal_task(db_session, agent, result["task_id"], status="timed_out")
    assert len(_summary_entries(agent)) == 1


async def test_turn_counter_and_last_turn_force_close(db_session, redis_db):
    agent, _, _, _, target = await _setup(db_session)
    result = await _spawn(db_session, agent, target, max_turns=2)
    task_id = result["task_id"]

    turn = await begin_goal_turn(db_session, agent, target)
    assert turn.entry["turns_used"] == 1
    await record_turn_outcome(db_session, agent, task_id, acted=True)
    assert _entry(agent, task_id) is not None

    turn = await begin_goal_turn(db_session, agent, target)
    assert turn.entry["turns_used"] == 2
    await record_turn_outcome(db_session, agent, task_id, acted=True)  # last turn, no terminal call
    assert _entry(agent, task_id) is None
    assert "Turn limit" in _summary_entries(agent)[0]["scoped_system_prompt"]


async def test_two_idle_turns_force_close(db_session, redis_db):
    agent, _, _, _, target = await _setup(db_session)
    result = await _spawn(db_session, agent, target)
    task_id = result["task_id"]
    await begin_goal_turn(db_session, agent, target)
    await record_turn_outcome(db_session, agent, task_id, acted=False)
    assert _entry(agent, task_id)["idle_turns"] == 1
    await begin_goal_turn(db_session, agent, target)
    await record_turn_outcome(db_session, agent, task_id, acted=False)
    assert _entry(agent, task_id) is None
    assert "no progress" in _summary_entries(agent)[0]["scoped_system_prompt"]


async def test_acting_resets_idle_counter(db_session, redis_db):
    agent, _, _, _, target = await _setup(db_session)
    result = await _spawn(db_session, agent, target)
    task_id = result["task_id"]
    await begin_goal_turn(db_session, agent, target)
    await record_turn_outcome(db_session, agent, task_id, acted=False)
    await begin_goal_turn(db_session, agent, target)
    await record_turn_outcome(db_session, agent, task_id, acted=True)
    assert _entry(agent, task_id)["idle_turns"] == 0


async def test_begin_turn_returns_none_without_task(db_session, redis_db):
    agent, _, _, _, target = await _setup(db_session)
    assert await begin_goal_turn(db_session, agent, target) is None


async def test_cancel_closes_task(db_session, redis_db):
    agent, _, _, _, target = await _setup(db_session)
    result = await _spawn(db_session, agent, target)
    out = await _tool_cancel_goal_task(db_session, agent, {"chat_id": str(target)})
    assert out == {"status": "cancelled"}
    assert _entry(agent, result["task_id"]) is None
    with pytest.raises(ToolDeniedError):
        await _tool_cancel_goal_task(db_session, agent, {"chat_id": str(target)})


async def test_start_tool_maps_conflict_to_tool_denied(db_session, redis_db):
    agent, _, _, _, target = await _setup(db_session)
    args = {"chat_id": str(target), "goal": "g", "done_when": "d"}
    await _tool_start_goal_task(db_session, agent, args)
    with pytest.raises(ToolDeniedError):
        await _tool_start_goal_task(db_session, agent, args)


# --- trigger engine wake-up --------------------------------------------------

async def test_reply_in_goal_chat_wakes_agent_without_any_other_trigger(db_session, redis_db):
    agent, _, other, _, target = await _setup(db_session)  # default triggers: nothing enabled
    await _spawn(db_session, agent, target)

    message = await create_message(
        db_session, message_id=next_id(), chat_id=target, sender_id=other, content="yes it is available", type=1
    )
    await db_session.commit()
    await evaluate_triggers(message)

    for agent_id, chat_id in await due_pairs(now=time.time() + 3600):
        message_id = await pop_latest_message_id(agent_id, chat_id)
        if message_id is not None:
            await enqueue_invocation(agent_id=agent_id, chat_id=chat_id, message_id=message_id)
    entries = [f for _i, f in await redis_client.xrange(settings.AGENT_INVOKE_STREAM_KEY)]
    assert len(entries) == 1 and int(entries[0]["message_id"]) == message.id


async def test_reply_in_other_chat_does_not_wake_agent(db_session, redis_db):
    agent, owner, other, _, target = await _setup(db_session)
    unrelated = await make_chat(db_session, owner, other)
    await _spawn(db_session, agent, target)

    message = await create_message(
        db_session, message_id=next_id(), chat_id=unrelated, sender_id=other, content="hello", type=1
    )
    await db_session.commit()
    await evaluate_triggers(message)
    assert await due_pairs(now=time.time() + 3600) == []


# --- _run_turn wiring (Gemini stubbed, everything else real) -----------------

from unittest.mock import AsyncMock, patch  # noqa: E402

from modules.agents.invoke_worker import _run_turn  # noqa: E402
from modules.agents.judge import JudgeVerdict  # noqa: E402
from modules.messaging import service as message_service  # noqa: E402
from modules.messaging.crud import get_chat_messages  # noqa: E402
from tests.modules.agents._gemini_stub import function_call_result, mock_gemini_turn, text_result  # noqa: E402


async def _reply_setup(db_session):
    agent, owner, other, owner_chat, target = await _setup(db_session)
    result = await _spawn(db_session, agent, target)
    reply = await message_service.process_outgoing(
        db_session, sender_id=other, chat_id=target, client_message_id="c1", content="450 NIS, ok?"
    )
    await db_session.commit()
    return agent, owner_chat, target, reply.id, result["task_id"]


def _judge():
    return patch(
        "modules.agents.invoke_worker.evaluate_message",
        new=AsyncMock(return_value=JudgeVerdict(True, "on-topic", is_follow_up=False)),
    )


async def test_run_turn_send_then_text_keeps_task_open_and_counts_turn(db_session, redis_db):
    agent, _, target, message_id, task_id = await _reply_setup(db_session)
    with _judge() as judge, mock_gemini_turn(
        function_call_result("send_message", {"chat_id": str(target), "content": "Can you do 400?"}),
        text_result("done"),
    ):
        await _run_turn(agent.id, target, message_id)

    assert judge.call_args.kwargs["goal_task_active"] is True
    await db_session.refresh(agent)
    entry = _entry(agent, task_id)
    assert entry["turns_used"] == 1 and entry["idle_turns"] == 0
    messages = await get_chat_messages(db_session, chat_id=target)
    assert messages[0].content == "Can you do 400?"


async def test_run_turn_complete_task_closes_task_and_ends_turn(db_session, redis_db):
    agent, owner_chat, target, message_id, task_id = await _reply_setup(db_session)
    with _judge(), mock_gemini_turn(
        function_call_result("complete_task", {"outcome": "achieved", "summary": "bought for 450"}),
    ):
        await _run_turn(agent.id, target, message_id)

    await db_session.refresh(agent)
    assert _entry(agent, task_id) is None
    summaries = _summary_entries(agent)
    assert len(summaries) == 1 and summaries[0]["chat_id"] == str(owner_chat)


async def test_run_turn_text_only_turns_force_close_after_idle_cap(db_session, redis_db):
    agent, _, target, message_id, task_id = await _reply_setup(db_session)
    for _ in range(settings.AGENT_GOAL_TASK_MAX_IDLE_TURNS):
        with _judge(), mock_gemini_turn(text_result("hmm")):
            await _run_turn(agent.id, target, message_id)
        await db_session.refresh(agent)

    assert _entry(agent, task_id) is None
    assert len(_summary_entries(agent)) == 1
