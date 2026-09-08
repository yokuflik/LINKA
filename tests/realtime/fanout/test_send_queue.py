"""
The async send path: enqueue_outgoing_message -> message_send_stream ->
send worker (drain_once) -> message persisted + new_message fanned out.

Real Postgres + Redis, like the rest of the suite. Running this file wipes
the dev DB on teardown (see CLAUDE.md) - dump or re-seed afterwards.
"""
import asyncio
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from database.crud.crud_message import get_chat_messages
from database.crud.crud_user import create_user
from services import chat_service, realtime_service
from services.fanout import fanout_worker
from services.fanout import routing
from services.fanout import send_queue
from services.fanout import worker as send_worker

pytestmark = pytest.mark.asyncio


async def _group(session: AsyncSession, owner_id: int, member_ids) -> int:
    await create_user(session, user_id=owner_id, phone_number=f"+97250{owner_id}")
    for m in member_ids:
        await create_user(session, user_id=m, phone_number=f"+97250{m}")
    chat = await chat_service.create_group_chat(
        session, creator_id=owner_id, title="G", initial_member_ids=list(member_ids)
    )
    return chat.id


async def _drain_all(session) -> int:
    await send_queue.ensure_group()
    total = 0
    while True:
        n = await send_worker.drain_once(session, block_ms=0, claim_stale=False)
        if n == 0:
            return total
        total += n


async def _drain_fanout(session) -> int:
    await send_queue.ensure_fanout_group()
    total = 0
    while True:
        n = await fanout_worker.drain_once(session, block_ms=0, claim_stale=False)
        if n == 0:
            return total
        total += n


async def test_enqueue_then_drain_persists_and_fans_out(db_session: AsyncSession, redis_db):
    chat_id = await _group(db_session, 1, [2])
    await send_queue.ensure_group()

    await routing.add_chat_for_instance("test-server", chat_id)
    agen = realtime_service.subscribe_to_instance_inbox("test-server")
    first_event = asyncio.create_task(agen.__anext__())
    try:
        await asyncio.sleep(0.2)
        await send_queue.enqueue_outgoing_message(
            chat_id=chat_id, sender_id=1, client_message_id=str(uuid.uuid4()), content="hi"
        )
        written = await send_worker.drain_once(db_session, claim_stale=False)
        await _drain_fanout(db_session)
        event = await asyncio.wait_for(first_event, timeout=2.0)
    finally:
        await agen.aclose()

    assert written == 1
    messages = await get_chat_messages(db_session, chat_id=chat_id, limit=10)
    assert [m.content for m in messages] == ["hi"]
    assert event["event"] == "new_message"
    assert event["message_id"] == str(messages[0].id)


async def test_duplicate_client_message_id_in_stream_writes_one_row(db_session: AsyncSession, redis_db):
    chat_id = await _group(db_session, 1, [2])
    cmid = str(uuid.uuid4())

    await send_queue.enqueue_outgoing_message(chat_id=chat_id, sender_id=1, client_message_id=cmid, content="hi")
    await _drain_all(db_session)
    # A second stream entry for the same client_message_id (a client retry that
    # got enqueued twice).
    await send_queue.enqueue_outgoing_message(chat_id=chat_id, sender_id=1, client_message_id=cmid, content="hi")
    await _drain_all(db_session)

    messages = await get_chat_messages(db_session, chat_id=chat_id, limit=10)
    assert len(messages) == 1


async def test_duplicate_notifies_sender_channel(db_session: AsyncSession, redis_db):
    chat_id = await _group(db_session, 1, [2])
    cmid = str(uuid.uuid4())
    await send_queue.enqueue_outgoing_message(chat_id=chat_id, sender_id=1, client_message_id=cmid, content="hi")
    await _drain_all(db_session)

    agen = realtime_service.subscribe_to_user(1)
    evt_task = asyncio.create_task(agen.__anext__())
    try:
        await asyncio.sleep(0.2)
        await send_queue.enqueue_outgoing_message(chat_id=chat_id, sender_id=1, client_message_id=cmid, content="hi")
        await _drain_all(db_session)
        event = await asyncio.wait_for(evt_task, timeout=2.0)
    finally:
        await agen.aclose()

    assert event["event"] == "message_already_sent"
    assert event["client_message_id"] == cmid


async def test_duplicate_re_enqueues_fanout_for_crash_recovery(db_session: AsyncSession, redis_db):
    # If the first send entry committed the row but crashed before enqueuing
    # fan-out, the message would never be delivered. The duplicate entry must
    # re-enqueue fan-out (worker.py recovery path), not just notify the sender.
    chat_id = await _group(db_session, 1, [2])
    cmid = str(uuid.uuid4())
    await send_queue.enqueue_outgoing_message(chat_id=chat_id, sender_id=1, client_message_id=cmid, content="hi")
    await _drain_all(db_session)
    drained_first = await _drain_fanout(db_session)
    assert drained_first == 1

    await send_queue.enqueue_outgoing_message(chat_id=chat_id, sender_id=1, client_message_id=cmid, content="hi")
    await _drain_all(db_session)

    assert await _drain_fanout(db_session) == 1


async def test_bad_media_key_fails_the_message_and_notifies_sender(db_session: AsyncSession, redis_db):
    chat_id = await _group(db_session, 1, [2])
    cmid = str(uuid.uuid4())

    agen = realtime_service.subscribe_to_user(1)
    evt_task = asyncio.create_task(agen.__anext__())
    try:
        await asyncio.sleep(0.2)
        await send_queue.enqueue_outgoing_message(
            chat_id=chat_id, sender_id=1, client_message_id=cmid, type=2,
            media_key="does/not/exist.jpg",
        )
        acked = await send_worker.drain_once(db_session, claim_stale=False)
        event = await asyncio.wait_for(evt_task, timeout=2.0)
    finally:
        await agen.aclose()

    # Permanent failure: the entry is acked (retrying can't help) and no row written.
    assert acked == 1
    assert event["event"] == "message_failed"
    assert event["client_message_id"] == cmid
    messages = await get_chat_messages(db_session, chat_id=chat_id, limit=10)
    assert messages == []


async def test_messages_land_on_the_chats_shard(db_session: AsyncSession, redis_db):
    from config import SEND_STREAM_SHARDS
    from services.redis_client import redis_client

    chat_id = await _group(db_session, 1, [2])
    await send_queue.enqueue_outgoing_message(
        chat_id=chat_id, sender_id=1, client_message_id=str(uuid.uuid4()), content="x"
    )

    shard = send_queue.shard_for_chat(chat_id, SEND_STREAM_SHARDS)
    expected_key = send_queue.stream_key("message_send_stream", shard)
    other_keys = [k for k in send_queue.send_stream_keys() if k != expected_key]

    assert await redis_client.xlen(expected_key) == 1
    for k in other_keys:
        assert await redis_client.xlen(k) == 0

    # A shard-scoped drain of a different shard leaves the entry; the chat's
    # shard drains it.
    for k in other_keys:
        s = int(k.rsplit(":", 1)[1]) if ":" in k else 0
        assert await send_worker.drain_once(db_session, shard=s, claim_stale=False) == 0
    assert await send_worker.drain_once(db_session, shard=shard, claim_stale=False) == 1


async def test_messages_are_written_in_enqueue_order(db_session: AsyncSession, redis_db):
    chat_id = await _group(db_session, 1, [2])

    contents = [f"msg-{i}" for i in range(50)]
    for c in contents:
        await send_queue.enqueue_outgoing_message(
            chat_id=chat_id, sender_id=1, client_message_id=str(uuid.uuid4()), content=c
        )
    await _drain_all(db_session)

    messages = await get_chat_messages(db_session, chat_id=chat_id, limit=100)
    # get_chat_messages is newest-first; ids are monotonic, so reversing gives
    # enqueue order.
    got = [m.content for m in reversed(messages)]
    assert got == contents
