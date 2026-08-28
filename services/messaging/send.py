"""The outgoing-message send path and fan-out: process_outgoing, send_system_message, fan_out_message."""

import asyncio
from datetime import datetime, timezone
from typing import Optional
from sqlalchemy.ext.asyncio import AsyncSession

from database.crud.crud_message import create_message
from database.crud.crud_participant import get_chat_participants, is_participant
from database.models.message import Message, MessageStatus
from services import notification_service, presence_service, realtime_service
from services.fanout import send_queue
from services.messaging.common import SYSTEM_MESSAGE_TYPE, _check_content_length
from services.messaging.errors import MessageAlreadySentError, NotAParticipantError
from services.messaging.media_validation import _validate_media
from services.storage import media_service
from services.redis_client import redis_client
from utils.snowflake import next_id

# How long a client_message_id is remembered for idempotency - long enough to
# cover any realistic client retry window (a flaky connection retrying a send).
_IDEMPOTENCY_TTL_SECONDS = 86400
_IDEMPOTENCY_KEY_PREFIX = "msg_idem:"


def _idempotency_key(chat_id: int, client_message_id: str) -> str:
    return f"{_IDEMPOTENCY_KEY_PREFIX}{chat_id}:{client_message_id}"


async def process_outgoing(
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
    The full send flow, run by the fan-out worker off ``message_send_stream``
    (no longer inline on the WebSocket request path): idempotency check,
    permission check, persist, fan out to whoever's connected, push to
    whoever isn't.

    ``media`` (for a media-type message: 2=image/3=video/4=audio/5=file) is
    ``{"key", "name"?, "duration_seconds"?}`` - the object storage key the
    client got from an upload ticket and already PUT its bytes to. It's
    HEAD-verified against storage and the per-kind size/MIME limits here.

    Raises MessageAlreadySentError if this client_message_id was already
    written (duplicate stream entry).
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
            # Already written by an earlier stream entry for the same
            # client_message_id. Don't re-persist / re-fan-out; let the worker
            # tell the sender to reconcile.
            raise MessageAlreadySentError(existing_message_id)
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

    # Fan-out is a second hop off its own stream (services/fanout/fanout_worker):
    # the message row is already committed by create_message, so the worker can
    # load it. Building the event + publishing + pushing to offline members no
    # longer blocks this worker's per-message drain.
    await send_queue.enqueue_fanout(
        message_id=message.id,
        chat_id=message.chat_id,
        sender_id=message.sender_id,
        client_message_id=client_message_id,
    )
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
    # Same queue as normal messages so ordering within a chat is preserved.
    await send_queue.enqueue_fanout(
        message_id=message.id, chat_id=message.chat_id, sender_id=None
    )
    return message


_PUSH_BODY_BY_MEDIA_TYPE = {2: "\U0001F4F7 Photo", 3: "\U0001F3A5 Video", 4: "\U0001F3A4 Voice message", 5: "\U0001F4CE File"}


def _push_body_for_media(type: int) -> str:
    return _PUSH_BODY_BY_MEDIA_TYPE.get(type, "")


async def fan_out_message(session: AsyncSession, message: Message, client_message_id: Optional[str] = None) -> None:
    """
    Build the ``new_message`` event for a persisted message, publish it to the
    chat channel, and push to whichever members are offline. Called by
    services/fanout/fanout_worker off ``message_fanout_stream`` - no longer
    inline on the send path.
    """
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
        # Echoed back only on the live event (never persisted on Message) so
        # the sender's own connection can match this to its optimistic bubble
        # and swap in the real id / created_at. Null for a system message.
        "client_message_id": client_message_id,
        # Always SENT at the instant a message is created - the chat-wide
        # receipt cursors can't already cover an id that didn't exist a
        # moment ago, so this needs no DB lookup to be correct.
        "status": MessageStatus.SENT,
    }
    await realtime_service.publish_event(message.chat_id, event)

    participants = await get_chat_participants(session, message.chat_id)
    recipient_ids = [p.user_id for p in participants if p.user_id != message.sender_id]

    online_ids = await presence_service.get_online_participants(recipient_ids)

    # Recipients who muted this chat get no offline push (ADR 0004). A
    # connected client filters muted chats itself; this is the only
    # server-side effect of muting. "muted forever" is a far-future
    # muted_until, so a single "> now()" check covers every case.
    now = datetime.now(timezone.utc)
    muted_ids = {
        p.user_id
        for p in participants
        if p.muted_until is not None and p.muted_until > now
    }
    offline_ids = [
        uid for uid in recipient_ids if uid not in online_ids and uid not in muted_ids
    ]

    # Concurrent, not sequential: one at a time, this loop's latency scales
    # with the chat's offline member count. It runs in the fan-out worker now,
    # off the request path entirely.
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
