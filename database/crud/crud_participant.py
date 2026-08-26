from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete, tuple_
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
    new_participant = Participant(
        chat_id=chat_id,
        user_id=user_id,
        role=role
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
    await session.commit()
    
    return result.scalar_one_or_none()


async def remove_participant(session: AsyncSession, chat_id: int, user_id: int) -> bool:
    """
    Remove a user from a chat.
    
    Time Complexity: O(log N)
    """
    stmt = delete(Participant).where(Participant.chat_id == chat_id, Participant.user_id == user_id)
    result = await session.execute(stmt)
    await session.commit()
    
    # Returns True if a row was actually deleted, False otherwise
    return result.rowcount > 0