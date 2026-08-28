from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession

from database.crud.crud_participant import get_all_chat_ids_for_user
from database.crud.crud_user import get_user_by_id, get_user_by_phone, update_user_profile
from database.models.user import User
from services import realtime_service
from services.storage.media_service import public_avatar_url


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


async def broadcast_profile_update(session: AsyncSession, user_id: int) -> None:
    """
    Tell everyone who shares a chat with this user that their profile
    (display name / about / photo) just changed, so open clients can update
    the cached name+avatar they show in chat lists, headers and message
    bubbles without waiting to re-open that chat.

    A profile edit touches every chat the user is in (all private chats +
    all shared groups), so this is a *transient* fan-out event over the
    normal chat routing (same path as `typing`) - never a persisted system
    message: that would mean one INSERT into the partitioned `messages`
    table per shared chat, per edit, plus permanent history noise.

    Best-effort: a Redis hiccup here just means a client refreshes on its
    own next chat-open (see useChats.resolve* / the visibilitychange hook).
    """
    user = await get_user_by_id(session, user_id)
    if user is None:
        return
    key = user.profile_pic_url
    resolved_pic = (
        key if (key and key.startswith(("http://", "https://"))) else (public_avatar_url(key) if key else None)
    )
    event = {
        "event": "profile_updated",
        "user_id": str(user.id),
        "display_name": user.display_name,
        "about_text": user.about_text,
        "profile_pic_url": resolved_pic,
    }
    chat_ids = await get_all_chat_ids_for_user(session, user_id)
    for chat_id in chat_ids:
        await realtime_service.publish_event(chat_id, event)
