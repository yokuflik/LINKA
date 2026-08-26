from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete, tuple_, func
from sqlalchemy.orm import contains_eager
from sqlalchemy.exc import IntegrityError
from typing import Sequence, Optional, Tuple
from datetime import datetime
import logging

from database.models.participant import Participant
from database.models.chat import Chat
from database.models.user import User

logger = logging.getLogger(__name__)

# Same reasoning as crud_message.MAX_PAGE_SIZE: `limit` must be a hard cap,
# not just a default a caller can override into "load everything".
MAX_PAGE_SIZE = 100

async def add_participant_to_chat(
    session: AsyncSession, 
    chat_id: int, 
    user_id: int, 
    role: int = 1
) -> Optional[Participant]:
    """
    Add a user to a chat.
    
    Time Complexity: O(log N)
    Explanation: Inserting into the composite Primary Key B-Tree (chat_id, user_id) 
    and the secondary index (user_id) takes logarithmic time.
    """
    # A brand-new participant's watermarks start at the chat's *current*
    # last_message_id, not NULL/0 - anything sent before they joined isn't
    # theirs to block on. Without this, adding one new member to an
    # otherwise fully-read 1000-person group would freeze
    # Chat.all_read_up_to_message_id/all_delivered_up_to_message_id back
    # down to "nothing", making years of already-read history report as
    # unread/undelivered again the moment they were added.
    chat_last_message_id = await session.scalar(select(Chat.last_message_id).where(Chat.id == chat_id))

    new_participant = Participant(
        chat_id=chat_id,
        user_id=user_id,
        role=role,
        last_delivered_message_id=chat_last_message_id,
        last_read_message_id=chat_last_message_id,
    )
    session.add(new_participant)
    try:
        await session.commit()
        await session.refresh(new_participant)
        return new_participant
    except IntegrityError as e:
        # Handles cases where the user is already in the chat (Composite PK violation)
        await session.rollback()
        logger.error(f"Failed to add user {user_id} to chat {chat_id}. Error: {e}")
        return None


async def get_user_chats(
    session: AsyncSession,
    user_id: int,
    before: Optional[Tuple[datetime, int]] = None,
    limit: int = 30,
) -> Sequence[Participant]:
    """
    Fetch one page of a user's chats (Home Screen), ordered by most recent
    activity first - never the whole list.

    Time Complexity: O(log N + limit)
    Explanation: The index on Participant.user_id finds this user's rows
    without scanning the multi-billion row participants table; each is then
    joined to its chat by primary key (O(1) per row). Sorting/paginating by
    Chat.last_message_at needs no index of its own here, because a single
    user's chat count is always small (participants.user_id already bounds
    it) - it's the per-chat message history (unbounded, up to millions of
    rows) that the real index work in crud_message.get_chat_messages is for.

    `before`: the (last_message_at, chat_id) of the last chat from the
    previous page, to fetch the next one.
    """
    limit = min(limit, MAX_PAGE_SIZE)

    stmt = (
        select(Participant)
        .join(Chat, Chat.id == Participant.chat_id)
        .where(Participant.user_id == user_id)
        .options(contains_eager(Participant.chat))
        .order_by(Chat.last_message_at.desc(), Chat.id.desc())
        .limit(limit)
    )

    if before is not None:
        cursor_last_message_at, cursor_chat_id = before
        stmt = stmt.where(tuple_(Chat.last_message_at, Chat.id) < tuple_(cursor_last_message_at, cursor_chat_id))

    result = await session.execute(stmt)
    return result.scalars().all()


async def get_all_chat_ids_for_user(session: AsyncSession, user_id: int) -> Sequence[int]:
    """
    Every chat_id this user is a participant of, unpaginated - deliberately
    separate from get_user_chats(), which is capped at MAX_PAGE_SIZE for the
    home screen. The WebSocket connection manager needs this uncapped
    version: subscribing to only a user's first 100 chats would silently
    stop delivering real-time events on their 101st chat and up.

    Time Complexity: O(log N + K) where K is this user's chat count -
    bounded per-user, never a function of table size.
    """
    stmt = select(Participant.chat_id).where(Participant.user_id == user_id)
    result = await session.execute(stmt)
    return result.scalars().all()


async def is_participant(session: AsyncSession, chat_id: int, user_id: int) -> bool:
    """
    Permission check used by chat_service/message_service before letting a
    user act on a chat (send a message, view history, manage members).

    Time Complexity: O(log N)
    Explanation: A direct hit on the (chat_id, user_id) composite Primary Key.
    """
    stmt = select(Participant.chat_id).where(Participant.chat_id == chat_id, Participant.user_id == user_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


async def update_participant_role(session: AsyncSession, chat_id: int, user_id: int, role: int) -> Optional[Participant]:
    """
    Promotes/demotes a participant (e.g. Member <-> Admin). Permission checks
    (only an Owner may do this) belong in chat_service, not here.
    """
    stmt = (
        update(Participant)
        .where(Participant.chat_id == chat_id, Participant.user_id == user_id)
        .values(role=role)
        .returning(Participant)
    )
    result = await session.execute(stmt)
    await session.commit()
    return result.scalar_one_or_none()


async def get_chat_participants(session: AsyncSession, chat_id: int) -> Sequence[Participant]:
    """
    Fetch all participants in a specific chat (Used by Redis Pub/Sub for message fanout).
    
    Time Complexity: O(log N + K) where K is the number of participants in the chat.
    Explanation: Hits the primary key B-Tree which is naturally clustered by chat_id.
    """
    stmt = select(Participant).where(Participant.chat_id == chat_id)
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_chat_participants_with_users(session: AsyncSession, chat_id: int) -> Sequence[Participant]:
    """
    Same as get_chat_participants, but with each row's User eagerly loaded -
    for UI purposes (e.g. showing a private chat's title as the other
    participant's phone number) where the caller needs more than just ids.

    Time Complexity: O(log N + K) where K is the number of participants -
    one join against users by primary key per row, still index-bound.
    """
    stmt = (
        select(Participant)
        .join(User, User.id == Participant.user_id)
        .where(Participant.chat_id == chat_id)
        .options(contains_eager(Participant.user))
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def recompute_chat_receipt_cursors(session: AsyncSession, chat_id: int) -> None:
    """
    Rolls Chat.all_delivered_up_to_message_id / all_read_up_to_message_id up
    to MIN(watermark) across this chat's current participants - "the
    highest message id that literally everyone has delivered/read". Shared
    by update_last_delivered_message/update_last_read_message/
    remove_participant below and by crud_message.create_message (sending
    implies having seen the chat up to that point, which can itself unstick
    a cursor that was stuck on the sender's own stale watermark).

    A participant who hasn't delivered/read anything yet (NULL) has to
    count as 0, not be skipped - plain SQL MIN() ignores NULLs, which would
    let the cursor advance past messages a real participant never actually
    got.

    Time Complexity: O(K) where K is this chat's participant count (at most
    ~1000 even for a large group), via the existing index on
    Participant.chat_id (part of its composite Primary Key) - completely
    independent of the chat's total message history, which is what makes
    this affordable to run on every single send/delivery-ack/read-ack.

    Does not commit - callers run this alongside their own write(s) and
    commit once, atomically.
    """
    stmt = select(
        func.min(func.coalesce(Participant.last_delivered_message_id, 0)),
        func.min(func.coalesce(Participant.last_read_message_id, 0)),
    ).where(Participant.chat_id == chat_id)
    min_delivered, min_read = (await session.execute(stmt)).one()

    await session.execute(
        update(Chat)
        .where(Chat.id == chat_id)
        .values(all_delivered_up_to_message_id=min_delivered, all_read_up_to_message_id=min_read)
    )


async def update_last_delivered_message(
    session: AsyncSession,
    chat_id: int,
    user_id: int,
    message_id: int,
) -> Optional[Participant]:
    """
    Same watermark pattern as update_last_read_message, one step earlier in
    the pipeline: a message reaching this participant's device, not
    necessarily opened/read yet.
    """
    stmt = (
        update(Participant)
        .where(Participant.chat_id == chat_id, Participant.user_id == user_id)
        .values(last_delivered_message_id=message_id)
        .returning(Participant)
    )

    result = await session.execute(stmt)
    participant = result.scalar_one_or_none()

    if participant is not None:
        await recompute_chat_receipt_cursors(session, chat_id)

    await session.commit()
    return participant


async def update_last_read_message(
    session: AsyncSession,
    chat_id: int,
    user_id: int,
    message_id: int
) -> Optional[Participant]:
    """
    Update the watermark (last read message) for a user in a chat.

    Time Complexity: O(log N) + O(1)
    Explanation: B-Tree lookup takes O(log N). The update is an O(1) heap operation.
    Using RETURNING prevents a secondary SELECT query.
    """
    stmt = (
        update(Participant)
        .where(Participant.chat_id == chat_id, Participant.user_id == user_id)
        .values(last_read_message_id=message_id)
        .returning(Participant)
    )

    result = await session.execute(stmt)
    participant = result.scalar_one_or_none()

    if participant is not None:
        await recompute_chat_receipt_cursors(session, chat_id)

    await session.commit()

    return participant


async def remove_participant(session: AsyncSession, chat_id: int, user_id: int) -> bool:
    """
    Remove a user from a chat.

    Time Complexity: O(log N) + O(K) for the receipt-cursor recompute below
    (K = this chat's remaining participant count) if the removed user was
    actually a member - a departure can only ever raise
    Chat.all_delivered_up_to_message_id/all_read_up_to_message_id (one
    fewer participant left who might have been the bottleneck), so it's
    worth recomputing immediately rather than waiting for someone else's
    next delivery/read ack to do it.
    """
    stmt = delete(Participant).where(Participant.chat_id == chat_id, Participant.user_id == user_id)
    result = await session.execute(stmt)
    removed = result.rowcount > 0

    if removed:
        await recompute_chat_receipt_cursors(session, chat_id)

    await session.commit()

    return removed