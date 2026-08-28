"""
Step 2: fan-out moved onto its own stream. enqueue_fanout -> message_fanout_stream
-> fanout worker (drain_once) -> new_message published + push to offline members.

Real Postgres + Redis. Running this file wipes the dev DB on teardown (see
CLAUDE.md) - dump or re-seed afterwards.
"""
import asyncio
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from database.crud.crud_message import create_message
from database.crud.crud_user import create_user
from services import chat_service, presence_service, realtime_service
from services.fanout import fanout_worker, routing, send_queue
from utils.snowflake import next_id

pytestmark = pytest.mark.asyncio


async def _group(session: AsyncSession, owner_id: int, member_ids) -> int:
    await create_user(session, user_id=owner_id, phone_number=f"+97250{owner_id}")
    for m in member_ids:
        await create_user(session, user_id=m, phone_number=f"+97250{m}")
    chat = await chat_service.create_group_chat(
        session, creator_id=owner_id, title="G", initial_member_ids=list(member_ids)
    )
    return chat.id


async def _persist(session: AsyncSession, chat_id: int, sender_id: int, content: str):
    return await create_message(
        session, message_id=next_id(), chat_id=chat_id, sender_id=sender_id, type=1, content=content
    )


async def _drain_fanout(session) -> int:
    await send_queue.ensure_fanout_group()
    total = 0
    while True:
        n = await fanout_worker.drain_once(session, block_ms=0, claim_stale=False)
        if n == 0:
            return total
        total += n


async def test_enqueue_fanout_then_drain_publishes_new_message(db_session: AsyncSession, redis_db):
    chat_id = await _group(db_session, 1, [2])
    await send_queue.ensure_fanout_group()
    message = await _persist(db_session, chat_id, 1, "hi")

    await routing.add_chat_for_instance("test-server", chat_id)
    agen = realtime_service.subscribe_to_instance_inbox("test-server")
    first_event = asyncio.create_task(agen.__anext__())
    try:
        await asyncio.sleep(0.2)
        await send_queue.enqueue_fanout(
            message_id=message.id, chat_id=chat_id, sender_id=1, client_message_id="cmid-1"
        )
        acked = await fanout_worker.drain_once(db_session, claim_stale=False)
        event = await asyncio.wait_for(first_event, timeout=2.0)
    finally:
        await agen.aclose()

    assert acked == 1
    assert event["event"] == "new_message"
    assert event["message_id"] == str(message.id)
    assert event["client_message_id"] == "cmid-1"


async def test_fanout_pushes_only_to_offline_members(db_session: AsyncSession, redis_db, monkeypatch):
    pushed_to = []

    async def fake_send_push(user_id, title, body, data=None):
        pushed_to.append(user_id)

    from services import notification_service
    monkeypatch.setattr(notification_service, "send_push", fake_send_push)

    chat_id = await _group(db_session, 1, [2, 3])
    await presence_service.mark_online(2, "conn-1", "server-1")  # 2 online, 3 offline
    message = await _persist(db_session, chat_id, 1, "hi")

    await send_queue.enqueue_fanout(message_id=message.id, chat_id=chat_id, sender_id=1)
    await _drain_fanout(db_session)

    assert pushed_to == [3]


async def test_missing_message_row_is_acked_not_retried(db_session: AsyncSession, redis_db):
    chat_id = await _group(db_session, 1, [2])
    await send_queue.enqueue_fanout(message_id=next_id(), chat_id=chat_id, sender_id=1)

    acked = await fanout_worker.drain_once(db_session, claim_stale=False)

    assert acked == 1


async def test_redelivered_fanout_just_republishes(db_session: AsyncSession, redis_db):
    chat_id = await _group(db_session, 1, [2])
    message = await _persist(db_session, chat_id, 1, "hi")

    await send_queue.enqueue_fanout(message_id=message.id, chat_id=chat_id, sender_id=1)
    await _drain_fanout(db_session)
    # A second entry for the same message (e.g. the send worker's already-sent
    # recovery path) - fan-out is idempotent, clients dedupe by message_id.
    await send_queue.enqueue_fanout(message_id=message.id, chat_id=chat_id, sender_id=1)
    acked = await _drain_fanout(db_session)

    assert acked == 1
