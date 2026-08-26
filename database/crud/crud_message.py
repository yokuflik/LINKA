from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from sqlalchemy.sql import func
from sqlalchemy.exc import IntegrityError
from typing import Sequence, Optional
import logging

from database.models.message import Message, MessageStatus
from database.models.chat import Chat, LAST_MESSAGE_PREVIEW_LENGTH
from database.models.participant import Participant
from database.crud.crud_participant import recompute_chat_receipt_cursors

logger = logging.getLogger(__name__)

# Hard ceiling on any page size a caller can request, regardless of what they
# pass in. Without this, `limit` is just a suggestion - someone (a bug, or a
# malicious client) passing limit=1_000_000 would defeat pagination entirely.
MAX_PAGE_SIZE = 100


def build_last_message_preview(content: Optional[str]) -> Optional[str]:
    """The Chat.last_message_preview value a given message's content maps to."""
    return None if content is None else content[:LAST_MESSAGE_PREVIEW_LENGTH]


def compute_message_status(message_id: int, chat: Chat) -> MessageStatus:
    """
    Derived, not stored - see MessageStatus. O(1) regardless of chat size,
    group size, or how much history this chat has: just two integer
    comparisons against the chat's own receipt-watermark columns.
    """
    if chat.all_read_up_to_message_id is not None and message_id <= chat.all_read_up_to_message_id:
        return MessageStatus.READ
    if chat.all_delivered_up_to_message_id is not None and message_id <= chat.all_delivered_up_to_message_id:
        return MessageStatus.DELIVERED
    return MessageStatus.SENT

async def create_message(
    session: AsyncSession,
    message_id: int,
    chat_id: int,
    sender_id: Optional[int] = None,
    type: int = 1,
    content: Optional[str] = None,
    reply_to_message_id: Optional[int] = None,
) -> Optional[Message]:
    """
    Insert a new message into the chat and bump the chat's recency
    (last_message_at/last_message_id/last_message_preview) and receipt
    watermarks, atomically.

    Time Complexity: O(log N + K), K = this chat's participant count.
    Explanation: created_at is left to the server default, so the row lands
    in the current (latest) partition and the (chat_id, id) index on that
    partition alone absorbs the write - independent of total table size.
    The recency bump is a single-row update by primary key on the small
    `chats` table, so it doesn't change that complexity; the receipt-cursor
    recompute (see recompute_chat_receipt_cursors) is the only O(K) part,
    and K is bounded by group size (~1000 at most), never by message count.
    """
    new_message = Message(
        id=message_id,  # Snowflake ID generated at the application layer
        chat_id=chat_id,
        sender_id=sender_id,
        type=type,
        content=content,
        reply_to_message_id=reply_to_message_id,
    )

    session.add(new_message)
    try:
        # Flushed (not yet committed) so a bad FK/id collision on the message
        # itself is caught before the chat is touched, without a partial commit.
        await session.flush()

        chat_update_values = {
            "last_message_at": new_message.created_at,
            "last_message_id": new_message.id,
        }
        # System messages (sender_id=None - "X joined the group", or a
        # private role-change notice) must never become the chat list's
        # preview line: unlike the message stream, that list has no
        # per-viewer filtering, so anyone glancing at their chat list would
        # see it, including text meant only for the two people involved in a
        # role change. Leaving last_message_preview untouched here keeps it
        # on the last real user message.
        if sender_id is not None:
            chat_update_values["last_message_preview"] = build_last_message_preview(new_message.content)

        await session.execute(update(Chat).where(Chat.id == chat_id).values(**chat_update_values))

        if sender_id is not None:
            # Sending implies having seen the chat up to this point - without
            # this, the sender's own (now stale) watermark could keep
            # falsely blocking Chat.all_delivered_up_to_message_id/
            # all_read_up_to_message_id for messages they'd actually already
            # seen from others, forever, until they explicitly re-opened the
            # chat. System messages (sender_id=None) have no one to bump.
            await session.execute(
                update(Participant)
                .where(Participant.chat_id == chat_id, Participant.user_id == sender_id)
                .values(last_delivered_message_id=new_message.id, last_read_message_id=new_message.id)
            )
            await recompute_chat_receipt_cursors(session, chat_id)

        await session.commit()
        await session.refresh(new_message)
        return new_message
    except IntegrityError as e:
        # Handles a bad chat_id/sender_id FK, or the (astronomically rare) id collision
        await session.rollback()
        logger.error(f"Failed to create message {message_id} in chat {chat_id}. Error: {e}")
        return None


async def get_message_by_id(session: AsyncSession, chat_id: int, message_id: int) -> Optional[Message]:
    """
    Fetch a single message by (chat_id, id).

    Time Complexity: O(log N)
    Explanation: chat_id is always known by the caller (a message is only ever
    read in the context of its chat), so this hits the (chat_id, id) index on
    each partition instead of an unpruned scan by id alone.
    """
    stmt = select(Message).where(Message.chat_id == chat_id, Message.id == message_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_chat_messages(
    session: AsyncSession,
    chat_id: int,
    before_id: Optional[int] = None,
    limit: int = 50,
    include_deleted: bool = False,
) -> Sequence[Message]:
    """
    Fetch a page of messages for a chat, newest first (cursor-based pagination).

    Time Complexity: O(log N + limit)
    Explanation: The (chat_id, id) index lets Postgres seek straight to the
    cursor and walk backwards for `limit` rows, instead of scanning the chat's
    full history - independent of whether the chat has 50 or 50 million messages.
    """
    limit = min(limit, MAX_PAGE_SIZE)

    stmt = select(Message).where(Message.chat_id == chat_id)

    if before_id is not None:
        stmt = stmt.where(Message.id < before_id)

    if not include_deleted:
        stmt = stmt.where(Message.deleted_at.is_(None))

    stmt = stmt.order_by(Message.id.desc()).limit(limit)

    result = await session.execute(stmt)
    return result.scalars().all()


async def edit_message_content(
    session: AsyncSession,
    chat_id: int,
    message_id: int,
    new_content: str,
) -> Optional[Message]:
    """
    Update a message's content and mark it as edited.

    Time Complexity: O(log N) + O(1)
    Explanation: B-Tree lookup via (chat_id, id) takes O(log N); the update
    itself is an O(1) heap operation. RETURNING avoids a secondary SELECT.
    """
    stmt = (
        update(Message)
        .where(Message.chat_id == chat_id, Message.id == message_id)
        .values(content=new_content, is_edited=True, edited_at=func.now())
        .returning(Message)
    )

    result = await session.execute(stmt)
    message = result.scalar_one_or_none()

    # The chat list would otherwise keep showing the pre-edit text until the
    # next message arrives. The last_message_id predicate is what makes this
    # a no-op when an *older* message is edited.
    if message is not None:
        await session.execute(
            update(Chat)
            .where(Chat.id == chat_id, Chat.last_message_id == message_id)
            .values(last_message_preview=build_last_message_preview(new_content))
        )

    await session.commit()

    return message


async def soft_delete_message(session: AsyncSession, chat_id: int, message_id: int) -> bool:
    """
    Soft-delete a message (sets deleted_at instead of removing the row).

    Time Complexity: O(log N)
    Explanation: Avoids a physical DELETE, which would be an expensive
    operation to run at this table's scale; a NULL check elsewhere hides it.
    """
    stmt = (
        update(Message)
        .where(Message.chat_id == chat_id, Message.id == message_id, Message.deleted_at.is_(None))
        .values(deleted_at=func.now())
    )

    result = await session.execute(stmt)
    deleted = result.rowcount > 0

    # Same reasoning as the edit path: leaving the snippet behind would keep
    # deleted text visible in every participant's chat list. It's cleared
    # rather than backfilled from the previous message - finding that one
    # means a scan back through the chat's history, and the recency ordering
    # (last_message_at) is unaffected either way.
    if deleted:
        await session.execute(
            update(Chat)
            .where(Chat.id == chat_id, Chat.last_message_id == message_id)
            .values(last_message_preview=None)
        )

    await session.commit()

    return deleted
