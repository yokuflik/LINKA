"""Live nudges that accompany chat mutations: personal-channel add/remove
events and the transient chat_updated broadcast for group-detail changes.

`realtime_service` is referenced through the `chat_service` facade at call time
(not imported directly) because tests monkeypatch `chat_service.realtime_service`
- see test_chat_service.py's _broadcast_chat_update capture test.
"""

from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession

from database.crud.crud_chat import get_chat_by_id
from services.storage.media_service import public_avatar_url


async def _notify_added_to_chat(user_id: int, chat_id: int) -> None:
    """
    Tells connection_manager (via the user's personal channel) to bring this
    user's already-open connections into the new chat's live subscription
    immediately, instead of only at their next reconnect - see
    ConnectionManager._handle_user_channel_event. Also reaches the client
    itself, to refresh its chat list / show a notification.
    """
    from services import chat_service

    await chat_service.realtime_service.publish_user_event(
        user_id, {"event": "added_to_chat", "chat_id": str(chat_id)}
    )


async def _notify_removed_from_chat(user_id: int, chat_id: int, actor_id: int, chat_title: Optional[str]) -> None:
    """
    Mirror of _notify_added_to_chat, for the opposite direction: tells
    connection_manager to drop this user's already-open connections from the
    chat's live subscription immediately (they're no longer a participant,
    so they shouldn't keep receiving its events), and reaches the client
    itself so it can drop the chat from its own list right away instead of
    only at the next reconnect/GET /chats. Fired both when someone else
    removes this user and when they leave on their own initiative, so every
    one of their connected devices stays in sync either way - actor_id is
    included so the client can tell the two cases apart (e.g. skip a "you
    were removed" toast on the device that did the leaving itself), and
    chat_title so that toast can name the group instead of just saying "a
    group" (the client's own list entry for it is about to disappear too).
    """
    from services import chat_service

    await chat_service.realtime_service.publish_user_event(
        user_id,
        {
            "event": "removed_from_chat",
            "chat_id": str(chat_id),
            "actor_id": str(actor_id),
            "chat_title": chat_title,
        },
    )


async def _broadcast_chat_update(session: AsyncSession, chat_id: int) -> None:
    """
    Tell every member of a group that its title / description / photo just
    changed, so open clients update the name+avatar they show in the sidebar
    and chat header without re-opening the chat or waiting for GET /chats.

    A **transient** chat-scoped event over the normal routing (same path as
    `typing`) - the accompanying system message ("X changed the group name")
    is the persisted record; this is only the live nudge that carries the new
    values so clients don't each have to re-fetch. Best-effort.
    """
    from services import chat_service

    chat = await get_chat_by_id(session, chat_id)
    if chat is None:
        return
    key = chat.profile_pic_url
    resolved_pic = (
        key if (key and key.startswith(("http://", "https://"))) else (public_avatar_url(key) if key else None)
    )
    await chat_service.realtime_service.publish_event(
        chat_id,
        {
            "event": "chat_updated",
            "title": chat.title,
            "about_text": chat.about_text,
            "profile_pic_url": resolved_pic,
            "profile_pic_preview": chat.profile_pic_preview,
        },
    )
