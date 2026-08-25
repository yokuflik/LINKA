from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete
from sqlalchemy.exc import IntegrityError
from typing import Optional
import logging

from database.models.chat import Chat

logger = logging.getLogger(__name__)

async def get_chat_by_id(session: AsyncSession, chat_id: int) -> Optional[Chat]:
    """
    Fetch a chat by its Primary Key (id).
    """
    stmt = select(Chat).where(Chat.id == chat_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def create_chat(
    session: AsyncSession, 
    chat_id: int, 
    is_group: bool, 
    title: Optional[str] = None,
    about_text: Optional[str] = None,
    profile_pic_url: Optional[str] = None
) -> Optional[Chat]:
    """
    Insert a new chat into the database.
    """
    new_chat = Chat(
        id=chat_id, # Snowflake ID generated at the application layer
        is_group=is_group,
        title=title if is_group else None, # Enforce logic: private chats don't have titles
        about_text=about_text if is_group else None,
        profile_pic_url=profile_pic_url
    )
    
    session.add(new_chat)
    try:
        await session.commit()
        await session.refresh(new_chat)
        return new_chat
    except IntegrityError as e:
        # Handles edge cases where a Snowflake ID collision occurs (statistically near zero)
        await session.rollback()
        logger.error(f"Failed to create chat, ID {chat_id} might already exist. Error: {e}")
        return None


async def update_chat_details(
    session: AsyncSession, 
    chat_id: int, 
    title: Optional[str] = None, 
    about_text: Optional[str] = None,
    profile_pic_url: Optional[str] = None
) -> Optional[Chat]:
    """
    Update chat profile fields (typically for groups).
    """
    update_data = {}
    if title is not None:
        update_data["title"] = title
    if about_text is not None:
        update_data["about_text"] = about_text
    if profile_pic_url is not None:
        update_data["profile_pic_url"] = profile_pic_url

    if not update_data:
        return await get_chat_by_id(session, chat_id)

    stmt = (
        update(Chat)
        .where(Chat.id == chat_id)
        .values(**update_data)
        .returning(Chat)
    )
    
    result = await session.execute(stmt)
    await session.commit()
    
    return result.scalar_one_or_none()


async def delete_chat(session: AsyncSession, chat_id: int) -> bool:
    """
    Delete a chat and all its cascades (like participants).
    """
    stmt = delete(Chat).where(Chat.id == chat_id)
    result = await session.execute(stmt)
    await session.commit()
    
    # rowcount tells us if a row was actually found and deleted
    return result.rowcount > 0