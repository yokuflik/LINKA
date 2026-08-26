from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession

from database.crud.crud_user import get_user_by_id, get_user_by_phone, update_user_profile
from database.models.user import User


async def get_profile(session: AsyncSession, user_id: int) -> Optional[User]:
    return await get_user_by_id(session, user_id)


async def get_profile_by_phone(session: AsyncSession, phone_number: str) -> Optional[User]:
    return await get_user_by_phone(session, phone_number)


async def update_profile(
    session: AsyncSession,
    user_id: int,
    display_name: Optional[str] = None,
    about_text: Optional[str] = None,
    profile_pic_url: Optional[str] = None,
) -> Optional[User]:
    return await update_user_profile(
        session,
        user_id=user_id,
        display_name=display_name,
        about_text=about_text,
        profile_pic_url=profile_pic_url,
    )
