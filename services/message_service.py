import asyncio
from typing import Optional, Sequence
from sqlalchemy.ext.asyncio import AsyncSession

from database.crud.crud_chat import get_chat_by_id
from database.crud.crud_message import (
    compute_message_status,
    create_message,
    edit_message_content,
    get_chat_messages,
    get_message_by_id,
    soft_delete_message,
)
from database.crud.crud_participant import (
    get_chat_participants,
    is_participant,
    update_last_delivered_message,
    update_last_read_message,
)
from database.models.message import Message, MessageStatus
from config import (
    MAX_MEDIA_FILENAME_LENGTH,
    MAX_MESSAGE_CONTENT_LENGTH,
    MEDIA_KIND_BY_MESSAGE_TYPE,
    MEDIA_MESSAGE_TYPES,
)
from services import notification_service, presence_service, realtime_service
from services.storage import media_service
from services.storage.errors import MediaNotFoundError, MediaValidationError
from services.redis_client import redis_client
from utils.snowflake import next_id

# System messages ("X joined the group", etc.) have no sender
SYSTEM_MESSAGE_TYPE = 6


class MessageTooLongError(Exception):
    pass


def _check_content_length(content: Optional[str]) -> None:
    if content is not None and len(content) > MAX_MESSAGE_CONTENT_LENGTH:
        raise MessageTooLongError(f"Message content exceeds {MAX_MESSAGE_CONTENT_LENGTH} characters")

# How long a client_message_id is remembered for idempotency - long enough to
# cover any realistic client retry window (a flaky connection retrying a send).
_IDEMPOTENCY_TTL_SECONDS = 86400
_IDEMPOTENCY_KEY_PREFIX = "msg_idem:"


class NotAParticipantError(Exception):
    pass


class MediaAttachment:
    """Validated media fields for one outgoing message (see _validate_media)."""

    __slots__ = ("key", "mime", "size", "name", "duration_seconds")

    def __init__(self, key, mime, size, name, duration_seconds):
        self.key = key
        self.mime = mime
        self.size = size
        self.name = name
        self.duration_seconds = duration_seconds


async def _validate_media(type: int, media: Optional[dict]) -> Optional[MediaAttachment]:
    """
    Validate the media payload for a media-type message (2=image/3=video/
    4=audio/5=file). Confirms the object actually exists in storage (HEAD)
    and that its real content-type/size are within the per-kind limits -
    a client-declared key is never trusted. Returns None for a text/system
    message. Raises MediaValidationError (-> 400) / MediaNotFoundError (-> 404).
    """
    if type not in MEDIA_MESSAGE_TYPES:
        if media:
            raise MediaValidationError("media payload is only valid for a media-type message")
        return None

    if not media or not media.get("key"):
        raise MediaValidationError("a media-type message requires a media.key")

    kind = MEDIA_KIND_BY_MESSAGE_TYPE[type]
    key = str(media["key"])

    # Authoritative type/size from storage - not the client's declared values.
    meta = await media_service.object_metadata(key)

    allowed = media_service.ALLOWED_UPLOAD_MIME.get(kind, set())
    if meta.content_type not in allowed:
        raise MediaValidationError(
            f"stored object type {meta.content_type!r} is not allowed for {kind!r}"
        )
    ceiling = media_service.MAX_UPLOAD_BYTES_BY_KIND[kind]
    if meta.size <= 0 or meta.size > ceiling:
        raise MediaValidationError(
            f"stored object size {meta.size} is outside the limit for {kind!r}"
        )

    name = media.get("name")
    if name is not None:
        name = str(name)[:MAX_MEDIA_FILENAME_LENGTH]

    duration = media.get("duration_seconds")
    duration = int(duration) if duration is not None else None

    return MediaAttachment(key=key, mime=meta.content_type, size=meta.size, name=name, duration_seconds=duration)


def _idempotency_key(chat_id: int, client_message_id: str) -> str:
    return f"{_IDEMPOTENCY_KEY_PREFIX}{chat_id}:{client_message_id}"


async def send_message(
    session: AsyncSession,
    sender_id: int,
    chat_id: int,
    client_message_id: str,
    content: Optional[str] = None,
    type: int = 1,
    reply_to_message_id: Optional[int] = None,
    media: Optional[dict] = None,
) -> Message:
    """
    The full send flow: idempotency check, permission check, persist,
    acknowledge, fan out to whoever's connected, push to whoever isn't.

    ``media`` (for a media-type message: 2=image/3=video/4=audio/5=file) is
    ``{"key", "name"?, "duration_seconds"?}`` - the object storage key the
    client got from an upload ticket and already PUT its bytes to. It's
    HEAD-verified against storage and the per-kind size/MIME limits here.
    """
    _check_content_length(content)

    if not await is_participant(session, chat_id, sender_id):
        raise NotAParticipantError(f"User {sender_id} is not a participant of chat {chat_id}")

    attachment = await _validate_media(type, media)

    idem_key = _idempotency_key(chat_id, client_message_id)

    # Reserve this client_message_id up front. If a retry races in before the
    # first attempt has written its id back, it briefly finds "pending" - see
    # the retry loop below - rather than a false negative (SETNX failing) or
    # slipping through as a duplicate insert.
    reserved = await redis_client.set(idem_key, "pending", nx=True, ex=_IDEMPOTENCY_TTL_SECONDS)

    if not reserved:
        existing_message_id = await _wait_for_idempotent_result(idem_key)
        if existing_message_id is not None:
            existing = await get_message_by_id(session, chat_id=chat_id, message_id=existing_message_id)
            if existing is not None:
                return existing
        # The original attempt's process crashed before writing the result back;
        # fall through and actually send it so the client isn't left hanging forever.

    message_id = next_id()
    message = await create_message(
        session,
        message_id=message_id,
        chat_id=chat_id,
        sender_id=sender_id,
        type=type,
        content=content,
        reply_to_message_id=reply_to_message_id,
        media_key=attachment.key if attachment else None,
        media_mime=attachment.mime if attachment else None,
        media_size=attachment.size if attachment else None,
        media_name=attachment.name if attachment else None,
        media_duration_seconds=attachment.duration_seconds if attachment else None,
    )

    await redis_client.set(idem_key, str(message.id), ex=_IDEMPOTENCY_TTL_SECONDS)

    await _fan_out(session, message)
    return message


async def _wait_for_idempotent_result(idem_key: str, attempts: int = 20, delay_seconds: float = 0.1) -> Optional[int]:
    for _ in range(attempts):
        value = await redis_client.get(idem_key)
        if value != "pending":
            return int(value) if value is not None else None
        await asyncio.sleep(delay_seconds)
    return None


async def send_system_message(session: AsyncSession, chat_id: int, content: str) -> Message:
    """No sender, no idempotency/permission check - triggered internally by chat_service."""
    message = await create_message(
        session,
        message_id=next_id(),
        chat_id=chat_id,
        sender_id=None,
        type=SYSTEM_MESSAGE_TYPE,
        content=content,
    )
    await _fan_out(session, message)
    return message


_PUSH_BODY_BY_MEDIA_TYPE = {2: "\U0001F4F7 Photo", 3: "\U0001F3A5 Video", 4: "\U0001F3A4 Voice message", 5: "\U0001F4CE File"}


def _push_body_for_media(type: int) -> str:
    return _PUSH_BODY_BY_MEDIA_TYPE.get(type, "")


async def _fan_out(session: AsyncSession, message: Message) -> None:
    # Ids go out as strings - a JSON number bigger than 2^53 (every one of
    # our Snowflake ids) silently loses precision the instant a browser
    # parses it. See routers/schemas.py's IdStr for the full story; this is
    # the WebSocket-side half of the same fix (Pydantic only covers REST).
    event = {
        "event": "new_message",
        "chat_id": str(message.chat_id),
        "message_id": str(message.id),
        "sender_id": str(message.sender_id) if message.sender_id is not None else None,
        "type": message.type,
        "content": message.content,
        "reply_to_message_id": str(message.reply_to_message_id) if message.reply_to_message_id is not None else None,
        # Media attachment (null for a text / system message). media_url is a
        # short-lived presigned GET so a client rendering the message live
        # doesn't need a second round trip to fetch it.
        "media_url": media_service.message_media_download_url(message.media_key),
        "media_mime": message.media_mime,
        "media_size": message.media_size,
        "media_name": message.media_name,
        "media_duration_seconds": message.media_duration_seconds,
        "created_at": message.created_at.isoformat(),
        # Always SENT at the instant a message is created - the chat-wide
        # receipt cursors can't already cover an id that didn't exist a
        # moment ago, so this needs no DB lookup to be correct.
        "status": MessageStatus.SENT,
    }
    await realtime_service.publish_event(message.chat_id, event)

    participants = await get_chat_participants(session, message.chat_id)
    recipient_ids = [p.user_id for p in participants if p.user_id != message.sender_id]

    online_ids = await presence_service.get_online_participants(recipient_ids)
    offline_ids = [uid for uid in recipient_ids if uid not in online_ids]

    # Concurrent, not sequential: one at a time, this loop's latency scales
    # with the chat's offline member count - for a large group that's a real
    # delay on the sender's own ack, since send_message() awaits _fan_out()
    # before returning.
    await asyncio.gather(
        *(
            notification_service.send_push(
                user_id,
                title="New message",
                body=message.content or _push_body_for_media(message.type),
                data={"chat_id": str(message.chat_id), "message_id": str(message.id)},
            )
            for user_id in offline_ids
        ),
        return_exceptions=True,
    )


async def get_message_history(
    session: AsyncSession,
    user_id: int,
    chat_id: int,
    before_id: Optional[int] = None,
    limit: int = 50,
) -> Sequence[Message]:
    if not await is_participant(session, chat_id, user_id):
        raise NotAParticipantError(f"User {user_id} is not a participant of chat {chat_id}")

    chat = await get_chat_by_id(session, chat_id)
    messages = await get_chat_messages(session, chat_id=chat_id, before_id=before_id, limit=limit)

    # Attached rather than a stored column - see MessageStatus. One extra
    # row fetch (the chat) for the whole page, then an O(1) comparison per
    # message already in hand; no per-message query.
    for message in messages:
        message.status = compute_message_status(message.id, chat)
        # Presigned GET for the attachment, same as the live fan-out event.
        # Callers serving large pages should cache per key for its TTL.
        message.media_url = media_service.message_media_download_url(message.media_key)

    return messages


async def edit_message(session: AsyncSession, user_id: int, chat_id: int, message_id: int, new_content: str) -> Message:
    _check_content_length(new_content)

    existing = await get_message_by_id(session, chat_id=chat_id, message_id=message_id)
    if existing is None or existing.sender_id != user_id:
        raise NotAParticipantError(f"User {user_id} may not edit message {message_id}")

    message = await edit_message_content(session, chat_id=chat_id, message_id=message_id, new_content=new_content)
    await realtime_service.publish_event(
        chat_id, {"event": "message_edited", "chat_id": str(chat_id), "message_id": str(message_id), "content": new_content}
    )
    return message


async def delete_message(session: AsyncSession, user_id: int, chat_id: int, message_id: int) -> bool:
    existing = await get_message_by_id(session, chat_id=chat_id, message_id=message_id)
    if existing is None or existing.sender_id != user_id:
        raise NotAParticipantError(f"User {user_id} may not delete message {message_id}")

    deleted = await soft_delete_message(session, chat_id=chat_id, message_id=message_id)
    if deleted:
        await realtime_service.publish_event(
            chat_id, {"event": "message_deleted", "chat_id": str(chat_id), "message_id": str(message_id)}
        )
    return deleted


async def mark_as_delivered(session: AsyncSession, user_id: int, chat_id: int, message_id: int) -> None:
    await update_last_delivered_message(session, chat_id=chat_id, user_id=user_id, message_id=message_id)
    await realtime_service.publish_event(
        chat_id, {"event": "delivery_receipt", "chat_id": str(chat_id), "user_id": str(user_id), "message_id": str(message_id)}
    )


async def mark_as_read(session: AsyncSession, user_id: int, chat_id: int, message_id: int) -> None:
    await update_last_read_message(session, chat_id=chat_id, user_id=user_id, message_id=message_id)
    await realtime_service.publish_event(
        chat_id, {"event": "read_receipt", "chat_id": str(chat_id), "user_id": str(user_id), "message_id": str(message_id)}
    )
