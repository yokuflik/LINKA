"""Delivery / read / played receipts: enqueue on the WS path (ADR 0037), plus the per-message info view."""

import json
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from modules.chats.crud.crud_chat import get_chat_by_id
from modules.messaging.crud import get_message_by_id
from modules.chats.crud.crud_participant import get_chat_participants
from modules.chats.crud.crud_participant import is_participant
from modules.messaging.models import AUDIO_MESSAGE_TYPE
from modules.messaging.limits import DEFAULT_MESSAGING_LIMITS, MessagingLimits
from config import settings
from modules.receipts import crud as crud_receipt
from modules.messaging.errors import MessageNotFoundError
from modules.messaging.errors import NotAParticipantError
from modules.messaging.receipt_privacy import read_receipts_hidden_for_message
from modules.receipts import receipt_log
from infra.redis.client import redis_client

logger = logging.getLogger(__name__)


async def _enqueue(user_id: int, chat_id: int, kind: int, message_id: int) -> None:
    """
    Fire-and-forget: XADD the receipt onto `receipt_log_stream` and return
    (ADR 0037). The `receipt_log` worker advances the coarse watermark,
    writes the detailed-log row, applies the ADR 0003 privacy gate and
    publishes the live receipt event - all off the WS receive path. A Redis
    hiccup here is logged and swallowed (a receipt is not a critical path;
    the client re-sends on the next scroll / reconnect).
    """
    try:
        await receipt_log.enqueue_receipt_event(chat_id, user_id, kind, message_id)
    except Exception:
        logger.exception("failed to enqueue receipt event (chat=%s user=%s kind=%s)", chat_id, user_id, kind)


async def mark_as_delivered(session: AsyncSession | None, user_id: int, chat_id: int, message_id: int) -> None:
    """Enqueue a delivery receipt (ADR 0037). `session` is unused, kept for call-site compatibility."""
    await _enqueue(user_id, chat_id, settings.RECEIPT_KIND_DELIVERED, message_id)


async def mark_as_read(session: AsyncSession | None, user_id: int, chat_id: int, message_id: int) -> None:
    """Enqueue a read receipt (ADR 0037). `session` is unused."""
    await _enqueue(user_id, chat_id, settings.RECEIPT_KIND_READ, message_id)


async def mark_as_played(session: AsyncSession | None, user_id: int, chat_id: int, message_id: int) -> None:
    """
    Enqueue a played receipt (ADR 0037). `session` is unused. The worker
    drops the entry if the target message is not a voice recording (the
    check that used to raise NotAVoiceMessageError synchronously) so the
    played watermark can still only be moved by an actual listen.
    """
    await _enqueue(user_id, chat_id, settings.RECEIPT_KIND_PLAYED, message_id)


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
    *,
    limits: MessagingLimits = DEFAULT_MESSAGING_LIMITS,
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
    # 1:1 only: the sole reader (the other participant) has read receipts off.
    hide_read = await read_receipts_hidden_for_message(
        session,
        chat_id,
        sender_id=message.sender_id,
        chat=chat,
        participant_user_ids=[p.user_id for p in participants],
    )
    # Everyone but the sender is eligible to "receive/read/play" the message.
    eligible = [p.user_id for p in participants if p.user_id != message.sender_id]
    is_audio = message.type == AUDIO_MESSAGE_TYPE
    truncated = len(eligible) > limits.receipt_named_list_max_members

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
                session, chat_id, settings.RECEIPT_KIND_DELIVERED, message_id, eligible
            ),
            "read": await crud_receipt.crosser_count_for_message(
                session, chat_id, settings.RECEIPT_KIND_READ, message_id, eligible
            ),
            "played": (
                await crud_receipt.crosser_count_for_message(
                    session, chat_id, settings.RECEIPT_KIND_PLAYED, message_id, eligible
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
            await crud_receipt.crossers_for_message(session, chat_id, settings.RECEIPT_KIND_DELIVERED, message_id)
        )
        read_by = _entries(
            await crud_receipt.crossers_for_message(session, chat_id, settings.RECEIPT_KIND_READ, message_id)
        )
        played_by = (
            _entries(await crud_receipt.crossers_for_message(session, chat_id, settings.RECEIPT_KIND_PLAYED, message_id))
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

    if hide_read:
        # 1:1 chat where a peer hid read receipts: READ/PLAYED are masked to
        # DELIVERED for both users (ADR 0003). Delivery data is untouched.
        payload["counts"]["read"] = 0
        payload["counts"]["played"] = 0
        if not truncated:
            payload["read_by"] = []
            payload["played_by"] = []
            payload["pending"] = [str(uid) for uid in eligible]

    await redis_client.set(cache_key, json.dumps(payload), ex=_RECEIPT_DETAIL_CACHE_TTL_SECONDS)
    return payload
