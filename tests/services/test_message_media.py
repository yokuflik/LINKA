"""
Media-message send flow: message_service.send_message with a media payload,
against real Postgres/Redis + a real MinIO round trip. Skips cleanly if
object storage isn't reachable (matching test_storage_media_service.py).
"""
import hashlib
import urllib.request
import uuid

import pytest
import pytest_asyncio

from database.crud.crud_media_blob import reserve_blob
from database.crud.crud_user import create_user
from services import chat_service, message_service
from services.storage import media_service as media
from services.storage.errors import MediaNotFoundError, MediaValidationError

pytestmark = pytest.mark.asyncio


def _put(url: str, data: bytes, headers: dict) -> int:
    req = urllib.request.Request(url, data=data, method="PUT", headers=headers)
    return urllib.request.urlopen(req, timeout=5).status


@pytest_asyncio.fixture(scope="module", autouse=True)
async def _buckets_or_skip():
    try:
        await media.ensure_buckets()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"object storage not reachable: {exc}")


async def _make_private(session, a: int, b: int) -> int:
    await create_user(session, user_id=a, phone_number=f"+97250{a}")
    await create_user(session, user_id=b, phone_number=f"+97250{b}")
    chat = await chat_service.get_or_create_private_chat(session, a, b)
    return chat.id


async def _upload(session, kind: str, mime: str, body: bytes) -> str:
    """Content-addressed upload (ADR 0010): hash, reserve blob row, PUT bytes."""
    digest = hashlib.sha256(body).hexdigest()
    ticket = media.build_media_upload_ticket(
        kind, mime, len(body), digest, already_uploaded=False
    )
    await reserve_blob(
        session, sha256=digest, storage_key=ticket.storage_key, bucket=ticket.bucket,
        kind=kind, mime=mime, size=len(body),
    )
    assert _put(ticket.upload_url, body, ticket.required_headers) == 200
    return ticket.storage_key


async def test_send_image_message_stores_media_fields(db_session, redis_db):
    chat_id = await _make_private(db_session, 1, 2)
    body = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    key = await _upload(db_session, "image", "image/png", body)

    message = await message_service.process_outgoing(
        db_session, sender_id=1, chat_id=chat_id, client_message_id=str(uuid.uuid4()),
        type=2, media={"key": key},
    )

    assert message.type == 2
    assert message.media_key == key
    assert message.media_mime == "image/png"
    assert message.media_size == len(body)


async def test_send_file_message_keeps_original_name(db_session, redis_db):
    chat_id = await _make_private(db_session, 3, 4)
    key = await _upload(db_session, "file", "application/pdf", b"%PDF-1.4\n" + b"0" * 64)

    message = await message_service.process_outgoing(
        db_session, sender_id=3, chat_id=chat_id, client_message_id=str(uuid.uuid4()),
        type=5, media={"key": key, "name": "quarterly-report.pdf"},
    )

    assert message.media_name == "quarterly-report.pdf"


async def test_media_message_with_unknown_key_is_rejected(db_session, redis_db):
    chat_id = await _make_private(db_session, 5, 6)

    # No blob row for this key -> rejected before the storage HEAD (ADR 0010).
    with pytest.raises(MediaValidationError):
        await message_service.process_outgoing(
            db_session, sender_id=5, chat_id=chat_id, client_message_id=str(uuid.uuid4()),
            type=2, media={"key": "de/image/does-not-exist.png"},
        )


async def test_media_key_reserved_but_never_uploaded_is_rejected(db_session, redis_db):
    chat_id = await _make_private(db_session, 11, 12)
    digest = hashlib.sha256(b"ghost-bytes").hexdigest()
    ticket = media.build_media_upload_ticket(
        "image", "image/png", 11, digest, already_uploaded=False
    )
    await reserve_blob(
        db_session, sha256=digest, storage_key=ticket.storage_key, bucket=ticket.bucket,
        kind="image", mime="image/png", size=11,
    )
    with pytest.raises(MediaNotFoundError):
        await message_service.process_outgoing(
            db_session, sender_id=11, chat_id=chat_id, client_message_id=str(uuid.uuid4()),
            type=2, media={"key": ticket.storage_key},
        )


async def test_identical_bytes_dedupe_to_one_object(db_session, redis_db):
    chat_id = await _make_private(db_session, 13, 14)
    body = b"\x89PNG\r\n\x1a\n" + b"viral" * 20
    digest = hashlib.sha256(body).hexdigest()

    key1 = await _upload(db_session, "image", "image/png", body)
    await message_service.process_outgoing(
        db_session, sender_id=13, chat_id=chat_id, client_message_id=str(uuid.uuid4()),
        type=2, media={"key": key1},
    )

    # A second "upload" of the same bytes: the ticket endpoint would report
    # already_uploaded and hand back the same key.
    from database.crud.crud_media_blob import get_blob_by_hash

    blob = await get_blob_by_hash(db_session, digest)
    assert blob is not None and blob.uploaded_at is not None
    ticket2 = media.build_media_upload_ticket(
        "image", "image/png", len(body), digest, already_uploaded=True
    )
    assert ticket2.already_uploaded and ticket2.upload_url == ""
    assert ticket2.storage_key == key1

    await message_service.process_outgoing(
        db_session, sender_id=14, chat_id=chat_id, client_message_id=str(uuid.uuid4()),
        type=2, media={"key": ticket2.storage_key},
    )
    blob = await get_blob_by_hash(db_session, digest)
    assert blob.ref_count == 2


async def test_media_type_without_payload_is_rejected(db_session, redis_db):
    chat_id = await _make_private(db_session, 7, 8)

    with pytest.raises(MediaValidationError):
        await message_service.process_outgoing(
            db_session, sender_id=7, chat_id=chat_id, client_message_id=str(uuid.uuid4()),
            type=3,
        )


async def test_history_attaches_a_presigned_media_url(db_session, redis_db):
    chat_id = await _make_private(db_session, 9, 10)
    key = await _upload(db_session, "audio", "audio/mpeg", b"ID3" + b"\x00" * 128)

    await message_service.process_outgoing(
        db_session, sender_id=9, chat_id=chat_id, client_message_id=str(uuid.uuid4()),
        type=4, media={"key": key, "duration_seconds": 7},
    )

    history = await message_service.get_message_history(db_session, user_id=9, chat_id=chat_id)
    media_msg = next(m for m in history if m.media_key == key)
    assert media_msg.media_url and key in media_msg.media_url
    assert media_msg.media_duration_seconds == 7
