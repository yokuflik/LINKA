"""Read path: message history with derived status + presigned media URL attached."""

from typing import Optional, Sequence
from sqlalchemy.ext.asyncio import AsyncSession

from database.crud.crud_chat import get_chat_by_id
from database.crud.crud_message import compute_message_status, get_chat_messages
from database.crud.crud_participant import is_participant
from database.models.message import Message
from services.messaging.errors import NotAParticipantError
from services.messaging.receipt_privacy import mask_status, read_receipts_hidden_for_message
from services.storage import media_service


async def get_message_history(
    session: AsyncSession,
    user_id: int,
    chat_id: int,
    before_id: Optional[int] = None,
    limit: int = 50,
) -> Sequence[Message]:
    if not await is_participant(session, chat_id, user_id):
        raise NotAParticipantError(f"User {user_id} is not a participant of chat {chat_id}")

    chat = await get_chat_by_id(session, chat_id)
    # Asymmetric: the sender sees READ/PLAYED for a 1:1 message only if the
    # other participant (the reader) keeps their own read receipts on. The
    # viewer here is `user_id`, so the reader is the other participant.
    hide_read = await read_receipts_hidden_for_message(session, chat_id, sender_id=user_id, chat=chat)
    # Include soft-deleted rows: the client renders them as a "message deleted"
    # tombstone in place, so they must survive a chat reload / history paging.
    messages = await get_chat_messages(
        session, chat_id=chat_id, before_id=before_id, limit=limit, include_deleted=True
    )

    # Attached rather than a stored column - see MessageStatus. One extra
    # row fetch (the chat) for the whole page, then an O(1) comparison per
    # message already in hand; no per-message query.
    for message in messages:
        message.status = compute_message_status(message.id, chat, message.type)
        if hide_read and message.sender_id == user_id:
            message.status = mask_status(message.status)
        # Presigned GET for the attachment, same as the live fan-out event.
        # Callers serving large pages should cache per key for its TTL.
        message.media_url = media_service.message_media_download_url(message.media_key)

    return messages
