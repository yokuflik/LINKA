"""
Avatar (profile-picture) commit logic - services.avatar_service.

Direct-to-storage model like message media: the client PUTs the file to
MinIO itself, then calls back with the storage_key to commit it. These tests
do the real upload round trip against the `test_minio` container and skip
cleanly if it isn't reachable, matching test_storage_media_service.py.
"""
import urllib.request

import pytest
import pytest_asyncio

from database.crud.crud_user import create_user
from services import avatar_service, chat_service
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


async def _upload_avatar(mime: str = "image/png", body: bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32) -> str:
    ticket = avatar_service.request_upload(mime, len(body))
    assert _put(ticket.upload_url, body, ticket.required_headers) == 200
    return ticket.storage_key


# --------------------------------------------------------------------------
# request_upload - ticket validation (sync, no network)
# --------------------------------------------------------------------------
def test_request_upload_rejects_a_disallowed_mime():
    with pytest.raises(MediaValidationError):
        avatar_service.request_upload("application/x-msdownload", 10)


def test_request_upload_rejects_an_oversize_declaration():
    with pytest.raises(MediaValidationError):
        avatar_service.request_upload("image/png", 999 * 1024 * 1024)


# --------------------------------------------------------------------------
# set_avatar
# --------------------------------------------------------------------------
async def test_set_avatar_stores_the_key(db_session, redis_db):
    await create_user(db_session, user_id=1, phone_number="+972501")
    key = await _upload_avatar()

    updated = await avatar_service.set_avatar(db_session, user_id=1, storage_key=key)

    assert updated.profile_pic_url == key


async def test_set_avatar_for_a_nonexistent_user_returns_none(db_session, redis_db):
    key = await _upload_avatar()
    assert await avatar_service.set_avatar(db_session, user_id=999999, storage_key=key) is None


async def test_set_avatar_rejects_a_key_that_was_never_uploaded(db_session, redis_db):
    await create_user(db_session, user_id=1, phone_number="+972501")

    with pytest.raises(MediaNotFoundError):
        await avatar_service.set_avatar(db_session, user_id=1, storage_key="ab/avatar/never-uploaded.png")


async def test_set_avatar_rejects_a_non_avatar_key(db_session, redis_db):
    await create_user(db_session, user_id=1, phone_number="+972501")

    # A key that doesn't look like an avatar object never even hits storage.
    with pytest.raises(MediaValidationError):
        await avatar_service.set_avatar(db_session, user_id=1, storage_key="ab/image/not-an-avatar.png")


async def test_set_avatar_replaces_and_deletes_the_previous_object(db_session, redis_db):
    await create_user(db_session, user_id=1, phone_number="+972501")
    first_key = await _upload_avatar()
    await avatar_service.set_avatar(db_session, user_id=1, storage_key=first_key)

    second_key = await _upload_avatar()
    await avatar_service.set_avatar(db_session, user_id=1, storage_key=second_key)

    # The old object is best-effort deleted once it's no longer referenced.
    assert await media.object_exists(first_key, bucket=media.S3_BUCKET_AVATARS) is False
    assert await media.object_exists(second_key, bucket=media.S3_BUCKET_AVATARS) is True


# --------------------------------------------------------------------------
# clear_avatar
# --------------------------------------------------------------------------
async def test_clear_avatar_nulls_the_column_and_deletes_the_object(db_session, redis_db):
    await create_user(db_session, user_id=1, phone_number="+972501")
    key = await _upload_avatar()
    await avatar_service.set_avatar(db_session, user_id=1, storage_key=key)

    cleared = await avatar_service.clear_avatar(db_session, user_id=1)

    assert cleared.profile_pic_url is None
    assert await media.object_exists(key, bucket=media.S3_BUCKET_AVATARS) is False


async def test_clear_avatar_for_a_nonexistent_user_returns_none(db_session, redis_db):
    assert await avatar_service.clear_avatar(db_session, user_id=999999) is None


async def test_clear_avatar_when_none_set_is_a_noop(db_session, redis_db):
    await create_user(db_session, user_id=1, phone_number="+972501")
    cleared = await avatar_service.clear_avatar(db_session, user_id=1)
    assert cleared.profile_pic_url is None


# --------------------------------------------------------------------------
# Group avatars (via chat_service, which adds the admin/owner role check)
# --------------------------------------------------------------------------
async def test_set_group_avatar_requires_admin_or_owner(db_session, redis_db):
    await create_user(db_session, user_id=1, phone_number="+972501")
    await create_user(db_session, user_id=2, phone_number="+972502")
    group = await chat_service.create_group_chat(db_session, creator_id=1, title="Team", initial_member_ids=[2])
    key = await _upload_avatar()

    with pytest.raises(chat_service.PermissionDeniedError):
        await chat_service.set_group_avatar(db_session, actor_id=2, chat_id=group.id, storage_key=key)

    updated = await chat_service.set_group_avatar(db_session, actor_id=1, chat_id=group.id, storage_key=key)
    assert updated.profile_pic_url == key


async def test_clear_group_avatar_deletes_the_object(db_session, redis_db):
    await create_user(db_session, user_id=1, phone_number="+972501")
    group = await chat_service.create_group_chat(db_session, creator_id=1, title="Team")
    key = await _upload_avatar()
    await chat_service.set_group_avatar(db_session, actor_id=1, chat_id=group.id, storage_key=key)

    cleared = await chat_service.clear_group_avatar(db_session, actor_id=1, chat_id=group.id)

    assert cleared.profile_pic_url is None
    assert await media.object_exists(key, bucket=media.S3_BUCKET_AVATARS) is False


async def test_set_group_avatar_for_a_nonexistent_chat_is_denied_not_crashed(db_session, redis_db):
    await create_user(db_session, user_id=1, phone_number="+972501")
    key = await _upload_avatar()

    with pytest.raises(chat_service.PermissionDeniedError):
        await chat_service.set_group_avatar(db_session, actor_id=1, chat_id=999999, storage_key=key)
