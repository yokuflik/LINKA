import logging
import uuid

import jwt
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from config import SEND_MESSAGE_RATE_LIMIT_MAX, SEND_MESSAGE_RATE_LIMIT_WINDOW_SECONDS, SERVER_ID
from database.connection import session_scope
from database.crud.crud_participant import get_all_chat_ids_for_user
from services import auth_service, message_service, presence_service, rate_limit_service
from services.connection_manager import connection_manager

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
                    chat_id=payload["chat_id"],
                    message_id=payload["message_id"],
                    new_content=payload["content"],
                )
            await websocket.send_json({"type": "ack", "for": "edit_message", "message_id": message.id})

        elif message_type == "delete_message":
            async with session_scope() as session:
                deleted = await message_service.delete_message(
                    session, user_id=user_id, chat_id=payload["chat_id"], message_id=payload["message_id"]
                )
            await websocket.send_json({"type": "ack", "for": "delete_message", "deleted": deleted})

        elif message_type == "mark_read":
            async with session_scope() as session:
                await message_service.mark_as_read(
                    session, user_id=user_id, chat_id=payload["chat_id"], message_id=payload["message_id"]
                )
            await websocket.send_json({"type": "ack", "for": "mark_read"})

        else:
            await websocket.send_json({"type": "error", "code": "unknown_type", "message": f"Unknown message type: {message_type!r}"})

    except message_service.NotAParticipantError as e:
        await websocket.send_json({"type": "error", "code": "forbidden", "message": str(e)})
    except KeyError as e:
        await websocket.send_json({"type": "error", "code": "bad_request", "message": f"Missing field: {e}"})
    except Exception as e:
        logger.exception(f"Unhandled error dispatching {message_type!r} for user {user_id}")
        await websocket.send_json({"type": "error", "code": "internal_error", "message": str(e)})


async def _handle_send_message(user_id: int, payload: dict, websocket: WebSocket) -> None:
    allowed = await rate_limit_service.check_and_increment(
        user_id, "send_message", max_per_window=SEND_MESSAGE_RATE_LIMIT_MAX, window_seconds=SEND_MESSAGE_RATE_LIMIT_WINDOW_SECONDS
    )
    if not allowed:
        await websocket.send_json({"type": "error", "code": "rate_limited", "client_message_id": payload.get("client_message_id")})
        return

    async with session_scope() as session:
        message = await message_service.send_message(
            session,
            sender_id=user_id,
            chat_id=payload["chat_id"],
            client_message_id=payload["client_message_id"],
            content=payload.get("content"),
            type=payload.get("message_type", 1),
            reply_to_message_id=payload.get("reply_to_message_id"),
        )

    await websocket.send_json({
        "type": "ack",
        "for": "send_message",
        "client_message_id": payload["client_message_id"],
        "message_id": message.id,
        "created_at": message.created_at.isoformat(),
    })
