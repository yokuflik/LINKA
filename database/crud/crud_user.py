from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from typing import Optional
import logging

from database.models.user import User

logger = logging.getLogger(__name__)

async def get_user_by_id(session: AsyncSession, user_id: int) -> Optional[User]:
    """
    Fetch a user by their Primary Key (id).
    """
    stmt = select(User).where(User.id == user_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_user_by_phone(session: AsyncSession, phone_number: str) -> Optional[User]:
    """
    Fetch a user by their phone number.
    """
    stmt = select(User).where(User.phone_number == phone_number)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def create_user(
    session: AsyncSession, 
    user_id: int, 
    phone_number: str, 
    display_name: Optional[str] = None
) -> Optional[User]:
    """
    Insert a new user into the database.
    """
    new_user = User(
        id=user_id, # Assumes Snowflake ID is generated at the application layer
        phone_number=phone_number,
        display_name=display_name
    )
    
    session.add(new_user)
    try:
        await session.commit()
        await session.refresh(new_user)
        return new_user
    except IntegrityError as e:
        # Handles edge cases where a concurrent request tries to register the same phone number
        await session.rollback()
        logger.error(f"Failed to create user, phone number {phone_number} might already exist. Error: {e}")
        return None


async def update_user_profile(
    session: AsyncSession, 
    user_id: int, 
    display_name: Optional[str] = None, 
    about_text: Optional[str] = None,
    profile_pic_url: Optional[str] = None
) -> Optional[User]:
    """
    Update user profile fields.
    """
    update_data = {}
    if display_name is not None:
        update_data["display_name"] = display_name
    if about_text is not None:
        update_data["about_text"] = about_text
    if profile_pic_url is not None:
        update_data["profile_pic_url"] = profile_pic_url

    if not update_data:
        return await get_user_by_id(session, user_id)

    stmt = (
        update(User)
        .where(User.id == user_id)
        .values(**update_data)
        .returning(User) # PostgreSQL specific: returns the updated row in the same query
    )
    
    result = await session.execute(stmt)
    await session.commit()
    
    return result.scalar_one_or_none()
