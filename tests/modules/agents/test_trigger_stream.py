"""ADR 0106: fan-out gate (SMISMEMBER on enabled owners) -> agent_trigger_stream
-> AgentTriggerConsumer -> unchanged trigger engine. Real Postgres + Redis."""
import json

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from infra.redis.client import redis_client
from infra.ids.snowflake import next_id as snowflake_next_id
from modules.agents.cache import reconcile_enabled_owners, sync_agent_cache
from modules.agents.invoke_debounce import due_pairs
from modules.agents.models import Agent, DEFAULT_AGENT_RESTRICTIONS, DEFAULT_AGENT_TRIGGERS
from modules.agents.trigger_queue import maybe_enqueue_trigger_eval
from modules.agents.trigger_worker import AgentTriggerConsumer
from modules.chats.crud.crud_chat import create_chat
from modules.chats.crud.crud_participant import add_participant_to_chat
from modules.messaging.crud import create_message
from modules.users.crud import create_user

pytestmark = pytest.mark.asyncio

_ID = 0


def _next_id() -> int:
    global _ID
    _ID += 1
    return 910_000_000 + _ID


async def _user(session: AsyncSession) -> int:
    uid = _next_id()
    await create_user(session, user_id=uid, phone_number=f"+1556{uid}")
    return uid


async def _chat(session: AsyncSession, *user_ids: int) -> int:
    chat_id = _next_id()
    await create_chat(session, chat_id=chat_id, is_group=False)
    for uid in user_ids:
        await add_participant_to_chat(session, chat_id=chat_id, user_id=uid)
    return chat_id


async def _stream_len() -> int:
    return await redis_client.xlen(settings.AGENT_TRIGGER_STREAM_KEY)


async def test_no_enabled_owner_means_no_enqueue(redis_db):
    assert await maybe_enqueue_trigger_eval(message_id=1, chat_id=2, sender_id=3, recipient_ids=[4, 5]) is False
    assert await _stream_len() == 0


async def test_enabled_recipient_enqueues(redis_db):
    await redis_client.sadd(settings.AGENT_ENABLED_OWNERS_SET_KEY, 5)
    assert await maybe_enqueue_trigger_eval(message_id=1, chat_id=2, sender_id=3, recipient_ids=[4, 5]) is True
    assert await _stream_len() == 1


async def test_sender_is_not_a_candidate_unless_chat_has_no_recipients(redis_db):
    await redis_client.sadd(settings.AGENT_ENABLED_OWNERS_SET_KEY, 3)
    # sender 3 has an agent, but a recipient exists -> only recipients count
    assert await maybe_enqueue_trigger_eval(message_id=1, chat_id=2, sender_id=3, recipient_ids=[4]) is False
    # owner<->agent chat: no recipients -> gate on sender
    assert await maybe_enqueue_trigger_eval(message_id=2, chat_id=2, sender_id=3, recipient_ids=[]) is True


async def test_consumer_runs_trigger_engine_from_stream(db_session: AsyncSession, redis_db):
    owner = await _user(db_session)
    sender = await _user(db_session)
    owner_chat = await _chat(db_session, owner)
    target_chat = await _chat(db_session, owner, sender)
    agent = Agent(
        id=_next_id(),
        owner_user_id=owner,
        owner_agent_chat_id=owner_chat,
        is_enabled=True,
        triggers={**json.loads(json.dumps(DEFAULT_AGENT_TRIGGERS)), "on_any_message": {"enabled": True}},
        restrictions=json.loads(json.dumps(DEFAULT_AGENT_RESTRICTIONS)),
    )
    db_session.add(agent)
    await db_session.commit()
    await sync_agent_cache(agent)

    message = await create_message(
        db_session, message_id=snowflake_next_id(), chat_id=target_chat, sender_id=sender, content="hi", type=1
    )
    await db_session.commit()

    assert await maybe_enqueue_trigger_eval(
        message_id=message.id, chat_id=target_chat, sender_id=sender, recipient_ids=[owner]
    )
    consumer = AgentTriggerConsumer("test-consumer")
    assert await consumer.drain_once(db_session, block_ms=0, claim_stale=False) == 1
    assert (agent.id, target_chat) in await due_pairs(now=10**12)


async def test_consumer_acks_missing_message(db_session: AsyncSession, redis_db):
    await redis_client.xadd(settings.AGENT_TRIGGER_STREAM_KEY, {"message_id": "999", "chat_id": "888"})
    consumer = AgentTriggerConsumer("test-consumer")
    assert await consumer.drain_once(db_session, block_ms=0, claim_stale=False) == 1


async def test_reconcile_adds_missing_and_removes_stale(db_session: AsyncSession, redis_db):
    owner = await _user(db_session)
    owner_chat = await _chat(db_session, owner)
    db_session.add(Agent(
        id=_next_id(), owner_user_id=owner, owner_agent_chat_id=owner_chat, is_enabled=True,
        triggers=json.loads(json.dumps(DEFAULT_AGENT_TRIGGERS)),
        restrictions=json.loads(json.dumps(DEFAULT_AGENT_RESTRICTIONS)),
    ))
    await db_session.commit()
    await redis_client.sadd(settings.AGENT_ENABLED_OWNERS_SET_KEY, 424242)

    assert await reconcile_enabled_owners(db_session) == (1, 1)
    members = {int(m) for m in await redis_client.smembers(settings.AGENT_ENABLED_OWNERS_SET_KEY)}
    assert members == {owner}


# --- failure handling (ADR 0106): refund + bounded requeue, invalid-config notice ---

async def _any_message_setup(db_session, triggers_patch=None):
    owner = await _user(db_session)
    sender = await _user(db_session)
    owner_chat = await _chat(db_session, owner)
    target_chat = await _chat(db_session, owner, sender)
    agent = Agent(
        id=_next_id(), owner_user_id=owner, owner_agent_chat_id=owner_chat, is_enabled=True,
        triggers={**json.loads(json.dumps(DEFAULT_AGENT_TRIGGERS)), "on_any_message": {"enabled": True},
                  **(triggers_patch or {})},
        restrictions=json.loads(json.dumps(DEFAULT_AGENT_RESTRICTIONS)),
    )
    db_session.add(agent)
    await db_session.commit()
    await sync_agent_cache(agent)
    message = await create_message(
        db_session, message_id=snowflake_next_id(), chat_id=target_chat, sender_id=sender, content="hi", type=1
    )
    await db_session.commit()
    return owner, owner_chat, agent, message, target_chat


async def _activations(agent_id: int) -> int:
    from infra.ratelimit.service import _KEY_PREFIX
    return int(await redis_client.get(f"{_KEY_PREFIX}agent_activation:{agent_id}") or 0)


async def _notice_count(db_session, chat_id: int) -> int:
    from sqlalchemy import func, select
    from modules.messaging.models import Message
    return (await db_session.execute(
        select(func.count()).select_from(Message).where(Message.chat_id == chat_id)
    )).scalar_one()


async def test_invalid_agent_config_notifies_owner_once_without_retry(db_session: AsyncSession, redis_db):
    owner, owner_chat, agent, message, target_chat = await _any_message_setup(
        db_session, {"on_any_message": "garbage"}
    )
    consumer = AgentTriggerConsumer("test-consumer")
    message_id, sender_id = message.id, message.sender_id
    for _ in range(2):
        await maybe_enqueue_trigger_eval(
            message_id=message_id, chat_id=target_chat, sender_id=sender_id, recipient_ids=[owner]
        )
        await consumer.drain_once(db_session, block_ms=0, claim_stale=False)
    assert await _notice_count(db_session, owner_chat) == 1  # cooldown: one notice
    assert await _stream_len() == 2  # only the two originals, nothing re-queued
    assert await due_pairs(now=10**12) == []


async def test_transient_failure_refunds_quota_and_requeues_failed_owner(db_session: AsyncSession, redis_db, monkeypatch):
    from redis.exceptions import ConnectionError as RedisConnectionError
    from modules.agents import trigger_engine

    owner, _, agent, message, target_chat = await _any_message_setup(db_session)

    async def boom(*a, **k):
        raise RedisConnectionError("down")

    monkeypatch.setattr(trigger_engine, "_arm_debounce_eager", boom)
    message_id, sender_id = message.id, message.sender_id
    await maybe_enqueue_trigger_eval(
        message_id=message_id, chat_id=target_chat, sender_id=sender_id, recipient_ids=[owner]
    )
    consumer = AgentTriggerConsumer("test-consumer")
    await consumer.drain_once(db_session, block_ms=0, claim_stale=False)

    assert await _activations(agent.id) == 0  # refunded
    entries = await redis_client.xrange(settings.AGENT_TRIGGER_STREAM_KEY)
    assert len(entries) == 2
    retry = entries[-1][1]
    assert retry["attempts"] == "1" and retry["owner_ids"] == str(owner)


async def test_requeue_dropped_after_max_attempts(db_session: AsyncSession, redis_db):
    consumer = AgentTriggerConsumer("test-consumer")
    last = {"message_id": "1", "chat_id": "2", "attempts": str(settings.AGENT_TRIGGER_MAX_ATTEMPTS - 1)}
    await consumer._requeue(last, {7})
    assert await _stream_len() == 0


async def test_failed_owner_does_not_break_next_owner_in_same_message(db_session: AsyncSession, redis_db):
    """The first owner's invalid config triggers session.rollback(); the second
    owner must still be evaluated (message attributes must not have expired)."""
    sender = await _user(db_session)
    bad_owner = await _user(db_session)
    good_owner = await _user(db_session)
    chat_id = _next_id()
    await create_chat(db_session, chat_id=chat_id, is_group=False)
    for uid in (sender, bad_owner, good_owner):
        await add_participant_to_chat(db_session, chat_id=chat_id, user_id=uid)
    agents = []
    for uid, patch in ((bad_owner, {"on_any_message": "garbage"}), (good_owner, {})):
        owner_chat = await _chat(db_session, uid)
        agent = Agent(
            id=_next_id(), owner_user_id=uid, owner_agent_chat_id=owner_chat, is_enabled=True,
            triggers={**json.loads(json.dumps(DEFAULT_AGENT_TRIGGERS)), "on_any_message": {"enabled": True}, **patch},
            restrictions=json.loads(json.dumps(DEFAULT_AGENT_RESTRICTIONS)),
        )
        db_session.add(agent)
        agents.append(agent)
    await db_session.commit()
    for a in agents:
        await sync_agent_cache(a)
    good_agent_id = agents[1].id
    message = await create_message(
        db_session, message_id=snowflake_next_id(), chat_id=chat_id, sender_id=sender, content="hi", type=1
    )
    await db_session.commit()
    await maybe_enqueue_trigger_eval(
        message_id=message.id, chat_id=chat_id, sender_id=sender, recipient_ids=[bad_owner, good_owner]
    )
    await AgentTriggerConsumer("test-consumer").drain_once(db_session, block_ms=0, claim_stale=False)
    assert (good_agent_id, chat_id) in await due_pairs(now=10**12)
