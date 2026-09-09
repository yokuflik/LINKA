from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from infra.db.connection import get_db
from api.dependencies import get_current_user_id
from api.schemas import AvatarCommitIn
from api.schemas import AvatarUploadTicketIn
from api.schemas import AvatarUploadTicketOut
from api.schemas import PublicKeyIn
from api.schemas import PublicKeyOut
from api.schemas import UserOut
from api.schemas import UserProfileUpdateIn
from api.schemas import UserSettingsOut
from api.schemas import UserSettingsUpdateIn
from config import (
    LIST_READ_RATE_MAX,
    LIST_READ_RATE_WINDOW_SECONDS,
    USERNAME_CHECK_RATE_MAX,
    USERNAME_CHECK_RATE_WINDOW_SECONDS,
)
from modules.users import avatar_service
from infra.ratelimit import service as rate_limit_service
from modules.users import service as user_service
from modules.settings import service as settings_service

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

    # ADR 0024: only touch display_name when the client actually sent the key
    # (present => set/clear; absent => leave unchanged).
    write_display_name = "display_name" in body.model_fields_set
    user = await user_service.update_profile(
        session,
        user_id,
        about_text=body.about_text,
        display_name=body.display_name,
        write_display_name=write_display_name,
    )
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    await user_service.broadcast_profile_update(session, user_id)
    return user


@router.put("/me/public-key", response_model=PublicKeyOut)
async def set_my_public_key(
    body: PublicKeyIn,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """Publish this device's E2E public key (ADR 0026). Shape-checked only; a
    private key (`d` present) is rejected. The server never holds the private
    key or any plaintext."""
    try:
        row = await user_service.set_public_key(session, user_id, body.public_key, body.algo)
    except user_service.PublicKeyError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_public_key", "reason": e.reason},
        )
    return row


@router.get("/{target_user_id}/public-key", response_model=PublicKeyOut)
async def get_user_public_key(
    target_user_id: int,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """Fetch one user's current E2E public key (ADR 0026) - e.g. before starting
    a new encrypted 1:1 chat."""
    await rate_limit_service.enforce_sliding_window(
        user_id, "list_read", LIST_READ_RATE_MAX, LIST_READ_RATE_WINDOW_SECONDS
    )
    row = await user_service.get_public_key(session, target_user_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No public key for that user")
    return row


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
