"""
Service layer for scheduled messages (ADR 0031).

Thin business logic over `crud_scheduled`, called by the REST router in
`modules/messaging/router.py`. The actual delivery at fire time is the poll
worker's job (realtime/fanout/scheduled_worker.py); this module only manages
the pending state (create / list / reschedule / cancel) and the Redis due-set
+ media-ref bookkeeping that go with it.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from modules.messaging.limits import DEFAULT_SCHEDULED_LIMITS, ScheduledLimits
from infra.ids.client import next_id
from infra.redis.client import redis_client
from modules.chats.crud.crud_participant import is_participant
from modules.media import media_service
from modules.media.crud import delete_blob_row, deref_blob, get_blob_by_key
from modules.messaging import crud_scheduled
from modules.messaging.common import _check_content_length
from modules.messaging.errors import (
    NotAParticipantError,
    ScheduledLimitExceededError,
    ScheduledMessageNotFoundError,
    ScheduledTimeInvalidError,
)
from modules.messaging.media_validation import _clean_blur_hash
from modules.messaging.models import ScheduledMessage

logger = logging.getLogger(__name__)

# type 6 is a system message - never schedulable.
_SYSTEM_MESSAGE_TYPE = 6


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_epoch(dt: datetime) -> float:
    """ZSET score for a fire time. Naive datetimes are treated as UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _validate_time(scheduled_for: datetime, limits: ScheduledLimits) -> None:
    now = _utcnow()
    when = scheduled_for
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    earliest = now + timedelta(seconds=limits.min_lead_seconds)
    latest = now + timedelta(days=limits.max_lead_days)
    if when < earliest:
        raise ScheduledTimeInvalidError(
            f"scheduled_for must be at least {limits.min_lead_seconds}s in the future"
        )
    if when > latest:
        raise ScheduledTimeInvalidError(
            f"scheduled_for must be within {limits.max_lead_days} days"
        )


async def _due_set_add(scheduled_message_id: int, scheduled_for: datetime) -> None:
    try:
        await redis_client.zadd(
            settings.SCHEDULED_DUE_SET_KEY, {str(scheduled_message_id): _as_epoch(scheduled_for)}
        )
    except Exception as exc:  # noqa: BLE001 - Postgres is the source of truth; reconcile self-heals
        logger.warning("scheduled: due-set ZADD failed for %s: %s", scheduled_message_id, exc)


async def _due_set_remove(scheduled_message_id: int) -> None:
    try:
        await redis_client.zrem(settings.SCHEDULED_DUE_SET_KEY, str(scheduled_message_id))
    except Exception as exc:  # noqa: BLE001 - a stale entry is harmless: the worker re-checks status
        logger.warning("scheduled: due-set ZREM failed for %s: %s", scheduled_message_id, exc)


async def _ref_media_for_schedule(
    session: AsyncSession, message_type: int, media: Optional[dict]
) -> Optional[dict]:
    """
    Lenient media capture at schedule time (ADR 0031). The bytes were just PUT,
    so the authoritative HEAD is deferred to fire time; here we only confirm the
    key maps to a `media_blob` row and pin it with +1 ref so a concurrent purge
    of an identical earlier message can't delete the object before this fires.
    Returns the captured media dict (key/mime/size/name/duration/blur_hash) or
    None for a non-media message.
    """
    if message_type not in settings.MEDIA_MESSAGE_TYPES:
        if media and media.get("key"):
            raise ScheduledTimeInvalidError("media is only valid for a media-type message")
        return None
    if not media or not media.get("key"):
        raise ScheduledTimeInvalidError("a media-type message requires media.key")

    key = str(media["key"])
    blob = await get_blob_by_key(session, key)
    if blob is None:
        raise ScheduledTimeInvalidError("unknown media key; request an upload ticket first")

    name = media.get("name")
    if name is not None:
        name = str(name)
    duration = media.get("duration_seconds")
    duration = int(duration) if duration is not None else None
    blur_hash = _clean_blur_hash(media.get("blur_hash")) or blob.blur_hash

    # +1 ref (does not stamp uploaded_at - that happens on the real send).
    from modules.media.crud import confirm_and_ref

    await confirm_and_ref(
        session, storage_key=key, mime=blob.mime, size=blob.size, blur_hash=blur_hash
    )

    return {
        "key": key,
        "mime": blob.mime,
        "size": blob.size,
        "name": name,
        "duration_seconds": duration,
        "blur_hash": blur_hash,
    }


async def _deref_media(session: AsyncSession, media_key: Optional[str]) -> None:
    """Release the schedule-time +1 ref on cancel / permanent failure."""
    if not media_key:
        return
    try:
        remaining = await deref_blob(session, media_key)
        if remaining == 0:
            try:
                await media_service.delete_object(media_key)
            except Exception as exc:  # noqa: BLE001 - best effort
                logger.error("scheduled: failed to delete S3 object %s: %s", media_key, exc)
            await delete_blob_row(session, media_key)
    except Exception as exc:  # noqa: BLE001
        logger.error("scheduled: blob deref failed for %s: %s", media_key, exc)


async def schedule_message(
    session: AsyncSession,
    *,
    sender_id: int,
    chat_id: int,
    client_message_id: str,
    scheduled_for: datetime,
    message_type: int = 1,
    content: Optional[str] = None,
    media: Optional[dict] = None,
    reply_to_message_id: Optional[int] = None,
    limits: ScheduledLimits = DEFAULT_SCHEDULED_LIMITS,
) -> ScheduledMessage:
    """
    Validate and persist a pending scheduled message, then add it to the Redis
    due-set. Raises NotAParticipantError / ScheduledTimeInvalidError /
    ScheduledLimitExceededError / MessageTooLongError.
    """
    if message_type == _SYSTEM_MESSAGE_TYPE:
        raise ScheduledTimeInvalidError("system messages cannot be scheduled")

    _validate_time(scheduled_for, limits)
    _check_content_length(content, limits.max_message_content_length)

    if not await is_participant(session, chat_id, sender_id):
        raise NotAParticipantError(f"User {sender_id} is not a participant of chat {chat_id}")

    if await crud_scheduled.count_pending_for_user(session, sender_id) >= limits.max_pending_per_user:
        raise ScheduledLimitExceededError(
            f"at most {limits.max_pending_per_user} pending scheduled messages"
        )

    captured = await _ref_media_for_schedule(session, message_type, media)

    scheduled_message_id = await next_id()
    row = await crud_scheduled.create_scheduled(
        session,
        scheduled_message_id=scheduled_message_id,
        chat_id=chat_id,
        sender_id=sender_id,
        scheduled_for=scheduled_for,
        client_message_id=client_message_id,
        type=message_type,
        content=content,
        reply_to_message_id=reply_to_message_id,
        media_key=captured["key"] if captured else None,
        media_mime=captured["mime"] if captured else None,
        media_size=captured["size"] if captured else None,
        media_name=captured["name"] if captured else None,
        media_duration_seconds=captured["duration_seconds"] if captured else None,
        media_blur_hash=captured["blur_hash"] if captured else None,
    )
    await _due_set_add(row.id, row.scheduled_for)
    return row


async def list_scheduled(
    session: AsyncSession, sender_id: int, chat_id: Optional[int] = None
) -> Sequence[ScheduledMessage]:
    """Pending scheduled messages for the user, oldest fire time first."""
    return await crud_scheduled.list_pending_for_user(session, sender_id, chat_id)


async def reschedule(
    session: AsyncSession,
    *,
    sender_id: int,
    scheduled_message_id: int,
    scheduled_for: Optional[datetime] = None,
    content: Optional[str] = None,
    set_content: bool = False,
    limits: ScheduledLimits = DEFAULT_SCHEDULED_LIMITS,
) -> ScheduledMessage:
    """
    Change the fire time and/or caption of a pending row the user owns.
    Raises ScheduledTimeInvalidError / MessageTooLongError /
    ScheduledMessageNotFoundError.
    """
    if scheduled_for is not None:
        _validate_time(scheduled_for, limits)
    if set_content:
        _check_content_length(content, limits.max_message_content_length)

    row = await crud_scheduled.update_scheduled(
        session,
        scheduled_message_id,
        sender_id,
        scheduled_for=scheduled_for,
        content=content,
        set_content=set_content,
    )
    if row is None:
        raise ScheduledMessageNotFoundError(str(scheduled_message_id))

    if scheduled_for is not None:
        await _due_set_add(row.id, row.scheduled_for)
    return row


async def cancel_scheduled(
    session: AsyncSession, *, sender_id: int, scheduled_message_id: int
) -> ScheduledMessage:
    """
    Cancel a pending row the user owns: mark it cancelled, drop it from the
    due-set, and release its schedule-time media ref. Raises
    ScheduledMessageNotFoundError.
    """
    row = await crud_scheduled.cancel_scheduled(session, scheduled_message_id, sender_id)
    if row is None:
        raise ScheduledMessageNotFoundError(str(scheduled_message_id))

    await _due_set_remove(row.id)
    await _deref_media(session, row.media_key)
    return row
