from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete
from sqlalchemy.exc import IntegrityError
from typing import Sequence, Optional
import logging

from database.models.participant import Participant

logger = logging.getLogger(__name__)

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


async def get_user_chats(session: AsyncSession, user_id: int) -> Sequence[Participant]:
    """
    Fetch all chats a specific user is a member of (Crucial for the Home Screen).
    
    Time Complexity: O(log N + K) where K is the number of chats the user is in.
    Explanation: Leverages the reverse index on user_id to instantly find the rows 
    without scanning the entire multi-billion row table.
    """
    stmt = select(Participant).where(Participant.user_id == user_id)
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_chat_participants(session: AsyncSession, chat_id: int) -> Sequence[Participant]:
    """
    Fetch all participants in a specific chat (Used by Redis Pub/Sub for message fanout).
    
    Time Complexity: O(log N + K) where K is the number of participants in the chat.
    Explanation: Hits the primary key B-Tree which is naturally clustered by chat_id.
    """
    stmt = select(Participant).where(Participant.chat_id == chat_id)
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