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
from config import MAX_MESSAGE_CONTENT_LENGTH
from services import notification_service, presence_service, realtime_service
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
) -> Message:
    """
    The full send flow: idempotency check, permission check, persist,
    acknowledge, fan out to whoever's connected, push to whoever isn't.
    """
    _check_content_length(content)

    if not await is_participant(session, chat_id, sender_id):
        raise NotAParticipantError(f"User {sender_id} is not a participant of chat {chat_id}")

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
                body=message.content or "",
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
