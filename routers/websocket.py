import logging
import uuid

import jwt
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from config import SEND_MESSAGE_RATE_LIMIT_MAX, SEND_MESSAGE_RATE_LIMIT_WINDOW_SECONDS, SERVER_ID
from database.connection import session_scope
from database.crud.crud_participant import get_all_chat_ids_for_user, is_participant
from database.crud.crud_private_chat_pair import get_pair_chat_id
from services import auth_service, message_service, presence_service, rate_limit_service, realtime_service
from services.connection_manager import connection_manager
from services.storage.errors import MediaNotFoundError, MediaValidationError

logger = logging.getLogger(__name__)

router = APIRouter()

# WebSocket close codes in the 4000-4999 range are reserved for application
# use (per RFC 6455) - 4401 mirrors HTTP 401 so a client can tell
# "bad/expired token" apart from a generic close.
_CLOSE_UNAUTHORIZED = 4401


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = Query(...)):
    """
    Auth happens via a `token` query param (WebSocket clients can't send
    custom headers as cleanly as HTTP ones) and is checked *before*
    websocket.accept(), so a bad/expired token never gets a live connection.
    """
    try:
        user_id = auth_service.verify_access_token(token)
    except jwt.PyJWTError:
        await websocket.close(code=_CLOSE_UNAUTHORIZED)
        return

    await websocket.accept()
    connection_id = str(uuid.uuid4())

    # A DB session is opened per-operation below, never held for the whole
    # (potentially hours-long) connection lifetime - doing otherwise would
    # tie up one pooled connection per open WebSocket, which doesn't scale.
    async with session_scope() as session:
        chat_ids = list(await get_all_chat_ids_for_user(session, user_id))

    await connection_manager.connect(user_id, connection_id, websocket, chat_ids)
    await presence_service.mark_online(user_id, connection_id, SERVER_ID)

    try:
        while True:
            payload = await websocket.receive_json()
            await _dispatch(user_id, connection_id, payload, websocket)
    except WebSocketDisconnect:
        pass
    finally:
        await connection_manager.disconnect(connection_id)
        await presence_service.mark_offline(user_id, connection_id)


async def _dispatch(user_id: int, connection_id: str, payload: dict, websocket: WebSocket) -> None:
    """
    Every branch is wrapped so one bad/malformed message (or a permission
    error) never tears down the whole connection - only send_message's own
    exceptions are handled per-branch below, everything else falls through
    to the catch-all so a client typo can't kill their session.
    """
    message_type = payload.get("type")

    try:
        if message_type == "heartbeat":
            await presence_service.heartbeat(user_id, connection_id)
            await websocket.send_json({"type": "heartbeat_ack"})

        elif message_type == "send_message":
            await _handle_send_message(user_id, payload, websocket)

        elif message_type == "edit_message":
            async with session_scope() as session:
                message = await message_service.edit_message(
                    session,
                    user_id=user_id,
                    chat_id=int(payload["chat_id"]),
                    message_id=int(payload["message_id"]),
                    new_content=payload["content"],
                )
            await websocket.send_json({"type": "ack", "for": "edit_message", "message_id": str(message.id)})

        elif message_type == "delete_message":
            async with session_scope() as session:
                deleted = await message_service.delete_message(
                    session, user_id=user_id, chat_id=int(payload["chat_id"]), message_id=int(payload["message_id"])
                )
            await websocket.send_json({"type": "ack", "for": "delete_message", "deleted": deleted})

        elif message_type == "mark_delivered":
            async with session_scope() as session:
                await message_service.mark_as_delivered(
                    session, user_id=user_id, chat_id=int(payload["chat_id"]), message_id=int(payload["message_id"])
                )
            await websocket.send_json({"type": "ack", "for": "mark_delivered"})

        elif message_type == "mark_read":
            async with session_scope() as session:
                await message_service.mark_as_read(
                    session, user_id=user_id, chat_id=int(payload["chat_id"]), message_id=int(payload["message_id"])
                )
            await websocket.send_json({"type": "ack", "for": "mark_read"})

        elif message_type == "mark_played":
            async with session_scope() as session:
                await message_service.mark_as_played(
                    session, user_id=user_id, chat_id=int(payload["chat_id"]), message_id=int(payload["message_id"])
                )
            await websocket.send_json({"type": "ack", "for": "mark_played"})

        elif message_type == "typing":
            await _handle_typing(user_id, payload)

        elif message_type == "recording":
            await _handle_typing(user_id, payload, kind="recording_audio")

        elif message_type == "subscribe_presence":
            await _handle_subscribe_presence(user_id, connection_id, payload, websocket)

        elif message_type == "unsubscribe_presence":
            target_user_id = int(payload["user_id"])
            await connection_manager.unsubscribe_presence(connection_id, target_user_id)

        else:
            await websocket.send_json({"type": "error", "code": "unknown_type", "message": f"Unknown message type: {message_type!r}"})

    except message_service.NotAParticipantError as e:
        await websocket.send_json({"type": "error", "code": "forbidden", "message": str(e)})
    except (message_service.MessageTooLongError, message_service.NotAVoiceMessageError) as e:
        await websocket.send_json({"type": "error", "code": "bad_request", "message": str(e)})
    except MediaNotFoundError as e:
        await websocket.send_json({"type": "error", "code": "not_found", "message": str(e)})
    except MediaValidationError as e:
        await websocket.send_json({"type": "error", "code": "bad_request", "message": str(e)})
    except (KeyError, ValueError, TypeError) as e:
        # KeyError: a required field is missing. ValueError/TypeError: an id
        # field was present but not parseable as an int (e.g. garbage, or a
        # client-side bug reintroducing the float-precision issue below).
        await websocket.send_json({"type": "error", "code": "bad_request", "message": f"Invalid request: {e}"})
    except Exception:
        # Deliberately not str(e) here: an unexpected internal error's real
        # message (a DB error, a stack detail) is exactly the kind of thing
        # that shouldn't leak to the client - it's already fully captured
        # below via logger.exception() for whoever operates this service.
        logger.exception(f"Unhandled error dispatching {message_type!r} for user {user_id}")
        await websocket.send_json({"type": "error", "code": "internal_error", "message": "Something went wrong"})


async def _handle_send_message(user_id: int, payload: dict, websocket: WebSocket) -> None:
    allowed = await rate_limit_service.check_and_increment(
        user_id, "send_message", max_per_window=SEND_MESSAGE_RATE_LIMIT_MAX, window_seconds=SEND_MESSAGE_RATE_LIMIT_WINDOW_SECONDS
    )
    if not allowed:
        await websocket.send_json({"type": "error", "code": "rate_limited", "client_message_id": payload.get("client_message_id")})
        return

    reply_to_message_id = payload.get("reply_to_message_id")

    # Media message payload: {"media": {"key", "name"?, "duration_seconds"?}}
    # plus message_type 2/3/4/5. The key is HEAD-verified against storage in
    # message_service - a raw client key is never trusted.
    media = payload.get("media")
    if media is not None and not isinstance(media, dict):
        raise ValueError("media must be an object")

    async with session_scope() as session:
        message = await message_service.send_message(
            session,
            sender_id=user_id,
            chat_id=int(payload["chat_id"]),
            client_message_id=payload["client_message_id"],
            content=payload.get("content"),
            type=payload.get("message_type", 1),
            reply_to_message_id=int(reply_to_message_id) if reply_to_message_id is not None else None,
            media=media,
        )

    await websocket.send_json({
        "type": "ack",
        "for": "send_message",
        "client_message_id": payload["client_message_id"],
        "message_id": str(message.id),
        "created_at": message.created_at.isoformat(),
    })


async def _handle_typing(user_id: int, payload: dict, kind: str = "typing") -> None:
    """
    Fully ephemeral - no DB persistence, no ack. Fanned out to the chat like
    any other chat event (new_message, receipts, ...) via the same
    realtime_service/connection_manager pipeline, so it reuses the existing
    per-chat Redis channel instead of a new mechanism. The client is
    responsible for expiring the state on its own (~5s) rather than the
    server ever sending a matching "stopped" event - see CLAUDE.md's
    typing-indicator section.

    Same handler covers both the text-composing ("typing") and the
    voice-recording ("recording_audio") activity kinds - identical
    authorization and fan-out, only the "kind" field differs. The wire
    event stays "typing" so existing clients keep working; a client that
    doesn't understand a kind can treat it as plain typing.
    """
    chat_id = int(payload["chat_id"])

    async with session_scope() as session:
        if not await is_participant(session, chat_id, user_id):
            raise message_service.NotAParticipantError(f"User {user_id} is not a participant of chat {chat_id}")

    await realtime_service.publish_event(chat_id, {
        "event": "typing",
        "kind": kind,
        "chat_id": str(chat_id),
        "user_id": str(user_id),
    })


async def _handle_subscribe_presence(user_id: int, connection_id: str, payload: dict, websocket: WebSocket) -> None:
    """
    Subscribe-on-demand presence (see CLAUDE.md): the client sends this only
    when it opens a private (1:1) chat, never for a group - there is no
    group-presence concept at all, by design. Authorization is "does a
    private chat between these two users exist" (get_pair_chat_id, the same
    PrivateChatPair lookup get_or_create_private_chat uses) - this is what
    stops a client from watching an arbitrary user's presence just by
    knowing their id.
    """
    target_user_id = int(payload["user_id"])
    if target_user_id == user_id:
        await websocket.send_json({"type": "error", "code": "bad_request", "message": "Cannot subscribe to your own presence"})
        return

    async with session_scope() as session:
        chat_id = await get_pair_chat_id(session, user_id, target_user_id)

    if chat_id is None:
        await websocket.send_json({"type": "error", "code": "forbidden", "message": "No private chat with that user"})
        return

    await connection_manager.subscribe_presence(connection_id, target_user_id)
    status = await presence_service.get_status(target_user_id)
    await websocket.send_json({
        "type": "presence_status",
        "user_id": str(target_user_id),
        "status": status["status"],
        "last_seen_at": status["last_seen_at"],
    })
