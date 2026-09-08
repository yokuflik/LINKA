"""Per-user chat-list preferences: pin and mute. No role checks; multi-device
sync via personal-channel echoes. `realtime_service` goes through the
`chat_service` facade at call time (test monkeypatch compatibility)."""

from datetime import datetime
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession

from modules.chats.crud.crud_participant import set_chat_muted as _crud_set_chat_muted
from modules.chats.crud.crud_participant import set_chat_pinned as _crud_set_chat_pinned


async def set_chat_pinned(session: AsyncSession, user_id: int, chat_id: int, pinned: bool) -> bool:
    """
    Pin/unpin a chat for the calling user. Any participant may pin any of
    their own chats (no role check); pinning is purely a per-user chat-list
    ordering preference with no cap. Returns False if the user isn't a
    participant of the chat.
    """
    from modules.chats import service as chat_service

    participant = await _crud_set_chat_pinned(session, chat_id=chat_id, user_id=user_id, pinned=pinned)
    if participant is None:
        return False

    # Pinning is per-user, so the only clients that care are this same
    # user's *other* open connections (a second browser tab / device).
    # Push it over their personal channel - connection_manager forwards any
    # user_events payload to every connection of that user - so each one
    # re-sorts its chat list live instead of only on the next GET /chats.
    # The acting connection gets the echo too; re-applying the same flag is
    # idempotent.
    await chat_service.realtime_service.publish_user_event(
        user_id,
        {"event": "chat_pin_changed", "chat_id": str(chat_id), "pinned": pinned},
    )
    return True


async def set_chat_muted(
    session: AsyncSession, user_id: int, chat_id: int, muted_until: Optional[datetime]
) -> bool:
    """
    Mute/unmute a chat for the calling user. `muted_until` is an absolute
    expiry chosen by the client ("forever" = a far-future timestamp); None
    unmutes. No role check - a personal chat-list preference. Returns False
    if the user isn't a participant.

    Server-side, muting only suppresses offline push (see ADR 0004); the
    client does the rest. The mute state is pushed to the user's *other*
    connections so every device updates live (same pattern as pinning).
    """
    from modules.chats import service as chat_service

    participant = await _crud_set_chat_muted(
        session, chat_id=chat_id, user_id=user_id, muted_until=muted_until
    )
    if participant is None:
        return False

    await chat_service.realtime_service.publish_user_event(
        user_id,
        {
            "event": "chat_mute_changed",
            "chat_id": str(chat_id),
            "muted_until": muted_until.isoformat() if muted_until is not None else None,
        },
    )
    return True
