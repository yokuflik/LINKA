"""Read paths: the home-screen chat list (with unread counts + masked status)
and the per-chat member list."""

from typing import Sequence
from sqlalchemy.ext.asyncio import AsyncSession

from modules.messaging.crud import compute_message_status
from modules.messaging.crud import count_unread_messages
from modules.chats.crud.crud_participant import get_chat_participants_with_users
from modules.chats.crud.crud_participant import get_user_chats
from modules.chats.crud.crud_participant import is_participant
from modules.chats.models.participant import Participant
from modules.chats.errors import PermissionDeniedError


async def get_chat_list(
    session: AsyncSession,
    user_id: int,
    before=None,
    limit: int = 30,
) -> Sequence[Participant]:
    """
    Home screen. Each returned Participant has its Chat eagerly loaded and an
    `unread_count` attached (see below), so callers don't need a separate
    query per chat to render the WhatsApp-style unread badge.
    """
    participants = await get_user_chats(session, user_id, before=before, limit=limit)

    for participant in participants:
        chat = participant.chat
        # Attached rather than a stored column - same reasoning as
        # MessageStatus. The chat is already loaded, and this is a plain
        # comparison against its own last_message_id/
        # all_delivered_up_to_message_id/all_read_up_to_message_id - no
        # extra query per chat.
        chat.last_message_status = (
            compute_message_status(chat.last_message_id, chat) if chat.last_message_id is not None else None
        )
        # Asymmetric read-receipt privacy (ADR 0003): in a 1:1 chat, mask
        # READ/PLAYED -> DELIVERED when the *other* participant keeps their
        # own read receipts off. Only 1:1 chats pay the settings lookup.
        if chat.last_message_status is not None and not chat.is_group:
            from modules.messaging.receipt_privacy import mask_status
            from modules.messaging.receipt_privacy import read_receipts_hidden_for_message

            if await read_receipts_hidden_for_message(session, chat.id, sender_id=user_id, chat=chat):
                chat.last_message_status = mask_status(chat.last_message_status)
        # Unlike last_message_status (chat-wide), this is genuinely
        # per-viewer - how many messages *this* participant hasn't read yet
        # - so it's attached to the Participant, not the Chat. One indexed
        # COUNT query per chat (see count_unread_messages) - cheap via the
        # (chat_id, id) index, but still a real query per chat in the list.
        participant.unread_count = await count_unread_messages(session, chat.id, participant.last_read_message_id)

    return participants


async def get_chat_members(session: AsyncSession, requester_id: int, chat_id: int) -> Sequence[Participant]:
    """
    Every participant of a chat, each with their User eagerly loaded - e.g.
    so a client can render a private chat's title as the other person's
    phone number instead of a raw chat id.
    """
    if not await is_participant(session, chat_id, requester_id):
        raise PermissionDeniedError(f"User {requester_id} is not a participant of chat {chat_id}")
    return await get_chat_participants_with_users(session, chat_id)
