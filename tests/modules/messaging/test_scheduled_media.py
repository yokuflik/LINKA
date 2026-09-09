"""
Media ref-count bookkeeping for scheduled messages (ADR 0026 / ADR 0010):
scheduling a media message pins the blob with +1 ref so a concurrent purge
can't delete the bytes; cancelling releases it, and the last release deletes
the object + blob row.
"""
import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from modules.media import media_service
from modules.media.crud import get_blob_by_key, reserve_blob
from modules.messaging import scheduled_service
from modules.users.crud import create_user
from modules.chats import service as chat_service

pytestmark = pytest.mark.asyncio


async def _make_private(session: AsyncSession, a: int, b: int) -> int:
    await create_user(session, user_id=a, phone_number=f"+97250{a}")
    await create_user(session, user_id=b, phone_number=f"+97250{b}")
    chat = await chat_service.get_or_create_private_chat(session, a, b)
    return chat.id


async def _reserve(session, body: bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16) -> str:
    digest = hashlib.sha256(body + uuid.uuid4().bytes).hexdigest()
    ticket = media_service.build_media_upload_ticket(
        "image", "image/png", len(body), digest, already_uploaded=False
    )
    await reserve_blob(
        session, sha256=digest, storage_key=ticket.storage_key, bucket=ticket.bucket,
        kind="image", mime="image/png", size=len(body),
    )
    return ticket.storage_key


def _soon() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=1)


async def test_scheduling_a_media_message_adds_one_blob_ref(db_session: AsyncSession, redis_db):
    chat_id = await _make_private(db_session, 1, 2)
    key = await _reserve(db_session)
    assert (await get_blob_by_key(db_session, key)).ref_count == 0

    await scheduled_service.schedule_message(
        db_session, sender_id=1, chat_id=chat_id, client_message_id=str(uuid.uuid4()),
        scheduled_for=_soon(), message_type=2, media={"key": key, "name": "p.png"},
    )

    db_session.expire_all()
    assert (await get_blob_by_key(db_session, key)).ref_count == 1


async def test_cancelling_a_media_message_derefs_and_deletes_the_last_ref(
    db_session: AsyncSession, redis_db, monkeypatch
):
    deleted = []

    async def fake_delete_object(storage_key):
        deleted.append(storage_key)

    monkeypatch.setattr(media_service, "delete_object", fake_delete_object)

    chat_id = await _make_private(db_session, 1, 2)
    key = await _reserve(db_session)
    row = await scheduled_service.schedule_message(
        db_session, sender_id=1, chat_id=chat_id, client_message_id=str(uuid.uuid4()),
        scheduled_for=_soon(), message_type=2, media={"key": key, "name": "p.png"},
    )

    await scheduled_service.cancel_scheduled(
        db_session, sender_id=1, scheduled_message_id=row.id
    )

    assert deleted == [key]
    db_session.expire_all()
    assert await get_blob_by_key(db_session, key) is None


async def test_scheduling_media_with_an_unknown_key_is_rejected(db_session: AsyncSession, redis_db):
    chat_id = await _make_private(db_session, 1, 2)

    with pytest.raises(scheduled_service.ScheduledTimeInvalidError):
        await scheduled_service.schedule_message(
            db_session, sender_id=1, chat_id=chat_id, client_message_id=str(uuid.uuid4()),
            scheduled_for=_soon(), message_type=2, media={"key": "media/does-not-exist"},
        )
