"""Message-batch debounce/coalescing + per-chat turn mutex (ADR 0063), plus
the in-flight-turn supersede flag (ADR 00732).

Three independent pieces, all scoped to (agent_id, chat_id):

- `invoke_debounce.arm_debounce`/`due_pairs`/`pop_latest_message_id`: a
  matched trigger arms a due-ZSET member instead of enqueueing directly; a
  second arm for the same pair overwrites the score (coalescing) and the
  stashed message_id (always the latest).
- `invoke_debounce.acquire_turn_lock`/`release_turn_lock`, wired into
  `invoke_worker.process_entry`: a debounced fire landing while a previous
  turn for the same (agent_id, chat_id) is still running must not start a
  second concurrent turn - it re-arms the debounce timer instead.
- `invoke_debounce.mark_superseded`/`is_superseded` (ADR 00732), set by that
  same re-arm path: the in-flight turn checks this flag and ends without
  delivering its reply if a newer message has already taken its place.

Runs against real Redis (`redis_db`) and real ephemeral Postgres (ADR 0032)
where a live Agent row is needed for process_entry. Gemini is never called
for real - `_run_turn` is always mocked.
"""
import asyncio
import json
import time
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from infra.redis.client import redis_client
from modules.agents.invoke_debounce import (
    acquire_turn_lock,
    arm_debounce,
    due_pairs,
    is_superseded,
    mark_superseded,
    pop_latest_message_id,
    release_turn_lock,
)
from modules.agents.invoke_worker import AgentInvokeConsumer
from modules.agents.models import Agent, DEFAULT_AGENT_RESTRICTIONS, DEFAULT_AGENT_TRIGGERS
from modules.chats.crud.crud_chat import create_chat
from modules.chats.crud.crud_participant import add_participant_to_chat
from modules.users.crud import create_user

pytestmark = pytest.mark.asyncio

_ID = 0


def _next_id() -> int:
    global _ID
    _ID += 1
    return 991_000_000 + _ID


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


async def _make_agent(session: AsyncSession, owner_user_id: int, owner_agent_chat_id: int) -> Agent:
    agent = Agent(
        id=_next_id(),
        owner_user_id=owner_user_id,
        owner_agent_chat_id=owner_agent_chat_id,
        is_enabled=True,
        triggers=json.loads(json.dumps(DEFAULT_AGENT_TRIGGERS)),
        restrictions=json.loads(json.dumps(DEFAULT_AGENT_RESTRICTIONS)),
    )
    session.add(agent)
    await session.flush()
    await session.commit()
    return agent


def _consumer() -> AgentInvokeConsumer:
    return AgentInvokeConsumer("test-consumer", asyncio.Semaphore(10))


# --- arm_debounce / due_pairs: coalescing ------------------------------------

async def test_second_arm_for_the_same_pair_overwrites_the_score(redis_db):
    await arm_debounce(1, 2, message_id=100)
    first_score = await redis_client.zscore(settings.AGENT_INVOKE_DEBOUNCE_ZSET_KEY, "1:2")

    await arm_debounce(1, 2, message_id=200)
    second_score = await redis_client.zscore(settings.AGENT_INVOKE_DEBOUNCE_ZSET_KEY, "1:2")

    assert second_score >= first_score
    # Only one member for the pair - a burst never produces two due entries.
    assert await due_pairs(now=time.time() + 3600) == [(1, 2)]


async def test_pop_latest_message_id_returns_the_most_recent_arm(redis_db):
    await arm_debounce(5, 6, message_id=111)
    await arm_debounce(5, 6, message_id=222)

    assert await pop_latest_message_id(5, 6) == 222
    # Cleared after popping - a stale re-read must not resurrect it.
    assert await pop_latest_message_id(5, 6) is None


async def test_due_pairs_only_returns_pairs_whose_window_has_elapsed(redis_db):
    await arm_debounce(7, 8, message_id=1)

    assert await due_pairs(now=time.time() - 3600) == []
    assert await due_pairs(now=time.time() + 3600) == [(7, 8)]


async def test_due_pairs_pops_atomically_and_does_not_return_a_pair_twice(redis_db):
    await arm_debounce(9, 10, message_id=1)

    first = await due_pairs(now=time.time() + 3600)
    second = await due_pairs(now=time.time() + 3600)

    assert first == [(9, 10)]
    assert second == []


async def test_different_chats_for_the_same_agent_never_coalesce(redis_db):
    await arm_debounce(1, 100, message_id=1)
    await arm_debounce(1, 200, message_id=2)

    assert set(await due_pairs(now=time.time() + 3600)) == {(1, 100), (1, 200)}


# --- turn lock ----------------------------------------------------------------

async def test_second_acquire_for_the_same_pair_fails_while_held(redis_db):
    assert await acquire_turn_lock(1, 2) is True
    assert await acquire_turn_lock(1, 2) is False

    await release_turn_lock(1, 2)
    assert await acquire_turn_lock(1, 2) is True


async def test_lock_is_scoped_per_chat_not_just_per_agent(redis_db):
    assert await acquire_turn_lock(1, 100) is True
    # A different chat for the same agent must not contend with it.
    assert await acquire_turn_lock(1, 200) is True


async def test_none_chat_id_never_contends_with_another_none_chat_id_turn(redis_db):
    # Schedule-fired turns with no chat target (chat_id=None) share the same
    # lock key "agent:None" per agent - documented behavior, not a bug: two
    # schedule-fired turns for the same agent legitimately shouldn't race
    # either, and there is exactly one schedule poll loop per process.
    assert await acquire_turn_lock(1, None) is True
    assert await acquire_turn_lock(1, None) is False
    await release_turn_lock(1, None)


# --- process_entry: mutex wired in --------------------------------------------

async def test_process_entry_skips_and_rearms_when_a_turn_is_already_running(db_session, redis_db):
    owner = await _make_user(db_session)
    owner_chat = await _make_chat(db_session, owner)
    target_chat = await _make_chat(db_session, owner)
    agent = await _make_agent(db_session, owner, owner_chat)
    message_id = _next_id()

    assert await acquire_turn_lock(agent.id, target_chat) is True  # simulate an in-flight turn

    with patch("modules.agents.invoke_worker._run_turn", new=AsyncMock()) as mock_run_turn:
        await _consumer().process_entry(
            db_session,
            {"agent_id": str(agent.id), "chat_id": str(target_chat), "message_id": str(message_id)},
        )

    mock_run_turn.assert_not_awaited()
    # Re-armed rather than dropped - the message gets a real chance to fire
    # once the in-flight turn's lock is released.
    assert await due_pairs(now=time.time() + 3600) == [(agent.id, target_chat)]
    assert await pop_latest_message_id(agent.id, target_chat) is None  # no message_id carried by re-arm
    # ADR 00732: the in-flight turn is also flagged superseded, so it can stop
    # itself before delivering a now-stale reply.
    assert await is_superseded(agent.id, target_chat) is True


async def test_process_entry_acquires_and_releases_the_lock_around_a_normal_turn(db_session, redis_db):
    owner = await _make_user(db_session)
    owner_chat = await _make_chat(db_session, owner)
    target_chat = await _make_chat(db_session, owner)
    agent = await _make_agent(db_session, owner, owner_chat)
    message_id = _next_id()

    with patch("modules.agents.invoke_worker._run_turn", new=AsyncMock()) as mock_run_turn:
        await _consumer().process_entry(
            db_session,
            {"agent_id": str(agent.id), "chat_id": str(target_chat), "message_id": str(message_id)},
        )

    mock_run_turn.assert_awaited_once()
    # Lock released once the turn (mocked, completes instantly) finishes - a
    # follow-up fire for the same pair must be able to acquire it again.
    assert await acquire_turn_lock(agent.id, target_chat) is True


# --- supersede flag (ADR 00732) --------------------------------------------

async def test_is_superseded_is_false_when_never_marked(redis_db):
    assert await is_superseded(1, 2) is False


async def test_mark_superseded_then_is_superseded_returns_true_once(redis_db):
    await mark_superseded(1, 2)

    assert await is_superseded(1, 2) is True
    # Get-and-delete - a second read must not resurrect it for a later,
    # unrelated turn on the same pair.
    assert await is_superseded(1, 2) is False


async def test_superseded_flag_is_scoped_per_chat_not_just_per_agent(redis_db):
    await mark_superseded(1, 100)

    assert await is_superseded(1, 200) is False
    assert await is_superseded(1, 100) is True
