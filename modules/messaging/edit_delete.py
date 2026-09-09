"""Editing and deleting an existing message."""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

from modules.media.crud import delete_blob_row
from modules.media.crud import deref_blob
from modules.messaging.crud import edit_message_content
from modules.messaging.crud import get_message_by_id
from modules.messaging.crud import purge_message as crud_purge_message
from modules.messaging.crud import soft_delete_message
from modules.messaging.crud import undelete_message
from realtime import realtime_service
from modules.messaging.common import _check_content_length
from modules.messaging.limits import DEFAULT_MESSAGING_LIMITS, MessagingLimits
from modules.messaging.errors import EncryptionRequiredError
from modules.messaging.errors import NotAParticipantError
from modules.media import media_service
from modules.users.crud import add_storage_usage


async def edit_message(
    session: AsyncSession,
    user_id: int,
    chat_id: int,
    message_id: int,
    new_content: str,
    enc_header: dict | None = None,
    *,
    limits: MessagingLimits = DEFAULT_MESSAGING_LIMITS,
) -> "object":
    _check_content_length(new_content, limits.max_message_content_length)

    existing = await get_message_by_id(session, chat_id=chat_id, message_id=message_id)
    if existing is None or existing.sender_id != user_id:
        raise NotAParticipantError(f"User {user_id} may not edit message {message_id}")

    # ADR 0027: an encrypted message can only be edited with a fresh enc header;
    # a plaintext edit would silently downgrade it and leak the text to the server.
    if enc_header is None and getattr(existing, "is_encrypted", False):
        raise EncryptionRequiredError(
            f"message {message_id} is encrypted - an edit must carry an enc header"
        )

    message = await edit_message_content(
        session,
        chat_id=chat_id,
        message_id=message_id,
        new_content=new_content,
        enc_header=enc_header,
    )
    await realtime_service.publish_event(
        chat_id,
        {
            "event": "message_edited",
            "chat_id": str(chat_id),
            "message_id": str(message_id),
            "content": new_content,
            "is_encrypted": bool(enc_header is not None),
            "enc_header": enc_header,
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

    # Capture pre-purge media size / original sender before the row is wiped -
    # needed to refund the sender's storage quota (ADR 0028).
    purged_size = existing.media_size or 0
    original_sender_id = existing.sender_id

    media_key = await crud_purge_message(session, chat_id=chat_id, message_id=message_id)
    if media_key is None:
        return False  # not currently deleted, or already purged

    if media_key:
        # Free the space this media occupied against the sender's quota
        # (ADR 0028). A soft delete does not refund; only this irreversible
        # purge does - that's the "delete files to make room" mechanism.
        if purged_size and original_sender_id is not None:
            try:
                await add_storage_usage(session, original_sender_id, -purged_size)
            except Exception as exc:
                logger.error("purge: storage quota refund failed for user %s: %s", original_sender_id, exc)

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
