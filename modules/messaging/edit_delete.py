"""Editing and deleting an existing message."""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

from database.crud.crud_media_blob import delete_blob_row, deref_blob
from database.crud.crud_message import (
    edit_message_content,
    get_message_by_id,
    purge_message as crud_purge_message,
    soft_delete_message,
    undelete_message,
)
from services import realtime_service
from services.messaging.common import _check_content_length
from services.messaging.errors import NotAParticipantError
from services.storage import media_service


async def edit_message(session: AsyncSession, user_id: int, chat_id: int, message_id: int, new_content: str) -> "object":
    _check_content_length(new_content)

    existing = await get_message_by_id(session, chat_id=chat_id, message_id=message_id)
    if existing is None or existing.sender_id != user_id:
        raise NotAParticipantError(f"User {user_id} may not edit message {message_id}")

    message = await edit_message_content(session, chat_id=chat_id, message_id=message_id, new_content=new_content)
    await realtime_service.publish_event(
        chat_id,
        {
            "event": "message_edited",
            "chat_id": str(chat_id),
            "message_id": str(message_id),
            "content": new_content,
            "edited_at": message.edited_at.isoformat() if message and message.edited_at else None,
        },
    )
    return message


async def delete_message(session: AsyncSession, user_id: int, chat_id: int, message_id: int) -> bool:
    existing = await get_message_by_id(session, chat_id=chat_id, message_id=message_id)
    if existing is None or existing.sender_id != user_id:
        raise NotAParticipantError(f"User {user_id} may not delete message {message_id}")

    deleted = await soft_delete_message(session, chat_id=chat_id, message_id=message_id)
    if deleted:
        await realtime_service.publish_event(
            chat_id, {"event": "message_deleted", "chat_id": str(chat_id), "message_id": str(message_id)}
        )
    return deleted


async def purge_message(session: AsyncSession, user_id: int, chat_id: int, message_id: int) -> bool:
    """
    Hard "delete forever" (ADR 0021). Sender-only; the message must already be
    soft-deleted. Wipes content / media to nothing, blocks restore permanently,
    and for a media message derefs the blob - deleting the object from S3 and
    the media_blob row on the last reference.
    """
    existing = await get_message_by_id(session, chat_id=chat_id, message_id=message_id)
    if existing is None or existing.sender_id != user_id:
        raise NotAParticipantError(f"User {user_id} may not purge message {message_id}")

    media_key = await crud_purge_message(session, chat_id=chat_id, message_id=message_id)
    if media_key is None:
        return False  # not currently deleted, or already purged

    if media_key:
        try:
            remaining = await deref_blob(session, media_key)
            if remaining == 0:
                try:
                    await media_service.delete_object(media_key)
                except Exception as exc:  # best-effort - row is already gone from messages
                    logger.error("purge: failed to delete S3 object %s: %s", media_key, exc)
                await delete_blob_row(session, media_key)
        except Exception as exc:
            logger.error("purge: blob deref failed for %s: %s", media_key, exc)

    await realtime_service.publish_event(
        chat_id,
        {"event": "message_purged", "chat_id": str(chat_id), "message_id": str(message_id)},
    )
    return True


async def restore_message(session: AsyncSession, user_id: int, chat_id: int, message_id: int) -> "object":
    """Reverse a soft delete. Only the original sender may restore; no time limit."""
    existing = await get_message_by_id(session, chat_id=chat_id, message_id=message_id)
    if existing is None or existing.sender_id != user_id:
        raise NotAParticipantError(f"User {user_id} may not restore message {message_id}")

    message = await undelete_message(session, chat_id=chat_id, message_id=message_id)
    if message is not None:
        await realtime_service.publish_event(
            chat_id,
            {
                "event": "message_restored",
                "chat_id": str(chat_id),
                "message_id": str(message_id),
                "content": message.content,
                "type": message.type,
                "is_edited": message.is_edited,
                "edited_at": message.edited_at.isoformat() if message.edited_at else None,
                # Presigned GET, same as the live new_message / history path.
                "media_url": media_service.message_media_download_url(message.media_key),
                "media_blur_hash": message.media_blur_hash,
            },
        )
    return message
