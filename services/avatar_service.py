"""
Profile-picture (avatar) upload logic.

Same direct-to-storage model as message media: the app server never handles
image bytes. The client asks for an upload ticket, PUTs the file straight to
object storage, then calls back with the resulting ``storage_key`` to commit
it as their avatar. ``profile_pic_url`` on ``User`` stores that storage key,
not a full URL - callers resolve it for display via
``media_service.public_avatar_url`` (see ``UserOut``).
"""

import logging
from typing import Optional

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from config import ALLOWED_UPLOAD_MIME, MAX_UPLOAD_BYTES_AVATAR, S3_BUCKET_AVATARS
from database.crud.crud_chat import get_chat_by_id, update_chat_details
from database.crud.crud_user import get_user_by_id, update_user_profile
from database.models.chat import Chat
from database.models.user import User
from services.storage import media_service
from services.storage.errors import MediaValidationError, StorageError

logger = logging.getLogger(__name__)

_AVATAR_KIND = "avatar"


def request_upload(mime: str, size_bytes: int) -> media_service.UploadTicket:
    """
    Presigned PUT for one avatar upload. Raises MediaValidationError (-> 400)
    for a disallowed MIME type or an oversize declaration.
    """
    return media_service.create_upload_ticket(_AVATAR_KIND, mime, size_bytes)


async def set_avatar(session: AsyncSession, user_id: int, storage_key: str) -> Optional[User]:
    """
    Commit a previously uploaded object as the user's avatar.

    Verifies the object actually exists in the avatars bucket and that its
    authoritative (storage-side) content-type and size are within the avatar
    limits - the presigned PUT already enforces the client's *declared*
    values, this re-checks what really landed. Replaces and best-effort
    deletes any previous avatar object.

    Returns None if the user doesn't exist.
    """
    # Raises MediaNotFoundError (-> 404) if the client never actually uploaded.
    await _validate_stored_avatar_object(storage_key)

    user = await get_user_by_id(session, user_id)
    if user is None:
        return None
    previous_key = user.profile_pic_url

    updated = await update_user_profile(session, user_id=user_id, profile_pic_url=storage_key)

    if previous_key and previous_key != storage_key and f"/{_AVATAR_KIND}/" in previous_key:
        await _delete_object_quietly(previous_key)

    return updated


async def clear_avatar(session: AsyncSession, user_id: int) -> Optional[User]:
    """Remove the user's avatar and best-effort delete the stored object."""
    user = await get_user_by_id(session, user_id)
    if user is None:
        return None
    previous_key = user.profile_pic_url

    stmt_user = await _force_clear(session, user_id)

    if previous_key and f"/{_AVATAR_KIND}/" in previous_key:
        await _delete_object_quietly(previous_key)
    return stmt_user


async def _force_clear(session: AsyncSession, user_id: int) -> Optional[User]:
    # update_user_profile ignores None values (partial update), so clearing
    # needs its own explicit write.
    from sqlalchemy import update

    stmt = (
        update(User)
        .where(User.id == user_id)
        .values(profile_pic_url=None)
        .returning(User)
    )
    result = await session.execute(stmt)
    await session.commit()
    return result.scalar_one_or_none()


async def _validate_stored_avatar_object(storage_key: str) -> None:
    """
    Shared check for both user and group avatars: the object really exists in
    the avatars bucket and its authoritative (storage-side) content-type and
    size are within the avatar limits. Raises MediaValidationError (-> 400) or
    MediaNotFoundError (-> 404).
    """
    if not storage_key or f"/{_AVATAR_KIND}/" not in storage_key:
        raise MediaValidationError("storage_key does not look like an avatar object key")

    meta = await media_service.object_metadata(storage_key, bucket=S3_BUCKET_AVATARS)
    if meta.content_type not in ALLOWED_UPLOAD_MIME[_AVATAR_KIND]:
        raise MediaValidationError(f"stored object type {meta.content_type!r} is not a valid avatar")
    if meta.size <= 0 or meta.size > MAX_UPLOAD_BYTES_AVATAR:
        raise MediaValidationError(f"stored object size {meta.size} is outside the avatar limit")


async def set_group_avatar(session: AsyncSession, chat_id: int, storage_key: str) -> Optional[Chat]:
    """
    Commit a previously uploaded object as a group's profile picture.

    Same model as set_avatar for users: the caller has already had its
    admin/owner role checked (chat_service.set_group_avatar), the object is
    verified against the avatar limits here, and any previous group avatar
    object is best-effort deleted. Returns None if the chat doesn't exist.
    """
    await _validate_stored_avatar_object(storage_key)

    chat = await get_chat_by_id(session, chat_id)
    if chat is None:
        return None
    previous_key = chat.profile_pic_url

    updated = await update_chat_details(session, chat_id=chat_id, profile_pic_url=storage_key)

    if previous_key and previous_key != storage_key and f"/{_AVATAR_KIND}/" in previous_key:
        await _delete_object_quietly(previous_key)

    return updated


async def clear_group_avatar(session: AsyncSession, chat_id: int) -> Optional[Chat]:
    """Remove a group's profile picture and best-effort delete the stored object."""
    chat = await get_chat_by_id(session, chat_id)
    if chat is None:
        return None
    previous_key = chat.profile_pic_url

    # update_chat_details ignores None (partial update), so clearing needs its
    # own explicit write - same as _force_clear for users.
    stmt = update(Chat).where(Chat.id == chat_id).values(profile_pic_url=None).returning(Chat)
    result = await session.execute(stmt)
    await session.commit()
    updated = result.scalar_one_or_none()

    if previous_key and f"/{_AVATAR_KIND}/" in previous_key:
        await _delete_object_quietly(previous_key)
    return updated


async def _delete_object_quietly(storage_key: str) -> None:
    try:
        await media_service.delete_object(storage_key, bucket=S3_BUCKET_AVATARS)
    except StorageError as exc:  # cleanup failure must not fail the request
        logger.warning("failed to delete old avatar object %s: %s", storage_key, exc)
