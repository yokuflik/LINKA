from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from database.connection import get_db
from routers.dependencies import get_current_user_id
from routers.schemas import (
    AvatarCommitIn,
    AvatarUploadTicketIn,
    AvatarUploadTicketOut,
    UserOut,
    UserProfileUpdateIn,
    UserSettingsOut,
    UserSettingsUpdateIn,
)
from services import avatar_service, user_service
from services.settings import service as settings_service

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
        session, user_id, display_name=body.display_name, about_text=body.about_text
    )
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    await user_service.broadcast_profile_update(session, user_id)
    return user


@router.get("/me/settings", response_model=UserSettingsOut)
async def get_my_settings(
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    return UserSettingsOut(settings=await settings_service.get_user_settings(session, user_id))


@router.patch("/me/settings", response_model=UserSettingsOut)
async def update_my_settings(
    body: UserSettingsUpdateIn,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    merged = await settings_service.update_user_settings(session, user_id, body.settings)
    return UserSettingsOut(settings=merged)


@router.post("/me/avatar/upload-ticket", response_model=AvatarUploadTicketOut)
async def create_avatar_upload_ticket(
    body: AvatarUploadTicketIn,
    user_id: int = Depends(get_current_user_id),
):
    """Step 1: get a presigned PUT the client uploads the image directly to."""
    ticket = avatar_service.request_upload(body.mime_type, body.size_bytes)
    return AvatarUploadTicketOut(
        storage_key=ticket.storage_key,
        upload_url=ticket.upload_url,
        required_headers=ticket.required_headers,
        expires_in=ticket.expires_in,
    )


@router.put("/me/avatar", response_model=UserOut)
async def set_my_avatar(
    body: AvatarCommitIn,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """Step 2: commit the uploaded object as this user's avatar."""
    user = await avatar_service.set_avatar(session, user_id, body.storage_key)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    await user_service.broadcast_profile_update(session, user_id)
    return user


@router.delete("/me/avatar", response_model=UserOut)
async def delete_my_avatar(
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    user = await avatar_service.clear_avatar(session, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    await user_service.broadcast_profile_update(session, user_id)
    return user
