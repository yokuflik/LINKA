"""
Poll worker that fires due scheduled messages (ADR 0031,
realtime/fanout/scheduled_worker.py).

`drain_once` claims due ids off the Redis ZSET, re-checks participation, and
enqueues the send on `message_send_stream` reusing the stored
`client_message_id`. `reconcile_once` rebuilds the ZSET from Postgres.
"""
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from config import MESSAGE_SEND_STREAM_KEY, SCHEDULED_DUE_SET_KEY
from modules.messaging import crud_scheduled
from modules.messaging import scheduled_service
from modules.messaging.models import ScheduledMessageStatus
from modules.users.crud import create_user
from modules.chats import service as chat_service
from realtime.fanout import scheduled_worker
from realtime.fanout.send_queue import shard_for_chat, stream_key
from config import SEND_STREAM_SHARDS

pytestmark = pytest.mark.asyncio


async def _make_group(session: AsyncSession, owner_id: int, member_ids) -> int:
    await create_user(session, user_id=owner_id, phone_number=f"+97250{owner_id}")
    for member_id in member_ids:
        await create_user(session, user_id=member_id, phone_number=f"+97250{member_id}")
    group = await chat_service.create_group_chat(
        session, creator_id=owner_id, title="Test", initial_member_ids=list(member_ids)
    )
    return group.id


async def _send_stream_entries(redis_db, chat_id: int):
    key = stream_key(MESSAGE_SEND_STREAM_KEY, shard_for_chat(chat_id, SEND_STREAM_SHARDS))
    return await redis_db.xrange(key)


async def _make_due(session, redis_db, chat_id: int, sender_id: int, **kw):
    """Schedule a row, then rewrite its due-set score to the past so drain fires it."""
    row = await scheduled_service.schedule_message(
        session, sender_id=sender_id, chat_id=chat_id,
        client_message_id=kw.pop("client_message_id", str(uuid.uuid4())),
        scheduled_for=datetime.now(timezone.utc) + timedelta(hours=1),
        content=kw.pop("content", "scheduled hi"), **kw,
    )
    await redis_db.zadd(SCHEDULED_DUE_SET_KEY, {str(row.id): time.time() - 1})
    return row


async def test_drain_once_fires_a_due_message_onto_the_send_stream(
    db_session: AsyncSession, redis_db
):
    chat_id = await _make_group(db_session, 1, [2])
    cmid = str(uuid.uuid4())
    row = await _make_due(db_session, redis_db, chat_id, 1, client_message_id=cmid, content="hello")

    fired = await scheduled_worker.drain_once()

    assert fired == 1
    entries = await _send_stream_entries(redis_db, chat_id)
    assert len(entries) == 1
    _, fields = entries[0]
    assert fields["client_message_id"] == cmid
    assert fields["content"] == "hello"
    assert int(fields["chat_id"]) == chat_id

    await db_session.refresh(row)
    assert row.status == ScheduledMessageStatus.SENT
    assert await redis_db.zscore(SCHEDULED_DUE_SET_KEY, str(row.id)) is None


async def test_drain_once_marks_failed_when_the_sender_is_no_longer_a_participant(
    db_session: AsyncSession, redis_db
):
    chat_id = await _make_group(db_session, 1, [2])
    row = await _make_due(db_session, redis_db, chat_id, 2)

    # User 2 leaves the group before the fire time.
    await chat_service.remove_member(db_session, actor_id=2, chat_id=chat_id, target_user_id=2)

    fired = await scheduled_worker.drain_once()

    assert fired == 1
    assert await _send_stream_entries(redis_db, chat_id) == []
    await db_session.refresh(row)
    assert row.status == ScheduledMessageStatus.FAILED
    assert row.last_error == "no longer a participant"


async def test_drain_once_is_idempotent_on_a_double_run(db_session: AsyncSession, redis_db):
    chat_id = await _make_group(db_session, 1, [2])
    await _make_due(db_session, redis_db, chat_id, 1)

    first = await scheduled_worker.drain_once()
    second = await scheduled_worker.drain_once()

    assert first == 1
    assert second == 0  # already claimed off the ZSET, nothing due
    assert len(await _send_stream_entries(redis_db, chat_id)) == 1


async def test_drain_once_skips_a_row_cancelled_after_it_became_due(
    db_session: AsyncSession, redis_db
):
    chat_id = await _make_group(db_session, 1, [2])
    row = await _make_due(db_session, redis_db, chat_id, 1)
    # Re-add to the due set (cancel would have removed it) to simulate a race
    # where the id is claimed but the row is already cancelled.
    await crud_scheduled.cancel_scheduled(db_session, row.id, 1)
    await redis_db.zadd(SCHEDULED_DUE_SET_KEY, {str(row.id): time.time() - 1})

    await scheduled_worker.drain_once()

    assert await _send_stream_entries(redis_db, chat_id) == []
    await db_session.refresh(row)
    assert row.status == ScheduledMessageStatus.CANCELLED


async def test_reconcile_once_repopulates_the_due_set_from_postgres(
    db_session: AsyncSession, redis_db
):
    chat_id = await _make_group(db_session, 1, [2])
    r1 = await scheduled_service.schedule_message(
        db_session, sender_id=1, chat_id=chat_id, client_message_id=str(uuid.uuid4()),
        scheduled_for=datetime.now(timezone.utc) + timedelta(hours=1), content="a",
    )
    r2 = await scheduled_service.schedule_message(
        db_session, sender_id=1, chat_id=chat_id, client_message_id=str(uuid.uuid4()),
        scheduled_for=datetime.now(timezone.utc) + timedelta(hours=2), content="b",
    )
    # Simulate a Redis flush of the due set.
    await redis_db.delete(SCHEDULED_DUE_SET_KEY)

    added = await scheduled_worker.reconcile_once()

    assert added == 2
    assert await redis_db.zscore(SCHEDULED_DUE_SET_KEY, str(r1.id)) == pytest.approx(
        r1.scheduled_for.timestamp(), abs=1
    )
    assert await redis_db.zscore(SCHEDULED_DUE_SET_KEY, str(r2.id)) == pytest.approx(
        r2.scheduled_for.timestamp(), abs=1
    )


async def test_reconcile_once_ignores_non_pending_rows(db_session: AsyncSession, redis_db):
    chat_id = await _make_group(db_session, 1, [2])
    pending = await scheduled_service.schedule_message(
        db_session, sender_id=1, chat_id=chat_id, client_message_id=str(uuid.uuid4()),
        scheduled_for=datetime.now(timezone.utc) + timedelta(hours=1), content="keep",
    )
    cancelled = await scheduled_service.schedule_message(
        db_session, sender_id=1, chat_id=chat_id, client_message_id=str(uuid.uuid4()),
        scheduled_for=datetime.now(timezone.utc) + timedelta(hours=1), content="gone",
    )
    await scheduled_service.cancel_scheduled(
        db_session, sender_id=1, scheduled_message_id=cancelled.id
    )
    await redis_db.delete(SCHEDULED_DUE_SET_KEY)

    added = await scheduled_worker.reconcile_once()

    assert added == 1
    assert await redis_db.zscore(SCHEDULED_DUE_SET_KEY, str(pending.id)) is not None
    assert await redis_db.zscore(SCHEDULED_DUE_SET_KEY, str(cancelled.id)) is None
