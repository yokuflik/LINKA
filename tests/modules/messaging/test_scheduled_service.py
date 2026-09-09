"""
Scheduled-messages service layer (ADR 0031): schedule / list / reschedule /
cancel, plus lead-time + pending-limit validation, the participant check, and
the schedule-time media ref/deref bookkeeping.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from config import SCHEDULED_DUE_SET_KEY
from modules.messaging import crud_scheduled
from modules.messaging import scheduled_service
from modules.messaging.models import ScheduledMessageStatus
from modules.users.crud import create_user
from modules.chats import service as chat_service

pytestmark = pytest.mark.asyncio


async def _make_group(session: AsyncSession, owner_id: int, member_ids) -> int:
    await create_user(session, user_id=owner_id, phone_number=f"+97250{owner_id}")
    for member_id in member_ids:
        await create_user(session, user_id=member_id, phone_number=f"+97250{member_id}")
    group = await chat_service.create_group_chat(
        session, creator_id=owner_id, title="Test", initial_member_ids=list(member_ids)
    )
    return group.id


def _soon(seconds: int = 3600) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


async def test_schedule_message_persists_a_pending_row_and_adds_it_to_the_due_set(
    db_session: AsyncSession, redis_db
):
    chat_id = await _make_group(db_session, 1, [2])
    when = _soon()

    row = await scheduled_service.schedule_message(
        db_session, sender_id=1, chat_id=chat_id,
        client_message_id=str(uuid.uuid4()), scheduled_for=when, content="later",
    )

    assert row.status == ScheduledMessageStatus.PENDING
    assert row.content == "later"
    score = await redis_db.zscore(SCHEDULED_DUE_SET_KEY, str(row.id))
    assert score == pytest.approx(when.timestamp(), abs=1)


async def test_schedule_message_rejects_a_non_participant(db_session: AsyncSession, redis_db):
    chat_id = await _make_group(db_session, 1, [2])
    await create_user(db_session, user_id=3, phone_number="+972503")

    with pytest.raises(scheduled_service.NotAParticipantError):
        await scheduled_service.schedule_message(
            db_session, sender_id=3, chat_id=chat_id,
            client_message_id=str(uuid.uuid4()), scheduled_for=_soon(), content="hi",
        )


async def test_schedule_message_rejects_a_time_too_soon(db_session: AsyncSession, redis_db):
    chat_id = await _make_group(db_session, 1, [2])

    with pytest.raises(scheduled_service.ScheduledTimeInvalidError):
        await scheduled_service.schedule_message(
            db_session, sender_id=1, chat_id=chat_id,
            client_message_id=str(uuid.uuid4()),
            scheduled_for=datetime.now(timezone.utc) + timedelta(seconds=1),
            content="hi",
        )


async def test_schedule_message_rejects_a_time_too_far_out(db_session: AsyncSession, redis_db):
    chat_id = await _make_group(db_session, 1, [2])

    with pytest.raises(scheduled_service.ScheduledTimeInvalidError):
        await scheduled_service.schedule_message(
            db_session, sender_id=1, chat_id=chat_id,
            client_message_id=str(uuid.uuid4()),
            scheduled_for=datetime.now(timezone.utc) + timedelta(days=400),
            content="hi",
        )


async def test_schedule_message_rejects_a_system_message_type(db_session: AsyncSession, redis_db):
    chat_id = await _make_group(db_session, 1, [2])

    with pytest.raises(scheduled_service.ScheduledTimeInvalidError):
        await scheduled_service.schedule_message(
            db_session, sender_id=1, chat_id=chat_id,
            client_message_id=str(uuid.uuid4()), scheduled_for=_soon(),
            message_type=6, content="X joined",
        )


async def test_schedule_message_enforces_the_pending_limit(
    db_session: AsyncSession, redis_db, monkeypatch
):
    monkeypatch.setattr(scheduled_service, "SCHEDULED_MAX_PENDING_PER_USER", 2)
    chat_id = await _make_group(db_session, 1, [2])

    for _ in range(2):
        await scheduled_service.schedule_message(
            db_session, sender_id=1, chat_id=chat_id,
            client_message_id=str(uuid.uuid4()), scheduled_for=_soon(), content="x",
        )

    with pytest.raises(scheduled_service.ScheduledLimitExceededError):
        await scheduled_service.schedule_message(
            db_session, sender_id=1, chat_id=chat_id,
            client_message_id=str(uuid.uuid4()), scheduled_for=_soon(), content="x",
        )


async def test_list_scheduled_returns_only_pending_rows_oldest_first(
    db_session: AsyncSession, redis_db
):
    chat_id = await _make_group(db_session, 1, [2])
    later = await scheduled_service.schedule_message(
        db_session, sender_id=1, chat_id=chat_id,
        client_message_id=str(uuid.uuid4()), scheduled_for=_soon(7200), content="later",
    )
    sooner = await scheduled_service.schedule_message(
        db_session, sender_id=1, chat_id=chat_id,
        client_message_id=str(uuid.uuid4()), scheduled_for=_soon(60), content="sooner",
    )
    cancelled = await scheduled_service.schedule_message(
        db_session, sender_id=1, chat_id=chat_id,
        client_message_id=str(uuid.uuid4()), scheduled_for=_soon(120), content="gone",
    )
    await scheduled_service.cancel_scheduled(
        db_session, sender_id=1, scheduled_message_id=cancelled.id
    )

    rows = await scheduled_service.list_scheduled(db_session, sender_id=1)
    assert [r.id for r in rows] == [sooner.id, later.id]


async def test_list_scheduled_filters_by_chat(db_session: AsyncSession, redis_db):
    chat_a = await _make_group(db_session, 1, [2])
    chat_b = await _make_group(db_session, 1, [2])
    await scheduled_service.schedule_message(
        db_session, sender_id=1, chat_id=chat_a,
        client_message_id=str(uuid.uuid4()), scheduled_for=_soon(), content="a",
    )
    b = await scheduled_service.schedule_message(
        db_session, sender_id=1, chat_id=chat_b,
        client_message_id=str(uuid.uuid4()), scheduled_for=_soon(), content="b",
    )

    rows = await scheduled_service.list_scheduled(db_session, sender_id=1, chat_id=chat_b)
    assert [r.id for r in rows] == [b.id]


async def test_reschedule_changes_the_time_and_reindexes_the_due_set(
    db_session: AsyncSession, redis_db
):
    chat_id = await _make_group(db_session, 1, [2])
    row = await scheduled_service.schedule_message(
        db_session, sender_id=1, chat_id=chat_id,
        client_message_id=str(uuid.uuid4()), scheduled_for=_soon(60), content="x",
    )
    new_when = _soon(9000)

    updated = await scheduled_service.reschedule(
        db_session, sender_id=1, scheduled_message_id=row.id, scheduled_for=new_when
    )

    assert updated.scheduled_for.timestamp() == pytest.approx(new_when.timestamp(), abs=1)
    score = await redis_db.zscore(SCHEDULED_DUE_SET_KEY, str(row.id))
    assert score == pytest.approx(new_when.timestamp(), abs=1)


async def test_reschedule_can_clear_the_caption(db_session: AsyncSession, redis_db):
    chat_id = await _make_group(db_session, 1, [2])
    row = await scheduled_service.schedule_message(
        db_session, sender_id=1, chat_id=chat_id,
        client_message_id=str(uuid.uuid4()), scheduled_for=_soon(), content="typo",
    )

    updated = await scheduled_service.reschedule(
        db_session, sender_id=1, scheduled_message_id=row.id,
        content=None, set_content=True,
    )
    assert updated.content is None


async def test_reschedule_rejects_a_row_the_caller_does_not_own(
    db_session: AsyncSession, redis_db
):
    chat_id = await _make_group(db_session, 1, [2])
    row = await scheduled_service.schedule_message(
        db_session, sender_id=1, chat_id=chat_id,
        client_message_id=str(uuid.uuid4()), scheduled_for=_soon(), content="x",
    )

    with pytest.raises(scheduled_service.ScheduledMessageNotFoundError):
        await scheduled_service.reschedule(
            db_session, sender_id=2, scheduled_message_id=row.id, scheduled_for=_soon(120)
        )


async def test_cancel_marks_cancelled_and_drops_it_from_the_due_set(
    db_session: AsyncSession, redis_db
):
    chat_id = await _make_group(db_session, 1, [2])
    row = await scheduled_service.schedule_message(
        db_session, sender_id=1, chat_id=chat_id,
        client_message_id=str(uuid.uuid4()), scheduled_for=_soon(), content="x",
    )

    cancelled = await scheduled_service.cancel_scheduled(
        db_session, sender_id=1, scheduled_message_id=row.id
    )

    assert cancelled.status == ScheduledMessageStatus.CANCELLED
    assert await redis_db.zscore(SCHEDULED_DUE_SET_KEY, str(row.id)) is None


async def test_cancel_of_an_unknown_or_unowned_row_raises(db_session: AsyncSession, redis_db):
    chat_id = await _make_group(db_session, 1, [2])
    row = await scheduled_service.schedule_message(
        db_session, sender_id=1, chat_id=chat_id,
        client_message_id=str(uuid.uuid4()), scheduled_for=_soon(), content="x",
    )

    with pytest.raises(scheduled_service.ScheduledMessageNotFoundError):
        await scheduled_service.cancel_scheduled(
            db_session, sender_id=2, scheduled_message_id=row.id
        )
    with pytest.raises(scheduled_service.ScheduledMessageNotFoundError):
        await scheduled_service.cancel_scheduled(
            db_session, sender_id=1, scheduled_message_id=999999
        )


async def test_double_cancel_raises_the_second_time(db_session: AsyncSession, redis_db):
    chat_id = await _make_group(db_session, 1, [2])
    row = await scheduled_service.schedule_message(
        db_session, sender_id=1, chat_id=chat_id,
        client_message_id=str(uuid.uuid4()), scheduled_for=_soon(), content="x",
    )

    await scheduled_service.cancel_scheduled(db_session, sender_id=1, scheduled_message_id=row.id)
    with pytest.raises(scheduled_service.ScheduledMessageNotFoundError):
        await scheduled_service.cancel_scheduled(
            db_session, sender_id=1, scheduled_message_id=row.id
        )


async def test_count_pending_ignores_cancelled_rows(db_session: AsyncSession, redis_db):
    chat_id = await _make_group(db_session, 1, [2])
    keep = await scheduled_service.schedule_message(
        db_session, sender_id=1, chat_id=chat_id,
        client_message_id=str(uuid.uuid4()), scheduled_for=_soon(), content="keep",
    )
    drop = await scheduled_service.schedule_message(
        db_session, sender_id=1, chat_id=chat_id,
        client_message_id=str(uuid.uuid4()), scheduled_for=_soon(), content="drop",
    )
    await scheduled_service.cancel_scheduled(db_session, sender_id=1, scheduled_message_id=drop.id)

    assert await crud_scheduled.count_pending_for_user(db_session, 1) == 1
    assert keep.id  # referenced
