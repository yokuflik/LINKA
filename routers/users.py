from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from database.connection import get_db
from routers.dependencies import get_current_user_id
from routers.schemas import UserOut, UserProfileUpdateIn
from services import user_service

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserOut)
async def get_my_profile(user_id: int = Depends(get_current_user_id), session: AsyncSession = Depends(get_db)):
    user = await user_service.get_profile(session, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user


@router.get("/by-phone", response_model=UserOut)
async def get_profile_by_phone(
    phone_number: str,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """Looks up a user by phone number - e.g. to start a private chat by phone
    instead of needing to already know their numeric id."""
    user = await user_service.get_profile_by_phone(session, phone_number)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No user with that phone number")
    return user


@router.patch("/me", response_model=UserOut)
async def update_my_profile(
    body: UserProfileUpdateIn,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    user = await user_service.update_profile(
        session, user_id, display_name=body.display_name, about_text=body.about_text, profile_pic_url=body.profile_pic_url
    )
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user
