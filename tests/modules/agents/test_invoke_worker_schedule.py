"""on_schedule firing (ADR 0046 decision 3) at the agent_worker layer -
modules/agents/invoke_worker.py's `_fire_schedule_entry`, `_schedule_poll_loop`,
and `process_entry`'s `kind="schedule"` branch.

Runs against the real ephemeral Postgres (ADR 0032) and real Redis
(`redis_db`) - the whole point of this layer is the interplay between the
`agent_schedule_due` ZSET, the live `Agent.triggers.on_schedule` row, and
`agent_invoke_stream`. Gemini is never called for real: `_run_turn` (or, at
the `process_entry` level, the coroutine it awaits) is always mocked, so no
tokens are spent and no real API dependency exists in this file.

Behavioral expectations encoded here (confirmed with the user, not just
"whatever the code does"):

- `_fire_schedule_entry` re-loads the live Agent row before firing (defense
  in depth for the enqueue-to-dequeue-style window between a poll tick and
  the fire) - a disabled or deleted agent's due member is dropped from the
  ZSET without enqueueing anything.
- A due entry that was removed or disabled since it was ZADD'd is likewise
  dropped without enqueueing - the ZSET can lag Postgres, Postgres is the
  source of truth.
- A live "recurring" entry that fires gets `enqueue_schedule_fire`'d exactly
  once, then rescheduled for its next daily occurrence (re-scored in the
  ZSET, not removed).
- A live "once" entry that fires gets `enqueue_schedule_fire`'d exactly once,
  then flipped to `enabled: false` in `Agent.triggers.on_schedule` (visible
  in the UI as fired, not silently deleted) and removed from the due ZSET so
  it never fires again.
- `_schedule_poll_loop` drains every currently-due member in one tick and
  stops (gracefully) once `stop_event` is set - it must not fire the same
  due member twice in one tick and must not crash the loop if one entry's
  firing raises.
- `process_entry`'s `kind="schedule"` branch re-validates the entry against
  the live Agent row before running a turn (same defense-in-depth pattern as
  the message-fired path re-checking `is_enabled`): a gone/disabled entry
  since enqueue is a no-op, never invoking `_run_turn`/Gemini.
- A schedule-fired turn still consumes the ordinary daily active-time budget
  and is still skipped entirely if that budget is already exhausted or the
  agent is disabled - no separate quota dimension for schedule-fired turns.
- A valid schedule-fired entry invokes `_run_turn` with the entry's
  `instruction` and (when set) `chat_id`, and with `message_id` absent
  (schedule-fired turns have no triggering message).
"""
import json
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from infra.redis.client import redis_client
from modules.agents.invoke_worker import (
    AgentInvokeConsumer,
    _fire_schedule_entry,
    _schedule_poll_loop,
)
from modules.agents.models import Agent, DEFAULT_AGENT_RESTRICTIONS, DEFAULT_AGENT_TRIGGERS
from modules.agents.schedule import sync_schedule_zset
from modules.chats.crud.crud_chat import create_chat
from modules.chats.crud.crud_participant import add_participant_to_chat
from modules.users.crud import create_user

pytestmark = pytest.mark.asyncio

_ID = 0


def _next_id() -> int:
    global _ID
    _ID += 1
    return 990_000_000 + _ID


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


def _recurring_entry(*, time_str: str = "09:00", enabled: bool = True, schedule_id: str, instruction: str = "say hi") -> dict:
    return {"id": schedule_id, "kind": "recurring", "time": time_str, "instruction": instruction, "enabled": enabled}


def _once_entry(*, at: str = "2026-06-01T12:00:00Z", enabled: bool = True, schedule_id: str, instruction: str = "send it", chat_id: int | None = None) -> dict:
    entry = {"id": schedule_id, "kind": "once", "at": at, "instruction": instruction, "enabled": enabled}
    if chat_id is not None:
        entry["chat_id"] = chat_id
    return entry


async def _make_agent(
    session: AsyncSession,
    owner_user_id: int,
    owner_agent_chat_id: int,
    *,
    is_enabled: bool = True,
    on_schedule: list | None = None,
) -> Agent:
    triggers = json.loads(json.dumps(DEFAULT_AGENT_TRIGGERS))
    triggers["on_schedule"] = on_schedule or []
    agent = Agent(
        id=_next_id(),
        owner_user_id=owner_user_id,
        owner_agent_chat_id=owner_agent_chat_id,
        is_enabled=is_enabled,
        triggers=triggers,
        restrictions=json.loads(json.dumps(DEFAULT_AGENT_RESTRICTIONS)),
    )
    session.add(agent)
    await session.flush()
    await session.commit()
    await sync_schedule_zset(agent)
    return agent


async def _stream_entries() -> list[dict]:
    entries = await redis_client.xrange(settings.AGENT_INVOKE_STREAM_KEY)
    return [fields for _id, fields in entries]


async def _zset_members_for(agent_id: int) -> set[str]:
    members = await redis_client.zrange(settings.AGENT_SCHEDULE_DUE_ZSET_KEY, 0, -1)
    prefix = f"{agent_id}:"
    return {m for m in members if m.startswith(prefix)}


# --- _fire_schedule_entry: happy paths ---------------------------------------

async def test_fire_recurring_entry_enqueues_and_reschedules_rather_than_removing(db_session, redis_db):
    owner = await _make_user(db_session)
    owner_chat = await _make_chat(db_session, owner)
    entry = _recurring_entry(schedule_id="s1", time_str="09:00")
    agent = await _make_agent(db_session, owner, owner_chat, on_schedule=[entry])

    await _fire_schedule_entry(db_session, agent.id, "s1")
    await db_session.commit()

    stream = await _stream_entries()
    assert len(stream) == 1
    assert int(stream[0]["agent_id"]) == agent.id
    assert stream[0]["schedule_id"] == "s1"
    assert stream[0]["kind"] == "schedule"
    # Recurring entries stay live and rescheduled, not removed.
    assert await _zset_members_for(agent.id) == {f"{agent.id}:s1"}


async def test_fire_once_entry_enqueues_then_disables_and_removes_from_zset(db_session, redis_db):
    owner = await _make_user(db_session)
    owner_chat = await _make_chat(db_session, owner)
    entry = _once_entry(schedule_id="s1")
    agent = await _make_agent(db_session, owner, owner_chat, on_schedule=[entry])

    await _fire_schedule_entry(db_session, agent.id, "s1")
    await db_session.commit()

    stream = await _stream_entries()
    assert len(stream) == 1
    assert stream[0]["schedule_id"] == "s1"
    # "once" entries are marked fired (visible, not deleted) and removed from
    # the due ZSET so they never fire again.
    assert await _zset_members_for(agent.id) == set()
    await db_session.refresh(agent)
    fired_entry = next(e for e in agent.triggers["on_schedule"] if e["id"] == "s1")
    assert fired_entry["enabled"] is False


# --- _fire_schedule_entry: stale/gone defense in depth -----------------------

async def test_fire_skips_and_evicts_when_agent_is_disabled(db_session, redis_db):
    owner = await _make_user(db_session)
    owner_chat = await _make_chat(db_session, owner)
    entry = _recurring_entry(schedule_id="s1")
    agent = await _make_agent(db_session, owner, owner_chat, is_enabled=False, on_schedule=[entry])
    # Force a ZSET member even though sync_schedule_zset would not have added
    # one for a disabled agent - simulates the entry having gone stale.
    await redis_client.zadd(settings.AGENT_SCHEDULE_DUE_ZSET_KEY, {f"{agent.id}:s1": 100.0})

    await _fire_schedule_entry(db_session, agent.id, "s1")
    await db_session.commit()

    assert await _stream_entries() == []
    assert await _zset_members_for(agent.id) == set()


async def test_fire_skips_and_evicts_when_agent_no_longer_exists(db_session, redis_db):
    fake_agent_id = _next_id()
    await redis_client.zadd(settings.AGENT_SCHEDULE_DUE_ZSET_KEY, {f"{fake_agent_id}:s1": 100.0})

    await _fire_schedule_entry(db_session, fake_agent_id, "s1")

    assert await _stream_entries() == []
    assert await _zset_members_for(fake_agent_id) == set()


async def test_fire_skips_and_evicts_when_entry_was_deleted_since_enqueue(db_session, redis_db):
    owner = await _make_user(db_session)
    owner_chat = await _make_chat(db_session, owner)
    agent = await _make_agent(db_session, owner, owner_chat, on_schedule=[])
    await redis_client.zadd(settings.AGENT_SCHEDULE_DUE_ZSET_KEY, {f"{agent.id}:gone": 100.0})

    await _fire_schedule_entry(db_session, agent.id, "gone")

    assert await _stream_entries() == []
    assert await _zset_members_for(agent.id) == set()


async def test_fire_skips_and_evicts_when_entry_was_disabled_since_enqueue(db_session, redis_db):
    owner = await _make_user(db_session)
    owner_chat = await _make_chat(db_session, owner)
    entry = _recurring_entry(schedule_id="s1", enabled=False)
    agent = await _make_agent(db_session, owner, owner_chat, on_schedule=[entry])
    await redis_client.zadd(settings.AGENT_SCHEDULE_DUE_ZSET_KEY, {f"{agent.id}:s1": 100.0})

    await _fire_schedule_entry(db_session, agent.id, "s1")

    assert await _stream_entries() == []
    assert await _zset_members_for(agent.id) == set()


# --- _schedule_poll_loop ------------------------------------------------------

async def test_poll_loop_fires_every_currently_due_member_in_one_tick(db_session, redis_db):
    owner_a = await _make_user(db_session)
    owner_b = await _make_user(db_session)
    chat_a = await _make_chat(db_session, owner_a)
    chat_b = await _make_chat(db_session, owner_b)
    entry_a = _recurring_entry(schedule_id="sa")
    entry_b = _once_entry(schedule_id="sb")
    agent_a = await _make_agent(db_session, owner_a, chat_a, on_schedule=[entry_a])
    agent_b = await _make_agent(db_session, owner_b, chat_b, on_schedule=[entry_b])
    # Force both members due right now regardless of their real next-fire score.
    await redis_client.zadd(
        settings.AGENT_SCHEDULE_DUE_ZSET_KEY,
        {f"{agent_a.id}:sa": 0.0, f"{agent_b.id}:sb": 0.0},
    )

    import asyncio
    from modules.agents import schedule as schedule_module

    stop_event = asyncio.Event()
    real_due_members = schedule_module.due_members

    async def _due_members_then_stop(*args, **kwargs):
        # Let exactly one iteration run the real lookup, then signal the loop
        # to exit after this iteration completes - stop_event.is_set() is
        # only checked at the top of the while loop, so setting it here (mid-
        # iteration) still lets the current tick's members finish firing.
        result = await real_due_members(*args, **kwargs)
        stop_event.set()
        return result

    with patch("modules.agents.invoke_worker.due_members", side_effect=_due_members_then_stop):
        await _schedule_poll_loop(stop_event)

    stream = await _stream_entries()
    fired_pairs = {(int(e["agent_id"]), e["schedule_id"]) for e in stream}
    assert fired_pairs == {(agent_a.id, "sa"), (agent_b.id, "sb")}


async def test_poll_loop_survives_one_entrys_firing_exception(db_session, redis_db):
    """A crash while firing one due member must not prevent the loop from
    completing its tick cleanly (matches the module's own try/except around
    each poll iteration) - it should not raise out of _schedule_poll_loop."""
    await redis_client.zadd(settings.AGENT_SCHEDULE_DUE_ZSET_KEY, {"999999999:broken": 0.0})

    import asyncio

    stop_event = asyncio.Event()

    async def _stop_after_first_tick(*_args, **_kwargs):
        stop_event.set()
        raise RuntimeError("boom")

    with patch("modules.agents.invoke_worker._fire_schedule_entry", side_effect=_stop_after_first_tick):
        await _schedule_poll_loop(stop_event)  # must not raise


# --- process_entry's kind="schedule" branch ----------------------------------

def _consumer() -> AgentInvokeConsumer:
    import asyncio
    return AgentInvokeConsumer("test-consumer", asyncio.Semaphore(10))


async def test_process_entry_runs_a_turn_for_a_live_schedule_entry(db_session, redis_db):
    owner = await _make_user(db_session)
    owner_chat = await _make_chat(db_session, owner)
    target_chat = await _make_chat(db_session, owner)
    entry = _recurring_entry(schedule_id="s1", instruction="check for unanswered messages")
    entry["chat_id"] = target_chat
    agent = await _make_agent(db_session, owner, owner_chat, on_schedule=[entry])

    with patch("modules.agents.invoke_worker._run_turn", new=AsyncMock()) as mock_run_turn:
        await _consumer().process_entry(
            db_session, {"agent_id": str(agent.id), "schedule_id": "s1", "kind": "schedule"}
        )

    mock_run_turn.assert_awaited_once()
    _args, kwargs = mock_run_turn.await_args
    called_args = mock_run_turn.await_args.args
    assert called_args[0] == agent.id
    assert called_args[1] == target_chat
    assert kwargs.get("schedule_instruction") == "check for unanswered messages"
    assert "message_id" not in kwargs


async def test_process_entry_runs_a_turn_with_no_chat_id_when_entry_has_none(db_session, redis_db):
    owner = await _make_user(db_session)
    owner_chat = await _make_chat(db_session, owner)
    entry = _recurring_entry(schedule_id="s1", instruction="summarize my day")
    agent = await _make_agent(db_session, owner, owner_chat, on_schedule=[entry])

    with patch("modules.agents.invoke_worker._run_turn", new=AsyncMock()) as mock_run_turn:
        await _consumer().process_entry(
            db_session, {"agent_id": str(agent.id), "schedule_id": "s1", "kind": "schedule"}
        )

    called_args = mock_run_turn.await_args.args
    assert called_args[0] == agent.id
    assert called_args[1] is None


async def test_process_entry_skips_a_schedule_entry_removed_since_enqueue(db_session, redis_db):
    owner = await _make_user(db_session)
    owner_chat = await _make_chat(db_session, owner)
    agent = await _make_agent(db_session, owner, owner_chat, on_schedule=[])

    with patch("modules.agents.invoke_worker._run_turn", new=AsyncMock()) as mock_run_turn:
        await _consumer().process_entry(
            db_session, {"agent_id": str(agent.id), "schedule_id": "gone", "kind": "schedule"}
        )

    mock_run_turn.assert_not_awaited()


async def test_process_entry_skips_a_schedule_entry_disabled_since_enqueue(db_session, redis_db):
    owner = await _make_user(db_session)
    owner_chat = await _make_chat(db_session, owner)
    entry = _recurring_entry(schedule_id="s1", enabled=False)
    agent = await _make_agent(db_session, owner, owner_chat, on_schedule=[entry])

    with patch("modules.agents.invoke_worker._run_turn", new=AsyncMock()) as mock_run_turn:
        await _consumer().process_entry(
            db_session, {"agent_id": str(agent.id), "schedule_id": "s1", "kind": "schedule"}
        )

    mock_run_turn.assert_not_awaited()


async def test_process_entry_skips_entirely_for_a_disabled_agent(db_session, redis_db):
    owner = await _make_user(db_session)
    owner_chat = await _make_chat(db_session, owner)
    entry = _recurring_entry(schedule_id="s1")
    agent = await _make_agent(db_session, owner, owner_chat, is_enabled=False, on_schedule=[entry])

    with patch("modules.agents.invoke_worker._run_turn", new=AsyncMock()) as mock_run_turn:
        await _consumer().process_entry(
            db_session, {"agent_id": str(agent.id), "schedule_id": "s1", "kind": "schedule"}
        )

    mock_run_turn.assert_not_awaited()


async def test_process_entry_skips_a_schedule_turn_when_daily_time_budget_exhausted(db_session, redis_db):
    owner = await _make_user(db_session)
    owner_chat = await _make_chat(db_session, owner)
    entry = _recurring_entry(schedule_id="s1")
    agent = await _make_agent(db_session, owner, owner_chat, on_schedule=[entry])

    with patch("modules.agents.invoke_worker.has_budget_remaining", new=AsyncMock(return_value=False)), \
         patch("modules.agents.invoke_worker._run_turn", new=AsyncMock()) as mock_run_turn:
        await _consumer().process_entry(
            db_session, {"agent_id": str(agent.id), "schedule_id": "s1", "kind": "schedule"}
        )

    mock_run_turn.assert_not_awaited()


async def test_process_entry_records_active_seconds_for_a_schedule_turn(db_session, redis_db):
    owner = await _make_user(db_session)
    owner_chat = await _make_chat(db_session, owner)
    entry = _recurring_entry(schedule_id="s1")
    agent = await _make_agent(db_session, owner, owner_chat, on_schedule=[entry])

    with patch("modules.agents.invoke_worker._run_turn", new=AsyncMock()), \
         patch("modules.agents.invoke_worker.record_active_seconds", new=AsyncMock()) as mock_record:
        await _consumer().process_entry(
            db_session, {"agent_id": str(agent.id), "schedule_id": "s1", "kind": "schedule"}
        )

    mock_record.assert_awaited_once()
    assert mock_record.await_args.args[0] == agent.id
