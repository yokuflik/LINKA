"""Group profile edits (title / description / photo). All require ROLE_ADMIN,
all emit a persisted system message + a transient chat_updated broadcast."""

from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession

from modules.chats.crud.crud_chat import get_chat_by_id
from modules.chats.crud.crud_chat import update_chat_details
from modules.chats.models.chat import Chat
from modules.users import avatar_service
from modules.messaging import service as message_service
from modules.chats.common import ROLE_ADMIN
from modules.chats.common import _display_name_for
from modules.chats.common import _require_role
from modules.chats.notifications import _broadcast_chat_update


async def update_group_details(
    session: AsyncSession,
    actor_id: int,
    chat_id: int,
    title: Optional[str] = None,
    about_text: Optional[str] = None,
    profile_pic_url: Optional[str] = None,
) -> Chat:
    await _require_role(session, chat_id, actor_id, min_role=ROLE_ADMIN)

    existing = await get_chat_by_id(session, chat_id)
    if existing is None:
        return None

    # Snapshot the old values now: update_chat_details issues an UPDATE ...
    # RETURNING Chat that refreshes this same identity-mapped instance in
    # place, so reading existing.title afterwards would already show the new
    # value and the "did it actually change?" guards below would never fire.
    old_title = existing.title
    old_about_text = existing.about_text

    chat = await update_chat_details(
        session, chat_id=chat_id, title=title, about_text=about_text, profile_pic_url=profile_pic_url
    )
    if chat is None:
        return None

    # Mirror the other group mutations: a detail change is announced in-chat.
    actor_name = await _display_name_for(session, actor_id)
    if title is not None and title != old_title:
        await message_service.send_system_message(
            session, chat_id=chat_id, content=f'{actor_name} changed the group name to "{title}"'
        )
    if about_text is not None and about_text != old_about_text:
        await message_service.send_system_message(
            session, chat_id=chat_id, content=f"{actor_name} changed the group description"
        )
    await _broadcast_chat_update(session, chat_id)
    return chat


async def ensure_can_manage_details(session: AsyncSession, actor_id: int, chat_id: int) -> None:
    """
    Public guard for endpoints that change group details (e.g. minting a
    group-avatar upload ticket) but don't go through update_group_details /
    set_group_avatar themselves. Raises PermissionDeniedError (-> 403).
    """
    await _require_role(session, chat_id, actor_id, min_role=ROLE_ADMIN)


async def set_group_avatar(
    session: AsyncSession, actor_id: int, chat_id: int, storage_key: str,
    preview: Optional[str] = None,
) -> Optional[Chat]:
    """
    Set a group's profile picture. Requires ROLE_ADMIN (same as any other
    group-detail change). The object-storage validation + old-object cleanup
    lives in avatar_service; this layer only owns the authorization and the
    "X changed the group photo" system message.
    """
    await _require_role(session, chat_id, actor_id, min_role=ROLE_ADMIN)
    chat = await avatar_service.set_group_avatar(session, chat_id, storage_key, preview)
    if chat is None:
        return None
    actor_name = await _display_name_for(session, actor_id)
    await message_service.send_system_message(
        session, chat_id=chat_id, content=f"{actor_name} changed the group photo"
    )
    await _broadcast_chat_update(session, chat_id)
    return chat


async def clear_group_avatar(session: AsyncSession, actor_id: int, chat_id: int) -> Optional[Chat]:
    """Remove a group's profile picture. Requires ROLE_ADMIN."""
    await _require_role(session, chat_id, actor_id, min_role=ROLE_ADMIN)
    chat = await avatar_service.clear_group_avatar(session, chat_id)
    if chat is None:
        return None
    actor_name = await _display_name_for(session, actor_id)
    await message_service.send_system_message(
        session, chat_id=chat_id, content=f"{actor_name} removed the group photo"
    )
    await _broadcast_chat_update(session, chat_id)
    return chat
