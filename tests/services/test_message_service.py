import asyncio
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from database.crud.crud_user import create_user
from services import chat_service, message_service, presence_service
from services.fanout import fanout_worker, send_queue

pytestmark = pytest.mark.asyncio


async def _make_group(session: AsyncSession, owner_id: int, member_ids) -> int:
    await create_user(session, user_id=owner_id, phone_number=f"+97250{owner_id}")
    for member_id in member_ids:
        await create_user(session, user_id=member_id, phone_number=f"+97250{member_id}")
    group = await chat_service.create_group_chat(session, creator_id=owner_id, title="Test", initial_member_ids=list(member_ids))
    return group.id


async def _drain_fanout(session) -> int:
    """Fan-out is a second hop off message_fanout_stream now - run it to completion."""
    await send_queue.ensure_fanout_group()
    total = 0
    while True:
        n = await fanout_worker.drain_once(session, block_ms=0, claim_stale=False)
        if n == 0:
            return total
        total += n


async def test_send_message_persists_it(db_session: AsyncSession, redis_db):
    chat_id = await _make_group(db_session, 1, [2])

    message = await message_service.process_outgoing(db_session, sender_id=1, chat_id=chat_id, client_message_id=str(uuid.uuid4()), content="hi")

    assert message.chat_id == chat_id
    assert message.sender_id == 1
    assert message.content == "hi"


async def test_send_message_rejects_a_non_participant(db_session: AsyncSession, redis_db):
    chat_id = await _make_group(db_session, 1, [2])
    await create_user(db_session, user_id=3, phone_number="+972503")

    with pytest.raises(message_service.NotAParticipantError):
        await message_service.process_outgoing(db_session, sender_id=3, chat_id=chat_id, client_message_id=str(uuid.uuid4()), content="hi")


async def test_retrying_the_same_client_message_id_raises_already_sent_with_the_same_id(db_session: AsyncSession, redis_db):
    chat_id = await _make_group(db_session, 1, [2])
    client_message_id = str(uuid.uuid4())

    first = await message_service.process_outgoing(db_session, sender_id=1, chat_id=chat_id, client_message_id=client_message_id, content="hi")
    with pytest.raises(message_service.MessageAlreadySentError) as exc:
        await message_service.process_outgoing(db_session, sender_id=1, chat_id=chat_id, client_message_id=client_message_id, content="hi")

    assert exc.value.message_id == first.id


async def test_different_client_message_ids_create_different_messages(db_session: AsyncSession, redis_db):
    chat_id = await _make_group(db_session, 1, [2])

    first = await message_service.process_outgoing(db_session, sender_id=1, chat_id=chat_id, client_message_id=str(uuid.uuid4()), content="hi")
    second = await message_service.process_outgoing(db_session, sender_id=1, chat_id=chat_id, client_message_id=str(uuid.uuid4()), content="hi")

    assert first.id != second.id


async def test_the_same_client_message_id_is_scoped_per_chat(db_session: AsyncSession, redis_db):
    # Two different chats reusing the same client_message_id (plausible if a
    # client generates ids per-session rather than globally) must not be
    # treated as duplicates of each other.
    chat_a = await _make_group(db_session, 1, [2])
    chat_b = await _make_group(db_session, 1, [2])
    client_message_id = str(uuid.uuid4())

    msg_a = await message_service.process_outgoing(db_session, sender_id=1, chat_id=chat_a, client_message_id=client_message_id, content="hi")
    msg_b = await message_service.process_outgoing(db_session, sender_id=1, chat_id=chat_b, client_message_id=client_message_id, content="hi")

    assert msg_a.id != msg_b.id


async def test_concurrent_send_with_the_same_client_message_id_never_double_sends(session_factory, redis_db):
    # The core idempotency guarantee under real concurrency: a client retry
    # racing the original request (both in flight at once, not sequential)
    # must still only ever produce one message.
    async with session_factory() as setup:
        chat_id = await _make_group(setup, 1, [2])
    client_message_id = str(uuid.uuid4())

    async def attempt():
        async with session_factory() as session:
            try:
                msg = await message_service.process_outgoing(
                    session, sender_id=1, chat_id=chat_id, client_message_id=client_message_id, content="hi"
                )
                return msg.id
            except message_service.MessageAlreadySentError as exc:
                return exc.message_id

    results = await asyncio.gather(attempt(), attempt(), attempt())

    # Exactly one real insert, and every racer agrees on its id.
    assert len(set(results)) == 1


async def test_concurrent_sends_with_different_ids_all_persist(session_factory, redis_db):
    # A burst of genuinely distinct messages (e.g. a fast typist, or several
    # group members posting at once) must all land - concurrency must never
    # silently drop or collide different messages.
    async with session_factory() as setup:
        chat_id = await _make_group(setup, 1, [2])

    async def attempt(i: int):
        async with session_factory() as session:
            return await message_service.process_outgoing(session, sender_id=1, chat_id=chat_id, client_message_id=str(uuid.uuid4()), content=f"msg-{i}")

    results = await asyncio.gather(*[attempt(i) for i in range(30)])
    assert len({r.id for r in results}) == 30


async def test_get_message_history_rejects_a_non_participant(db_session: AsyncSession, redis_db):
    chat_id = await _make_group(db_session, 1, [2])
    await create_user(db_session, user_id=3, phone_number="+972503")

    with pytest.raises(message_service.NotAParticipantError):
        await message_service.get_message_history(db_session, user_id=3, chat_id=chat_id)


async def test_get_message_history_returns_sent_messages(db_session: AsyncSession, redis_db):
    chat_id = await _make_group(db_session, 1, [2])
    await message_service.process_outgoing(db_session, sender_id=1, chat_id=chat_id, client_message_id=str(uuid.uuid4()), content="hi")

    history = await message_service.get_message_history(db_session, user_id=2, chat_id=chat_id)
    assert len(history) == 1


async def test_only_the_sender_can_edit_their_message(db_session: AsyncSession, redis_db):
    chat_id = await _make_group(db_session, 1, [2])
    message = await message_service.process_outgoing(db_session, sender_id=1, chat_id=chat_id, client_message_id=str(uuid.uuid4()), content="typo")

    with pytest.raises(message_service.NotAParticipantError):
        await message_service.edit_message(db_session, user_id=2, chat_id=chat_id, message_id=message.id, new_content="hijacked")

    edited = await message_service.edit_message(db_session, user_id=1, chat_id=chat_id, message_id=message.id, new_content="fixed")
    assert edited.content == "fixed"


async def test_editing_a_nonexistent_message_is_rejected_not_crashed(db_session: AsyncSession, redis_db):
    chat_id = await _make_group(db_session, 1, [2])

    with pytest.raises(message_service.NotAParticipantError):
        await message_service.edit_message(db_session, user_id=1, chat_id=chat_id, message_id=999999, new_content="x")


async def test_only_the_sender_can_delete_their_message(db_session: AsyncSession, redis_db):
    chat_id = await _make_group(db_session, 1, [2])
    message = await message_service.process_outgoing(db_session, sender_id=1, chat_id=chat_id, client_message_id=str(uuid.uuid4()), content="oops")

    with pytest.raises(message_service.NotAParticipantError):
        await message_service.delete_message(db_session, user_id=2, chat_id=chat_id, message_id=message.id)

    deleted = await message_service.delete_message(db_session, user_id=1, chat_id=chat_id, message_id=message.id)
    assert deleted is True


async def test_deleting_an_already_deleted_message_returns_false(db_session: AsyncSession, redis_db):
    chat_id = await _make_group(db_session, 1, [2])
    message = await message_service.process_outgoing(db_session, sender_id=1, chat_id=chat_id, client_message_id=str(uuid.uuid4()), content="oops")

    await message_service.delete_message(db_session, user_id=1, chat_id=chat_id, message_id=message.id)
    second_attempt = await message_service.delete_message(db_session, user_id=1, chat_id=chat_id, message_id=message.id)

    assert second_attempt is False


async def test_mark_as_read_updates_the_watermark(db_session: AsyncSession, redis_db):
    from database.crud.crud_participant import get_chat_participants

    chat_id = await _make_group(db_session, 1, [2])
    message = await message_service.process_outgoing(db_session, sender_id=1, chat_id=chat_id, client_message_id=str(uuid.uuid4()), content="hi")

    await message_service.mark_as_read(db_session, user_id=2, chat_id=chat_id, message_id=message.id)

    participants = await get_chat_participants(db_session, chat_id)
    reader = next(p for p in participants if p.user_id == 2)
    assert reader.last_read_message_id == message.id


async def test_fan_out_pushes_only_to_offline_recipients_never_the_sender(db_session: AsyncSession, redis_db, monkeypatch):
    pushed_to = []

    async def fake_send_push(user_id, title, body, data=None):
        pushed_to.append(user_id)

    from services import notification_service
    monkeypatch.setattr(notification_service, "send_push", fake_send_push)

    chat_id = await _make_group(db_session, 1, [2, 3])
    await presence_service.mark_online(2, "conn-1", "server-1")  # user 2 is online, user 3 is not

    await message_service.process_outgoing(db_session, sender_id=1, chat_id=chat_id, client_message_id=str(uuid.uuid4()), content="hi")
    await _drain_fanout(db_session)

    assert pushed_to == [3]


async def test_fan_out_to_a_large_group_only_pushes_the_offline_half(db_session: AsyncSession, redis_db, monkeypatch):
    # Scale check: a 200-member group where half are actively connected -
    # only the offline half should ever hit notification_service.
    pushed_to = []

    async def fake_send_push(user_id, title, body, data=None):
        pushed_to.append(user_id)

    from services import notification_service
    monkeypatch.setattr(notification_service, "send_push", fake_send_push)

    member_ids = list(range(2, 202))
    chat_id = await _make_group(db_session, 1, member_ids)

    online_ids = member_ids[:100]
    for i, user_id in enumerate(online_ids):
        await presence_service.mark_online(user_id, f"conn-{i}", "server-1")

    await message_service.process_outgoing(db_session, sender_id=1, chat_id=chat_id, client_message_id=str(uuid.uuid4()), content="hi")
    await _drain_fanout(db_session)

    assert set(pushed_to) == set(member_ids[100:])


async def test_send_message_fans_out_over_realtime_pubsub(db_session: AsyncSession, redis_db):
    from services import realtime_service
    from services.fanout import routing

    chat_id = await _make_group(db_session, 1, [2])

    await routing.add_chat_for_instance("test-server", chat_id)
    agen = realtime_service.subscribe_to_instance_inbox("test-server")
    first_item_task = asyncio.create_task(agen.__anext__())
    try:
        await asyncio.sleep(0.2)
        sent = await message_service.process_outgoing(db_session, sender_id=1, chat_id=chat_id, client_message_id=str(uuid.uuid4()), content="hi")
        await _drain_fanout(db_session)
        event = await asyncio.wait_for(first_item_task, timeout=2.0)
    finally:
        await agen.aclose()

    assert event["event"] == "new_message"
    # Ids go out as strings on this channel - see message_service.fan_out_message
    # and the big comment in poc/index.html for why (JS JSON-number precision).
    assert event["message_id"] == str(sent.id)
    assert isinstance(event["chat_id"], str)


async def test_send_message_rejects_content_over_the_length_cap(db_session: AsyncSession, redis_db, monkeypatch):
    monkeypatch.setattr(message_service, "MAX_MESSAGE_CONTENT_LENGTH", 10)
    chat_id = await _make_group(db_session, 1, [2])

    with pytest.raises(message_service.MessageTooLongError):
        await message_service.process_outgoing(
            db_session, sender_id=1, chat_id=chat_id, client_message_id=str(uuid.uuid4()), content="x" * 11
        )


async def test_send_message_allows_content_at_exactly_the_cap(db_session: AsyncSession, redis_db, monkeypatch):
    monkeypatch.setattr(message_service, "MAX_MESSAGE_CONTENT_LENGTH", 10)
    chat_id = await _make_group(db_session, 1, [2])

    message = await message_service.process_outgoing(
        db_session, sender_id=1, chat_id=chat_id, client_message_id=str(uuid.uuid4()), content="x" * 10
    )
    assert message.content == "x" * 10


async def test_edit_message_rejects_content_over_the_length_cap(db_session: AsyncSession, redis_db, monkeypatch):
    chat_id = await _make_group(db_session, 1, [2])
    message = await message_service.process_outgoing(db_session, sender_id=1, chat_id=chat_id, client_message_id=str(uuid.uuid4()), content="hi")

    monkeypatch.setattr(message_service, "MAX_MESSAGE_CONTENT_LENGTH", 10)
    with pytest.raises(message_service.MessageTooLongError):
        await message_service.edit_message(db_session, user_id=1, chat_id=chat_id, message_id=message.id, new_content="x" * 11)


async def test_send_message_threads_a_reply(db_session: AsyncSession, redis_db):
    from services import realtime_service
    from services.fanout import routing

    chat_id = await _make_group(db_session, 1, [2])
    original = await message_service.process_outgoing(db_session, sender_id=1, chat_id=chat_id, client_message_id=str(uuid.uuid4()), content="original")
    await _drain_fanout(db_session)  # flush the original's fan-out so the subscriber only sees the reply's

    await routing.add_chat_for_instance("test-server", chat_id)
    agen = realtime_service.subscribe_to_instance_inbox("test-server")
    first_item_task = asyncio.create_task(agen.__anext__())
    try:
        await asyncio.sleep(0.2)
        reply = await message_service.process_outgoing(
            db_session, sender_id=1, chat_id=chat_id, client_message_id=str(uuid.uuid4()),
            content="a reply", reply_to_message_id=original.id,
        )
        await _drain_fanout(db_session)
        event = await asyncio.wait_for(first_item_task, timeout=2.0)
    finally:
        await agen.aclose()

    # Persisted on the row...
    assert reply.reply_to_message_id == original.id
    # ...and echoed as a string on the live new_message event.
    assert event["reply_to_message_id"] == str(original.id)


async def test_fan_out_event_echoes_the_client_message_id(db_session: AsyncSession, redis_db):
    from services import realtime_service
    from services.fanout import routing

    chat_id = await _make_group(db_session, 1, [2])
    client_message_id = str(uuid.uuid4())

    await routing.add_chat_for_instance("test-server", chat_id)
    agen = realtime_service.subscribe_to_instance_inbox("test-server")
    first_item_task = asyncio.create_task(agen.__anext__())
    try:
        await asyncio.sleep(0.2)
        await message_service.process_outgoing(
            db_session, sender_id=1, chat_id=chat_id, client_message_id=client_message_id, content="hi"
        )
        await _drain_fanout(db_session)
        event = await asyncio.wait_for(first_item_task, timeout=2.0)
    finally:
        await agen.aclose()

    # The sender's client matches this to its optimistic bubble; never persisted.
    assert event["client_message_id"] == client_message_id


async def test_mark_as_delivered_updates_the_watermark_and_fans_out(db_session: AsyncSession, redis_db):
    from database.crud.crud_participant import get_chat_participants
    from services import realtime_service
    from services.fanout import routing

    chat_id = await _make_group(db_session, 1, [2])
    message = await message_service.process_outgoing(db_session, sender_id=1, chat_id=chat_id, client_message_id=str(uuid.uuid4()), content="hi")

    await routing.add_chat_for_instance("test-server", chat_id)
    agen = realtime_service.subscribe_to_instance_inbox("test-server")
    first_item_task = asyncio.create_task(agen.__anext__())
    try:
        await asyncio.sleep(0.2)
        await message_service.mark_as_delivered(db_session, user_id=2, chat_id=chat_id, message_id=message.id)
        event = await asyncio.wait_for(first_item_task, timeout=2.0)
    finally:
        await agen.aclose()

    participants = await get_chat_participants(db_session, chat_id)
    recipient = next(p for p in participants if p.user_id == 2)
    assert recipient.last_delivered_message_id == message.id

    assert event["event"] == "delivery_receipt"
    assert event["user_id"] == "2"
    assert event["message_id"] == str(message.id)


async def test_mark_as_played_updates_the_watermark(db_session: AsyncSession, redis_db):
    from database.crud.crud_message import create_message
    from database.crud.crud_participant import get_chat_participants
    from utils.snowflake import next_id

    chat_id = await _make_group(db_session, 1, [2])
    # mark_as_played only accepts a voice message (type 4); build one directly
    # (no MinIO round trip needed - the played watermark logic is what's under test).
    message = await create_message(db_session, message_id=next_id(), chat_id=chat_id, sender_id=1, type=4)

    await message_service.mark_as_played(db_session, user_id=2, chat_id=chat_id, message_id=message.id)

    participants = await get_chat_participants(db_session, chat_id)
    listener = next(p for p in participants if p.user_id == 2)
    assert listener.last_played_message_id == message.id


async def test_mark_as_played_rejects_a_non_voice_message(db_session: AsyncSession, redis_db):
    chat_id = await _make_group(db_session, 1, [2])
    message = await message_service.process_outgoing(
        db_session, sender_id=1, chat_id=chat_id, client_message_id=str(uuid.uuid4()), content="just text"
    )

    with pytest.raises(message_service.NotAVoiceMessageError):
        await message_service.mark_as_played(db_session, user_id=2, chat_id=chat_id, message_id=message.id)


async def test_marking_behind_the_watermark_fans_out_nothing(db_session: AsyncSession, redis_db):
    # A redundant/behind re-mark returns None from the CRUD layer, so
    # mark_as_* must publish no event and enqueue no receipt-log row.
    from services import realtime_service
    from services.fanout import routing

    chat_id = await _make_group(db_session, 1, [2])
    m1 = await message_service.process_outgoing(db_session, sender_id=1, chat_id=chat_id, client_message_id=str(uuid.uuid4()), content="1")
    m2 = await message_service.process_outgoing(db_session, sender_id=1, chat_id=chat_id, client_message_id=str(uuid.uuid4()), content="2")
    await message_service.mark_as_read(db_session, user_id=2, chat_id=chat_id, message_id=m2.id)

    await routing.add_chat_for_instance("test-server", chat_id)
    agen = realtime_service.subscribe_to_instance_inbox("test-server")
    first_item_task = asyncio.create_task(agen.__anext__())
    try:
        await asyncio.sleep(0.2)
        # m1 is behind the watermark (already at m2) - no-op
        await message_service.mark_as_read(db_session, user_id=2, chat_id=chat_id, message_id=m1.id)
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(first_item_task), timeout=0.5)
    finally:
        first_item_task.cancel()
        try:
            await first_item_task
        except (asyncio.CancelledError, StopAsyncIteration):
            pass
        await agen.aclose()


async def test_send_system_message_persists_synchronously_and_enqueues_fanout(db_session: AsyncSession, redis_db):
    from database.crud.crud_message import get_message_by_id

    chat_id = await _make_group(db_session, 1, [2])

    message = await message_service.send_system_message(db_session, chat_id=chat_id, content="X joined the group")

    # Persisted right away (chat_service depends on the returned row)...
    assert message.sender_id is None
    assert await get_message_by_id(db_session, chat_id=chat_id, message_id=message.id) is not None

    # ...and its fan-out went onto the same stream as normal messages.
    await send_queue.ensure_fanout_group()
    drained = await fanout_worker.drain_once(db_session, block_ms=0, claim_stale=False)
    assert drained >= 1
