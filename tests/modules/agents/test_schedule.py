"""on_schedule ZSET layer (ADR 0046 decision 3) - modules/agents/schedule.py.

Pure Redis-ZSET bookkeeping: keeping `agent_schedule_due` in lockstep with
`Agent.triggers.on_schedule`, computing next-fire timestamps, and the
poll-loop's read/write primitives (due_members/remove_due_member/
reschedule_recurring). No Gemini involved at all in this file - these
functions never run a turn, they only decide *when* one should be enqueued.

Behavioral expectations encoded here (confirmed with the user, not just
"whatever the code does"):

- sync_schedule_zset derives ZSET membership purely from
  Agent.triggers.on_schedule: enabled entries get a ZSET member scored by
  their next-fire timestamp; disabled entries, entries missing an id, and
  entries with unparseable/missing time data are excluded.
- A "recurring" entry's score is the next daily HH:MM occurrence (UTC) after
  now - today's slot if it hasn't passed yet, otherwise tomorrow's.
- A "once" entry's score is its exact ISO-8601 instant, whether in the past
  or future (due_members decides "due", not sync_schedule_zset).
- Re-syncing removes stale members for this agent: entries deleted from
  on_schedule, or flipped disabled, disappear from the ZSET. Sync never
  touches another agent's members.
- The entries cap (AGENT_MAX_SCHEDULE_ENTRIES) is enforced here too, in
  addition to the CRUD-layer guard (modules/agents/crud.py's
  _check_schedule_quota on the write path) - sync_schedule_zset must never
  ZADD more than the cap's worth of live members for one agent, as a
  defense-in-depth backstop against a row that somehow already has more
  entries than the cap allows (e.g. the cap lowered after the fact).
- due_members returns only members scored at-or-before "now" (default: real
  current time, or an explicit cutoff for deterministic tests).
- remove_due_member removes exactly that one agent+schedule_id member.
- reschedule_recurring re-scores a member for tomorrow's daily occurrence,
  it does not remove and re-add a fresh entry.
- next_daily_occurrence rolls to tomorrow when today's slot already passed,
  and stays on today when it hasn't.
"""
import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from config import settings
from infra.redis.client import redis_client
from modules.agents.models import Agent, DEFAULT_AGENT_RESTRICTIONS, DEFAULT_AGENT_TRIGGERS
from modules.agents.schedule import (
    due_members,
    next_daily_occurrence,
    remove_due_member,
    reschedule_recurring,
    sync_schedule_zset,
)

pytestmark = pytest.mark.asyncio

_ID = 0


def _next_id() -> int:
    global _ID
    _ID += 1
    return 970_000_000 + _ID


def _make_agent(*, triggers_on_schedule: list | None = None) -> Agent:
    triggers = json.loads(json.dumps(DEFAULT_AGENT_TRIGGERS))
    triggers["on_schedule"] = triggers_on_schedule or []
    return Agent(
        id=_next_id(),
        owner_user_id=_next_id(),
        owner_agent_chat_id=_next_id(),
        is_enabled=True,
        triggers=triggers,
        restrictions=json.loads(json.dumps(DEFAULT_AGENT_RESTRICTIONS)),
    )


def _recurring_entry(*, time_str: str, enabled: bool = True, schedule_id: str | None = None) -> dict:
    return {
        "id": schedule_id or uuid.uuid4().hex,
        "kind": "recurring",
        "time": time_str,
        "instruction": "say good morning",
        "enabled": enabled,
    }


def _once_entry(*, at: str, enabled: bool = True, schedule_id: str | None = None) -> dict:
    return {
        "id": schedule_id or uuid.uuid4().hex,
        "kind": "once",
        "at": at,
        "instruction": "send the reminder",
        "enabled": enabled,
    }


async def _zset_members_for(agent_id: int) -> set[str]:
    all_members = await redis_client.zrange(settings.AGENT_SCHEDULE_DUE_ZSET_KEY, 0, -1)
    prefix = f"{agent_id}:"
    return {m for m in all_members if m.startswith(prefix)}


# --- next_daily_occurrence ---------------------------------------------------

def test_next_daily_occurrence_stays_today_if_slot_not_yet_passed():
    now = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    result = next_daily_occurrence("14:30", after=now)
    assert result == datetime(2026, 1, 1, 14, 30, tzinfo=timezone.utc)


def test_next_daily_occurrence_rolls_to_tomorrow_if_slot_already_passed():
    now = datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)
    result = next_daily_occurrence("14:30", after=now)
    assert result == datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc)


def test_next_daily_occurrence_rolls_to_tomorrow_on_exact_match():
    now = datetime(2026, 1, 1, 14, 30, tzinfo=timezone.utc)
    result = next_daily_occurrence("14:30", after=now)
    assert result == datetime(2026, 1, 2, 14, 30, tzinfo=timezone.utc)


# --- sync_schedule_zset: basic membership ------------------------------------

async def test_sync_adds_a_live_recurring_entry(redis_db):
    agent = _make_agent(triggers_on_schedule=[_recurring_entry(time_str="09:00", schedule_id="s1")])
    await sync_schedule_zset(agent)

    members = await _zset_members_for(agent.id)
    assert members == {f"{agent.id}:s1"}


async def test_sync_adds_a_live_once_entry_scored_at_its_exact_instant(redis_db):
    at = "2026-06-01T12:00:00Z"
    agent = _make_agent(triggers_on_schedule=[_once_entry(at=at, schedule_id="s1")])
    await sync_schedule_zset(agent)

    score = await redis_client.zscore(settings.AGENT_SCHEDULE_DUE_ZSET_KEY, f"{agent.id}:s1")
    expected = datetime.fromisoformat(at.replace("Z", "+00:00")).timestamp()
    assert score == pytest.approx(expected)


async def test_sync_excludes_a_disabled_entry(redis_db):
    agent = _make_agent(triggers_on_schedule=[_recurring_entry(time_str="09:00", schedule_id="s1", enabled=False)])
    await sync_schedule_zset(agent)

    assert await _zset_members_for(agent.id) == set()


async def test_sync_excludes_an_entry_missing_an_id(redis_db):
    entry = _recurring_entry(time_str="09:00")
    del entry["id"]
    agent = _make_agent(triggers_on_schedule=[entry])
    await sync_schedule_zset(agent)

    assert await _zset_members_for(agent.id) == set()


async def test_sync_excludes_a_recurring_entry_missing_time(redis_db):
    entry = _recurring_entry(time_str="09:00", schedule_id="s1")
    del entry["time"]
    agent = _make_agent(triggers_on_schedule=[entry])
    await sync_schedule_zset(agent)

    assert await _zset_members_for(agent.id) == set()


async def test_sync_excludes_a_once_entry_with_unparseable_at(redis_db):
    agent = _make_agent(
        triggers_on_schedule=[_once_entry(at="not-a-real-timestamp", schedule_id="s1")]
    )
    await sync_schedule_zset(agent)

    assert await _zset_members_for(agent.id) == set()


async def test_sync_excludes_an_entry_with_an_unknown_kind(redis_db):
    entry = {"id": "s1", "kind": "monthly", "instruction": "x", "enabled": True}
    agent = _make_agent(triggers_on_schedule=[entry])
    await sync_schedule_zset(agent)

    assert await _zset_members_for(agent.id) == set()


async def test_sync_handles_multiple_live_entries_for_the_same_agent(redis_db):
    agent = _make_agent(
        triggers_on_schedule=[
            _recurring_entry(time_str="09:00", schedule_id="s1"),
            _once_entry(at="2026-06-01T12:00:00Z", schedule_id="s2"),
        ]
    )
    await sync_schedule_zset(agent)

    assert await _zset_members_for(agent.id) == {f"{agent.id}:s1", f"{agent.id}:s2"}


# --- sync_schedule_zset: re-sync removes stale members -----------------------

async def test_resync_removes_a_deleted_entrys_member(redis_db):
    agent = _make_agent(triggers_on_schedule=[_recurring_entry(time_str="09:00", schedule_id="s1")])
    await sync_schedule_zset(agent)
    assert await _zset_members_for(agent.id) == {f"{agent.id}:s1"}

    agent.triggers = {**agent.triggers, "on_schedule": []}
    await sync_schedule_zset(agent)

    assert await _zset_members_for(agent.id) == set()


async def test_resync_removes_a_now_disabled_entrys_member(redis_db):
    entry = _recurring_entry(time_str="09:00", schedule_id="s1")
    agent = _make_agent(triggers_on_schedule=[entry])
    await sync_schedule_zset(agent)
    assert await _zset_members_for(agent.id) == {f"{agent.id}:s1"}

    entry["enabled"] = False
    agent.triggers = {**agent.triggers, "on_schedule": [entry]}
    await sync_schedule_zset(agent)

    assert await _zset_members_for(agent.id) == set()


async def test_resync_never_touches_another_agents_members(redis_db):
    agent_a = _make_agent(triggers_on_schedule=[_recurring_entry(time_str="09:00", schedule_id="s1")])
    agent_b = _make_agent(triggers_on_schedule=[_recurring_entry(time_str="10:00", schedule_id="s1")])
    await sync_schedule_zset(agent_a)
    await sync_schedule_zset(agent_b)

    # Re-sync agent_a with an empty schedule - agent_b's identically-named
    # schedule_id "s1" must survive since ZSET members are agent-prefixed.
    agent_a.triggers = {**agent_a.triggers, "on_schedule": []}
    await sync_schedule_zset(agent_a)

    assert await _zset_members_for(agent_a.id) == set()
    assert await _zset_members_for(agent_b.id) == {f"{agent_b.id}:s1"}


async def test_resync_updates_an_existing_members_score(redis_db):
    entry = _recurring_entry(time_str="09:00", schedule_id="s1")
    agent = _make_agent(triggers_on_schedule=[entry])
    await sync_schedule_zset(agent)
    first_score = await redis_client.zscore(settings.AGENT_SCHEDULE_DUE_ZSET_KEY, f"{agent.id}:s1")

    entry["time"] = "23:45"
    agent.triggers = {**agent.triggers, "on_schedule": [entry]}
    await sync_schedule_zset(agent)
    second_score = await redis_client.zscore(settings.AGENT_SCHEDULE_DUE_ZSET_KEY, f"{agent.id}:s1")

    assert second_score != first_score


# --- sync_schedule_zset: entries cap (defense in depth) ----------------------

async def test_sync_never_exceeds_the_max_schedule_entries_cap(redis_db):
    """CRUD-layer writes (update_agent_config/update_agent_triggers) already
    reject a patch pushing on_schedule past AGENT_MAX_SCHEDULE_ENTRIES before
    it's ever persisted - but sync_schedule_zset must not blindly trust
    whatever is already on the row (e.g. the cap lowered after the fact, or
    a future write path that forgets the check). Feed it more entries than
    the cap allows and confirm at most the cap's worth become live members."""
    cap = settings.AGENT_MAX_SCHEDULE_ENTRIES
    entries = [
        _recurring_entry(time_str="09:00", schedule_id=f"s{i}") for i in range(cap + 5)
    ]
    agent = _make_agent(triggers_on_schedule=entries)
    await sync_schedule_zset(agent)

    members = await _zset_members_for(agent.id)
    assert len(members) <= cap


# --- due_members / remove_due_member / reschedule_recurring ------------------

async def test_due_members_returns_only_members_at_or_before_cutoff(redis_db):
    await redis_client.zadd(settings.AGENT_SCHEDULE_DUE_ZSET_KEY, {"1:past": 100.0, "1:future": 9_999_999_999.0})

    due = await due_members(now=200.0)

    assert due == ["1:past"]


async def test_due_members_includes_a_member_scored_exactly_at_cutoff(redis_db):
    await redis_client.zadd(settings.AGENT_SCHEDULE_DUE_ZSET_KEY, {"1:exact": 100.0})

    due = await due_members(now=100.0)

    assert due == ["1:exact"]


async def test_due_members_defaults_to_the_real_current_time(redis_db):
    await redis_client.zadd(
        settings.AGENT_SCHEDULE_DUE_ZSET_KEY,
        {"1:already-due": time.time() - 10, "1:not-yet": time.time() + 10_000},
    )

    due = await due_members()

    assert due == ["1:already-due"]


async def test_remove_due_member_removes_only_the_named_member(redis_db):
    await redis_client.zadd(
        settings.AGENT_SCHEDULE_DUE_ZSET_KEY, {"1:s1": 100.0, "1:s2": 200.0}
    )

    await remove_due_member(1, "s1")

    remaining = await redis_client.zrange(settings.AGENT_SCHEDULE_DUE_ZSET_KEY, 0, -1)
    assert remaining == ["1:s2"]


async def test_reschedule_recurring_rescores_for_tomorrow_rather_than_duplicating(redis_db):
    await redis_client.zadd(settings.AGENT_SCHEDULE_DUE_ZSET_KEY, {"1:s1": 100.0})

    await reschedule_recurring(1, "s1", "09:00")

    members = await redis_client.zrange(settings.AGENT_SCHEDULE_DUE_ZSET_KEY, 0, -1)
    assert members == ["1:s1"]
    new_score = await redis_client.zscore(settings.AGENT_SCHEDULE_DUE_ZSET_KEY, "1:s1")
    assert new_score > time.time()
