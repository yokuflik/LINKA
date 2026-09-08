"""
Read-receipts (blue-tick) privacy - per-reader, asymmetric. See ADR 0003.

Model (mirrors presence / last-seen): a user sends READ/PLAYED receipts iff
*their own* `privacy.read_receipts` is true. The other side's setting is
irrelevant to what I send. So a receipt from reader R is visible iff
R.privacy.read_receipts is true.

Only applies to 1:1 chats. Group chats (>2 members) always record and show
read/played for everyone, regardless of individual settings.

Delivery receipts are never affected. The internal watermark advances
(Participant.last_read_message_id, the detailed log) still happen - this is
a presentation-layer mask only, so the reader's own unread_count clears.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from database.crud.crud_chat import get_chat_by_id
from database.crud.crud_participant import get_chat_participants
from database.models.chat import Chat
from database.models.message import MessageStatus
from services.settings import service as settings_service


async def _is_one_to_one(
    session: AsyncSession,
    chat_id: int,
    chat: Chat | None,
    participant_user_ids: list[int] | None,
) -> bool:
    if chat is None:
        chat = await get_chat_by_id(session, chat_id)
    if chat is not None:
        return not chat.is_group
    if participant_user_ids is None:
        participant_user_ids = [p.user_id for p in await get_chat_participants(session, chat_id)]
    return len(participant_user_ids) == 2


async def reader_hides_read_receipts(
    session: AsyncSession,
    chat_id: int,
    reader_id: int,
    chat: Chat | None = None,
    participant_user_ids: list[int] | None = None,
) -> bool:
    """
    True if READ/PLAYED from `reader_id` must be hidden from the sender -
    i.e. this is a 1:1 chat and `reader_id` turned their own read receipts
    off. Group chats are never hidden.
    """
    if not await _is_one_to_one(session, chat_id, chat, participant_user_ids):
        return False
    return not await settings_service.get_read_receipts_enabled(session, reader_id)


async def read_receipts_hidden_for_message(
    session: AsyncSession,
    chat_id: int,
    sender_id: int | None,
    chat: Chat | None = None,
    participant_user_ids: list[int] | None = None,
) -> bool:
    """
    For a message the sender is viewing in a 1:1 chat: True if the other
    participant (the reader) has read receipts off, so the derived
    READ/PLAYED status must be masked to DELIVERED.
    """
    if not await _is_one_to_one(session, chat_id, chat, participant_user_ids):
        return False
    if participant_user_ids is None:
        participant_user_ids = [p.user_id for p in await get_chat_participants(session, chat_id)]
    others = [uid for uid in participant_user_ids if uid != sender_id]
    if len(others) != 1:
        return False
    return not await settings_service.get_read_receipts_enabled(session, others[0])


def mask_status(status: MessageStatus) -> MessageStatus:
    """Downgrade READ/PLAYED to DELIVERED; leave SENT/DELIVERED untouched."""
    if status in (MessageStatus.READ, MessageStatus.PLAYED):
        return MessageStatus.DELIVERED
    return status
