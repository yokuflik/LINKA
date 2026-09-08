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
from config import (
    LIST_READ_RATE_MAX,
    LIST_READ_RATE_WINDOW_SECONDS,
    USERNAME_CHECK_RATE_MAX,
    USERNAME_CHECK_RATE_WINDOW_SECONDS,
)
from services import avatar_service, rate_limit_service, user_service
from services.settings import service as settings_service

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserOut)
async def get_my_profile(user_id: int = Depends(get_current_user_id), session: AsyncSession = Depends(get_db)):
    await rate_limit_service.enforce_sliding_window(
        user_id, "list_read", LIST_READ_RATE_MAX, LIST_READ_RATE_WINDOW_SECONDS
    )
    user = await user_service.get_profile(session, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user


@router.get("/username-available")
async def username_available(
    username: str,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """Advisory only (ADR 0017): the real authority is the unique-index write on
    PATCH /users/me. Dedicated tight bucket so it can't enumerate the table."""
    await rate_limit_service.enforce_sliding_window(
        user_id, "username_check", USERNAME_CHECK_RATE_MAX, USERNAME_CHECK_RATE_WINDOW_SECONDS
    )
    return await user_service.check_username_available(session, user_id, username)


@router.get("/by-phone", response_model=UserOut)
async def get_profile_by_phone(
    phone_number: str,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """Looks up a user by phone number - e.g. to start a private chat by phone
    instead of needing to already know their numeric id."""
    await rate_limit_service.enforce_sliding_window(
        user_id, "list_read", LIST_READ_RATE_MAX, LIST_READ_RATE_WINDOW_SECONDS
    )
    user = await user_service.get_profile_by_phone(session, phone_number)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No user with that phone number")
    return user


@router.get("/by-username", response_model=UserOut)
async def get_profile_by_username(
    username: str,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """Exact-match username lookup (ADR 0017) - e.g. to start a private chat by
    username. No prefix / substring search: a full handle or nothing."""
    await rate_limit_service.enforce_sliding_window(
        user_id, "list_read", LIST_READ_RATE_MAX, LIST_READ_RATE_WINDOW_SECONDS
    )
    user = await user_service.get_profile_by_username(session, username)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No user with that username")
    return user


@router.patch("/me", response_model=UserOut)
async def update_my_profile(
    body: UserProfileUpdateIn,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    if body.username is not None:
        # Reason-coded (ADR 0017): format -> 400, taken/cooldown/grace_hold -> 409.
        await user_service.set_username(session, user_id, body.username)

    user = await user_service.update_profile(session, user_id, about_text=body.about_text)
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
    user = await avatar_service.set_avatar(session, user_id, body.storage_key, body.preview)
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
