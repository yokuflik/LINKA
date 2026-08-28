"""Editing and deleting an existing message."""

from sqlalchemy.ext.asyncio import AsyncSession

from database.crud.crud_message import (
    edit_message_content,
    get_message_by_id,
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
            },
        )
    return message
