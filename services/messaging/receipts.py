"""Delivery / read / played receipts: watermark advances, live events, detailed-log enqueue, info view."""

import json
import logging
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from database.crud.crud_chat import get_chat_by_id
from database.crud.crud_message import get_message_by_id
from database.crud.crud_participant import (
    get_chat_participants,
    is_participant,
    update_last_delivered_message,
    update_last_played_message,
    update_last_read_message,
)
from database.models.message import AUDIO_MESSAGE_TYPE
from config import (
    RECEIPT_KIND_DELIVERED,
    RECEIPT_KIND_PLAYED,
    RECEIPT_KIND_READ,
)
from database.crud import crud_receipt
from services import realtime_service
from services.messaging.errors import MessageNotFoundError, NotAParticipantError, NotAVoiceMessageError
from services.receipts import receipt_log
from services.redis_client import redis_client

logger = logging.getLogger(__name__)


async def _record_receipt(
    chat_id: int,
    user_id: int,
    kind: int,
    message_id: int,
    occurred_at: datetime,
) -> None:
    """
    Append the detailed history row (via the Redis Stream) for a watermark
    advance that actually happened. A Redis failure here is logged and
    swallowed - the coarse Participant.last_*_at timestamp was already
    written synchronously by the crud layer, and the live receipt event
    below still fires, so the fast-path tick is never affected.
    """
    try:
        await receipt_log.enqueue_receipt_event(chat_id, user_id, kind, message_id, occurred_at)
    except Exception:
        logger.exception("failed to enqueue receipt event (chat=%s user=%s kind=%s)", chat_id, user_id, kind)


async def mark_as_delivered(session: AsyncSession, user_id: int, chat_id: int, message_id: int) -> None:
    occurred_at = datetime.now(timezone.utc)
    participant = await update_last_delivered_message(
        session, chat_id=chat_id, user_id=user_id, message_id=message_id, occurred_at=occurred_at
    )
    if participant is None:
        return  # watermark already at/past this message - nothing changed
    await _record_receipt(chat_id, user_id, RECEIPT_KIND_DELIVERED, message_id, occurred_at)
    await realtime_service.publish_event(
        chat_id,
        {
            "event": "delivery_receipt",
            "chat_id": str(chat_id),
            "user_id": str(user_id),
            "message_id": str(message_id),
            "occurred_at": occurred_at.isoformat(),
        },
    )


async def mark_as_read(session: AsyncSession, user_id: int, chat_id: int, message_id: int) -> None:
    occurred_at = datetime.now(timezone.utc)
    participant = await update_last_read_message(
        session, chat_id=chat_id, user_id=user_id, message_id=message_id, occurred_at=occurred_at
    )
    if participant is None:
        return
    await _record_receipt(chat_id, user_id, RECEIPT_KIND_READ, message_id, occurred_at)
    await realtime_service.publish_event(
        chat_id,
        {
            "event": "read_receipt",
            "chat_id": str(chat_id),
            "user_id": str(user_id),
            "message_id": str(message_id),
            "occurred_at": occurred_at.isoformat(),
        },
    )


async def mark_as_played(session: AsyncSession, user_id: int, chat_id: int, message_id: int) -> None:
    """
    The recipient listened to a voice recording ("נשמעה"). Watermark-based
    like mark_as_delivered/mark_as_read - bumps this participant's
    last_played_message_id, which rolls up into Chat.all_played_up_to_message_id
    so the message's PLAYED status is derived, never written per-recording.
    Works identically for 1:1 and group chats. Rejects a non-voice message so
    the played watermark can only ever be moved by an actual listen.
    """
    if not await is_participant(session, chat_id, user_id):
        raise NotAParticipantError(f"User {user_id} is not a participant of chat {chat_id}")

    message = await get_message_by_id(session, chat_id=chat_id, message_id=message_id)
    if message is None or message.type != AUDIO_MESSAGE_TYPE:
        raise NotAVoiceMessageError(f"Message {message_id} in chat {chat_id} is not a voice recording")

    occurred_at = datetime.now(timezone.utc)
    participant = await update_last_played_message(
        session, chat_id=chat_id, user_id=user_id, message_id=message_id, occurred_at=occurred_at
    )
    if participant is None:
        return
    await _record_receipt(chat_id, user_id, RECEIPT_KIND_PLAYED, message_id, occurred_at)
    await realtime_service.publish_event(
        chat_id,
        {
            "event": "played_receipt",
            "chat_id": str(chat_id),
            "user_id": str(user_id),
            "message_id": str(message_id),
            "occurred_at": occurred_at.isoformat(),
        },
    )


# Short TTL: "who has read this" drifts by at most this many seconds for a
# viewer holding the details panel open. Live *_receipt events still stream
# to that client meanwhile for it to accumulate, so the panel isn't stale in
# practice - this only bounds the cold-open query rate for a hot group.
_RECEIPT_DETAIL_CACHE_TTL_SECONDS = 10
_RECEIPT_DETAIL_CACHE_PREFIX = "receipt_detail:"


def _receipt_detail_cache_key(chat_id: int, message_id: int) -> str:
    return f"{_RECEIPT_DETAIL_CACHE_PREFIX}{chat_id}:{message_id}"


async def get_message_receipts(
    session: AsyncSession,
    user_id: int,
    chat_id: int,
    message_id: int,
) -> dict:
    """
    The per-message "info" view: when each participant received / read /
    played this message and, in a group, who has. Any participant of the
    chat may view it for any message (Telegram-style), not just the sender.

    Reads message_receipt_log, never the hot-path watermark columns. For a
    group above config.RECEIPT_NAMED_LIST_MAX_MEMBERS participants only
    aggregate counts are returned (no per-member list). Result shape matches
    routers.schemas.MessageReceiptsOut.
    """
    if not await is_participant(session, chat_id, user_id):
        raise NotAParticipantError(f"User {user_id} is not a participant of chat {chat_id}")

    cache_key = _receipt_detail_cache_key(chat_id, message_id)
    cached = await redis_client.get(cache_key)
    if cached is not None:
        return json.loads(cached)

    message = await get_message_by_id(session, chat_id=chat_id, message_id=message_id)
    if message is None:
        raise MessageNotFoundError(f"Message {message_id} not found in chat {chat_id}")

    chat = await get_chat_by_id(session, chat_id)
    participants = await get_chat_participants(session, chat_id)
    # Everyone but the sender is eligible to "receive/read/play" the message.
    eligible = [p.user_id for p in participants if p.user_id != message.sender_id]
    is_audio = message.type == AUDIO_MESSAGE_TYPE
    # Read off the facade module at call time so a test that does
    # monkeypatch.setattr(message_service, "RECEIPT_NAMED_LIST_MAX_MEMBERS", ...)
    # still takes effect after the split into services/messaging/.
    from services import message_service

    truncated = len(eligible) > message_service.RECEIPT_NAMED_LIST_MAX_MEMBERS

    payload: dict = {
        "chat_id": str(chat_id),
        "message_id": str(message_id),
        "is_group": bool(chat.is_group) if chat is not None else len(participants) > 2,
        "message_type": message.type,
        "participant_count": len(eligible),
        "truncated": truncated,
    }

    if truncated:
        payload["counts"] = {
            "delivered": await crud_receipt.crosser_count_for_message(
                session, chat_id, RECEIPT_KIND_DELIVERED, message_id, eligible
            ),
            "read": await crud_receipt.crosser_count_for_message(
                session, chat_id, RECEIPT_KIND_READ, message_id, eligible
            ),
            "played": (
                await crud_receipt.crosser_count_for_message(
                    session, chat_id, RECEIPT_KIND_PLAYED, message_id, eligible
                )
                if is_audio
                else 0
            ),
        }
    else:
        eligible_set = set(eligible)

        def _entries(rows):
            return [
                {"user_id": str(uid), "occurred_at": at.isoformat()}
                for uid, at in rows
                if uid in eligible_set
            ]

        delivered_by = _entries(
            await crud_receipt.crossers_for_message(session, chat_id, RECEIPT_KIND_DELIVERED, message_id)
        )
        read_by = _entries(
            await crud_receipt.crossers_for_message(session, chat_id, RECEIPT_KIND_READ, message_id)
        )
        played_by = (
            _entries(await crud_receipt.crossers_for_message(session, chat_id, RECEIPT_KIND_PLAYED, message_id))
            if is_audio
            else []
        )
        read_user_ids = {int(e["user_id"]) for e in read_by}

        payload["delivered_by"] = delivered_by
        payload["read_by"] = read_by
        payload["played_by"] = played_by
        payload["pending"] = [str(uid) for uid in eligible if uid not in read_user_ids]
        payload["counts"] = {
            "delivered": len(delivered_by),
            "read": len(read_by),
            "played": len(played_by),
        }

    await redis_client.set(cache_key, json.dumps(payload), ex=_RECEIPT_DETAIL_CACHE_TTL_SECONDS)
    return payload
