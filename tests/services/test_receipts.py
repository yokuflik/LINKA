"""
Detailed receipt log + per-message "info" view.

Covers the write path (mark_* -> Redis Stream -> worker -> message_receipt_log,
with collapsing and forward-only movement) and the read path
(message_service.get_message_receipts: 1:1 timestamps, group name lists +
pending, and count-only truncation for a large group).

All against real Postgres + Redis, like the rest of tests/services.
"""
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from config import RECEIPT_KIND_READ
from database.crud.crud_user import create_user
from services import chat_service, message_service
from services.receipts import receipt_log

pytestmark = pytest.mark.asyncio


async def _group(session: AsyncSession, owner_id: int, member_ids) -> int:
    await create_user(session, user_id=owner_id, phone_number=f"+97250{owner_id}")
    for m in member_ids:
        await create_user(session, user_id=m, phone_number=f"+97250{m}")
    chat = await chat_service.create_group_chat(
        session, creator_id=owner_id, title="G", initial_member_ids=list(member_ids)
    )
    return chat.id


async def _send(session, sender_id, chat_id, content="hi", type=1):
    return await message_service.send_message(
        session, sender_id=sender_id, chat_id=chat_id,
        client_message_id=str(uuid.uuid4()), content=content, type=type,
    )


async def _drain(session):
    await receipt_log.ensure_group()
    total = 0
    while True:
        n = await receipt_log.drain_once(session, block_ms=0, claim_stale=False)
        total += n
        if n == 0:
            break
    return total


async def test_mark_read_appends_one_collapsed_log_row(db_session, redis_db):
    chat_id = await _group(db_session, 1, [2])
    m1 = await _send(db_session, 1, chat_id)
    m2 = await _send(db_session, 1, chat_id)
    m3 = await _send(db_session, 1, chat_id)

    # Three separate read acks by user 2, walking the watermark forward.
    await message_service.mark_as_read(db_session, user_id=2, chat_id=chat_id, message_id=m1.id)
    await message_service.mark_as_read(db_session, user_id=2, chat_id=chat_id, message_id=m2.id)
    await message_service.mark_as_read(db_session, user_id=2, chat_id=chat_id, message_id=m3.id)

    await _drain(db_session)

    rows = await _read_rows(db_session, chat_id, user_id=2, kind=RECEIPT_KIND_READ)
    # Collapsed to a single row at the furthest watermark.
    assert len(rows) == 1
    assert rows[0].up_to_message_id == m3.id


async def test_repeat_mark_read_behind_the_watermark_is_a_noop(db_session, redis_db):
    chat_id = await _group(db_session, 1, [2])
    m1 = await _send(db_session, 1, chat_id)
    m2 = await _send(db_session, 1, chat_id)

    await message_service.mark_as_read(db_session, user_id=2, chat_id=chat_id, message_id=m2.id)
    await _drain(db_session)
    # Now ack an older message - watermark can't go backwards, nothing enqueued.
    await message_service.mark_as_read(db_session, user_id=2, chat_id=chat_id, message_id=m1.id)
    assert await _drain(db_session) == 0

    rows = await _read_rows(db_session, chat_id, user_id=2, kind=RECEIPT_KIND_READ)
    assert len(rows) == 1
    assert rows[0].up_to_message_id == m2.id


async def test_receipts_view_for_a_group_lists_who_read_and_who_is_pending(db_session, redis_db):
    chat_id = await _group(db_session, 1, [2, 3])
    m = await _send(db_session, 1, chat_id)

    await message_service.mark_as_read(db_session, user_id=2, chat_id=chat_id, message_id=m.id)
    await _drain(db_session)

    view = await message_service.get_message_receipts(db_session, user_id=1, chat_id=chat_id, message_id=m.id)

    assert view["truncated"] is False
    assert view["participant_count"] == 2  # everyone but the sender
    assert {e["user_id"] for e in view["read_by"]} == {"2"}
    assert view["pending"] == ["3"]
    assert view["counts"]["read"] == 1


async def test_receipts_view_any_member_may_query_any_message(db_session, redis_db):
    chat_id = await _group(db_session, 1, [2, 3])
    m = await _send(db_session, 1, chat_id)
    await message_service.mark_as_read(db_session, user_id=3, chat_id=chat_id, message_id=m.id)
    await _drain(db_session)

    # User 2 (not the sender) asks about user 1's message - allowed.
    view = await message_service.get_message_receipts(db_session, user_id=2, chat_id=chat_id, message_id=m.id)
    assert {e["user_id"] for e in view["read_by"]} == {"3"}


async def test_receipts_view_rejects_a_non_participant(db_session, redis_db):
    chat_id = await _group(db_session, 1, [2])
    m = await _send(db_session, 1, chat_id)
    await create_user(db_session, user_id=9, phone_number="+972509")

    with pytest.raises(message_service.NotAParticipantError):
        await message_service.get_message_receipts(db_session, user_id=9, chat_id=chat_id, message_id=m.id)


async def test_receipts_view_missing_message(db_session, redis_db):
    chat_id = await _group(db_session, 1, [2])
    with pytest.raises(message_service.MessageNotFoundError):
        await message_service.get_message_receipts(db_session, user_id=1, chat_id=chat_id, message_id=123456789)


async def test_receipts_view_truncates_to_counts_for_a_large_group(db_session, redis_db, monkeypatch):
    monkeypatch.setattr(message_service, "RECEIPT_NAMED_LIST_MAX_MEMBERS", 2)
    chat_id = await _group(db_session, 1, [2, 3, 4])  # 3 eligible > 2
    m = await _send(db_session, 1, chat_id)
    await message_service.mark_as_read(db_session, user_id=2, chat_id=chat_id, message_id=m.id)
    await message_service.mark_as_read(db_session, user_id=3, chat_id=chat_id, message_id=m.id)
    await _drain(db_session)

    view = await message_service.get_message_receipts(db_session, user_id=1, chat_id=chat_id, message_id=m.id)
    assert view["truncated"] is True
    assert view.get("read_by", []) == []  # name lists omitted when truncated
    assert view["counts"]["read"] == 2
    assert view["participant_count"] == 3


async def test_played_only_populated_for_a_voice_message(db_session, redis_db):
    chat_id = await _group(db_session, 1, [2])
    text_msg = await _send(db_session, 1, chat_id, content="not audio")
    await message_service.mark_as_read(db_session, user_id=2, chat_id=chat_id, message_id=text_msg.id)
    await _drain(db_session)

    view = await message_service.get_message_receipts(db_session, user_id=1, chat_id=chat_id, message_id=text_msg.id)
    assert view["played_by"] == []
    assert view["counts"]["played"] == 0


# --- helpers -----------------------------------------------------------------

async def _read_rows(session, chat_id, user_id, kind):
    from sqlalchemy import select

    from database.models.message_receipt_log import MessageReceiptLog

    stmt = (
        select(MessageReceiptLog)
        .where(
            MessageReceiptLog.chat_id == chat_id,
            MessageReceiptLog.user_id == user_id,
            MessageReceiptLog.kind == kind,
        )
        .order_by(MessageReceiptLog.id)
    )
    return (await session.execute(stmt)).scalars().all()
