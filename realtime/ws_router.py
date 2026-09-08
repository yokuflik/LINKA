import logging
import uuid

import jwt
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from config import (
    CORS_ALLOW_ORIGINS,
    SERVER_ID,
    WS_EDIT_RATE_MAX,
    WS_EDIT_RATE_WINDOW_SECONDS,
    WS_FRAME_FLOOD_STRIKES,
    WS_FRAME_RATE_MAX,
    WS_FRAME_RATE_WINDOW_SECONDS,
    WS_MAX_CHAT_IDS_ON_CONNECT,
    WS_RECEIPTS_RATE_MAX,
    WS_RECEIPTS_RATE_WINDOW_SECONDS,
    WS_SEND_MESSAGE_BURST_MAX,
    WS_SEND_MESSAGE_BURST_WINDOW_SECONDS,
    WS_SEND_MESSAGE_RATE_MAX,
    WS_SEND_MESSAGE_RATE_WINDOW_SECONDS,
    WS_SUBSCRIBE_PRESENCE_RATE_MAX,
    WS_SUBSCRIBE_PRESENCE_RATE_WINDOW_SECONDS,
    WS_TYPING_RATE_MAX,
    WS_TYPING_RATE_WINDOW_SECONDS,
    WS_UPGRADE_IP_RATE_LIMIT_MAX,
    WS_UPGRADE_IP_RATE_LIMIT_WINDOW_SECONDS,
    WS_UPGRADE_USER_RATE_LIMIT_MAX,
    WS_UPGRADE_USER_RATE_LIMIT_WINDOW_SECONDS,
)
from infra.db.connection import session_scope
from modules.chats.crud.crud_participant import get_all_chat_ids_for_user
from modules.chats.crud.crud_participant import get_chat_participants
from modules.chats.crud.crud_participant import is_participant
from modules.chats.crud.crud_private_chat_pair import get_pair_chat_id
from modules.auth import service as auth_service
from modules.messaging import service as message_service
from realtime import presence_service
from infra.ratelimit import service as rate_limit_service
from realtime import realtime_service
from realtime import ws_connection_registry
from modules.settings import service as settings_service
from realtime.connection_manager import connection_manager
from realtime.fanout import send_queue
from modules.media.errors import MediaNotFoundError
from modules.media.errors import MediaValidationError

logger = logging.getLogger(__name__)

router = APIRouter()

# WebSocket close codes in the 4000-4999 range are reserved for application
# use (per RFC 6455) - 4401 mirrors HTTP 401 so a client can tell
# "bad/expired token" apart from a generic close. 4403 = disallowed Origin.
_CLOSE_UNAUTHORIZED = 4401
_CLOSE_FORBIDDEN_ORIGIN = 4403
# 4429 = handshake-churn limit (too many upgrades from this IP / user in a
# short window). 4409 (an older connection evicted by the concurrent-connection
# cap) is emitted by connection_manager._handle_force_disconnect.
_CLOSE_HANDSHAKE_CHURN = 4429

# "*" (dev) allows any Origin, including none (native clients / file://).
_ORIGIN_WILDCARD = CORS_ALLOW_ORIGINS == ["*"]


def _origin_allowed(origin: str | None) -> bool:
    if _ORIGIN_WILDCARD:
        return True
    if origin is None:
        # A browser always sends Origin on a WS handshake; its absence means a
        # non-browser client, which same-origin prod doesn't expect.
        return False
    return origin in CORS_ALLOW_ORIGINS


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = Query(...)):
    """
    Auth happens via a `token` query param (WebSocket clients can't send
    custom headers as cleanly as HTTP ones) and is checked *before*
    websocket.accept(), so a bad/expired token never gets a live connection.
    The Origin is checked the same way (CSWSH protection) - closed 4403.
    """
    if not _origin_allowed(websocket.headers.get("origin")):
        await websocket.close(code=_CLOSE_FORBIDDEN_ORIGIN)
        return

    try:
        user_id = auth_service.verify_access_token(token)
    except jwt.PyJWTError:
        await websocket.close(code=_CLOSE_UNAUTHORIZED)
        return

    # Handshake churn: throttle *successful* upgrades per IP and per user
    # before doing any work (the per-connect DB query, the Redis registration).
    # A connect/disconnect storm otherwise thrashes Postgres and Redis.
    ip = rate_limit_service.client_ip(websocket)
    ip_ok = await rate_limit_service.check_sliding_window(
        ip, "ws_upgrade_ip", WS_UPGRADE_IP_RATE_LIMIT_MAX, WS_UPGRADE_IP_RATE_LIMIT_WINDOW_SECONDS
    )
    user_ok = await rate_limit_service.check_sliding_window(
        user_id, "ws_upgrade_user", WS_UPGRADE_USER_RATE_LIMIT_MAX, WS_UPGRADE_USER_RATE_LIMIT_WINDOW_SECONDS
    )
    if not (ip_ok and user_ok):
        await websocket.close(code=_CLOSE_HANDSHAKE_CHURN)
        return

    await websocket.accept()
    connection_id = str(uuid.uuid4())

    # Concurrent-connection cap (cross-process): record this connection and
    # evict the oldest ones over WS_CONN_MAX_CONNECTIONS. Each evicted member
    # is a connection on some process's inbox - tell that process to close it.
    evicted = await ws_connection_registry.register(user_id, SERVER_ID, connection_id)
    for member in evicted:
        server_id, evicted_connection_id = ws_connection_registry.split_member(member)
        await realtime_service.publish_to_instance(server_id, {
            "event": "force_disconnect",
            "connection_id": evicted_connection_id,
            "reason": "connection_limit",
        })

    # A DB session is opened per-operation below, never held for the whole
    # (potentially hours-long) connection lifetime - doing otherwise would
    # tie up one pooled connection per open WebSocket, which doesn't scale.
    async with session_scope() as session:
        chat_ids = list(
            await get_all_chat_ids_for_user(session, user_id, limit=WS_MAX_CHAT_IDS_ON_CONNECT)
        )

    await connection_manager.connect(user_id, connection_id, websocket, chat_ids)
    await presence_service.mark_online(user_id, connection_id, SERVER_ID)

    # Consecutive over-limit frames. Reset to 0 on any frame that passes the
    # rate check; a sustained flood (WS_FRAME_FLOOD_STRIKES in a row) closes.
    frame_flood_strikes = 0

    try:
        while True:
            payload = await websocket.receive_json()

            frame_ok = await rate_limit_service.check_sliding_window(
                connection_id, "ws_frame", WS_FRAME_RATE_MAX, WS_FRAME_RATE_WINDOW_SECONDS
            )
            if not frame_ok:
                frame_flood_strikes += 1
                if frame_flood_strikes >= WS_FRAME_FLOOD_STRIKES:
                    await websocket.close(code=_CLOSE_HANDSHAKE_CHURN)
                    break
                # Drop the frame - don't dispatch, don't close. A laggy client
                # that batches its sends is not an attacker. Echo
                # client_message_id (if any) so the sender can requeue the
                # dropped send rather than leave its bubble stuck on 🕓.
                drop_err = {"type": "error", "code": "rate_limited"}
                if isinstance(payload, dict) and payload.get("client_message_id") is not None:
                    drop_err["client_message_id"] = payload["client_message_id"]
                await websocket.send_json(drop_err)
                continue

            frame_flood_strikes = 0
            await _dispatch(user_id, connection_id, payload, websocket)
    except WebSocketDisconnect:
        pass
    finally:
        await connection_manager.disconnect(connection_id)
        await presence_service.mark_offline(user_id, connection_id)
        await ws_connection_registry.unregister(user_id, SERVER_ID, connection_id)


async def _dispatch(user_id: int, connection_id: str, payload: dict, websocket: WebSocket) -> None:
    """
    Every branch is wrapped so one bad/malformed message (or a permission
    error) never tears down the whole connection - only send_message's own
    exceptions are handled per-branch below, everything else falls through
    to the catch-all so a client typo can't kill their session.

    Routing is a plain dict lookup (message type -> handler). Every handler
    shares the same (user_id, connection_id, payload, websocket) signature so
    the dispatcher stays uniform regardless of which args a given handler
    actually uses; unknown types are handled inline here.
    """
    message_type = payload.get("type")
    handler = _HANDLERS.get(message_type)

    # Echoed back on every error frame so the client can reconcile the
    # optimistic bubble that triggered it (e.g. mark a queued send failed
    # instead of leaving it on 🕓). Harmless / absent for actions without one.
    client_message_id = payload.get("client_message_id")

    def _err(code: str, message: str | None = None) -> dict:
        frame = {"type": "error", "code": code}
        if message is not None:
            frame["message"] = message
        if client_message_id is not None:
            frame["client_message_id"] = client_message_id
        return frame

    try:
        if handler is None:
            await websocket.send_json(_err("unknown_type", f"Unknown message type: {message_type!r}"))
            return

        # Per-action rate limit (per user, sliding). send_message has its own
        # two-tier check inside its handler; everything else that's abusable
        # shares a bucket here. Actions absent from the table (heartbeat,
        # unsubscribe_presence) are unmetered by design - cheap and self-limiting.
        # The max/window are read from module globals at call time (not baked
        # into the table) so tests can monkeypatch a single knob.
        bucket = _ACTION_LIMITS.get(message_type)
        if bucket is not None:
            action, max_attr, window_attr = bucket
            if not await rate_limit_service.check_sliding_window(
                user_id, action, globals()[max_attr], globals()[window_attr]
            ):
                frame = _err("rate_limited")
                frame["for"] = message_type
                await websocket.send_json(frame)
                return

        await handler(user_id, connection_id, payload, websocket)

    except message_service.NotAParticipantError as e:
        await websocket.send_json(_err("forbidden", str(e)))
    except (message_service.MessageTooLongError, message_service.NotAVoiceMessageError) as e:
        await websocket.send_json(_err("bad_request", str(e)))
    except MediaNotFoundError as e:
        await websocket.send_json(_err("not_found", str(e)))
    except MediaValidationError as e:
        await websocket.send_json(_err("bad_request", str(e)))
    except (KeyError, ValueError, TypeError) as e:
        # KeyError: a required field is missing. ValueError/TypeError: an id
        # field was present but not parseable as an int (e.g. garbage, or a
        # client-side bug reintroducing the float-precision issue below).
        await websocket.send_json(_err("bad_request", f"Invalid request: {e}"))
    except Exception:
        # Deliberately not str(e) here: an unexpected internal error's real
        # message (a DB error, a stack detail) is exactly the kind of thing
        # that shouldn't leak to the client - it's already fully captured
        # below via logger.exception() for whoever operates this service.
        logger.exception(f"Unhandled error dispatching {message_type!r} for user {user_id}")
        await websocket.send_json(_err("internal_error", "Something went wrong"))


# --- Individual action handlers ------------------------------------------------
# Each handler is isolated and independently testable. They all accept the same
# four arguments even when they don't need every one, so _HANDLERS can invoke
# them uniformly.


async def _handle_heartbeat(user_id: int, connection_id: str, payload: dict, websocket: WebSocket) -> None:
    await presence_service.heartbeat(user_id, connection_id)
    await websocket.send_json({"type": "heartbeat_ack"})


async def _handle_edit_message(user_id: int, connection_id: str, payload: dict, websocket: WebSocket) -> None:
    async with session_scope() as session:
        message = await message_service.edit_message(
            session,
            user_id=user_id,
            chat_id=int(payload["chat_id"]),
            message_id=int(payload["message_id"]),
            new_content=payload["content"],
        )
    await websocket.send_json({"type": "ack", "for": "edit_message", "message_id": str(message.id)})


async def _handle_delete_message(user_id: int, connection_id: str, payload: dict, websocket: WebSocket) -> None:
    async with session_scope() as session:
        deleted = await message_service.delete_message(
            session, user_id=user_id, chat_id=int(payload["chat_id"]), message_id=int(payload["message_id"])
        )
    await websocket.send_json({"type": "ack", "for": "delete_message", "deleted": deleted})


async def _handle_restore_message(user_id: int, connection_id: str, payload: dict, websocket: WebSocket) -> None:
    async with session_scope() as session:
        message = await message_service.restore_message(
            session, user_id=user_id, chat_id=int(payload["chat_id"]), message_id=int(payload["message_id"])
        )
    await websocket.send_json(
        {"type": "ack", "for": "restore_message", "restored": message is not None}
    )


async def _handle_purge_message(user_id: int, connection_id: str, payload: dict, websocket: WebSocket) -> None:
    async with session_scope() as session:
        purged = await message_service.purge_message(
            session, user_id=user_id, chat_id=int(payload["chat_id"]), message_id=int(payload["message_id"])
        )
    await websocket.send_json({"type": "ack", "for": "purge_message", "purged": purged})


async def _handle_mark_delivered(user_id: int, connection_id: str, payload: dict, websocket: WebSocket) -> None:
    async with session_scope() as session:
        await message_service.mark_as_delivered(
            session, user_id=user_id, chat_id=int(payload["chat_id"]), message_id=int(payload["message_id"])
        )
    await websocket.send_json({"type": "ack", "for": "mark_delivered"})


async def _handle_mark_read(user_id: int, connection_id: str, payload: dict, websocket: WebSocket) -> None:
    async with session_scope() as session:
        await message_service.mark_as_read(
            session, user_id=user_id, chat_id=int(payload["chat_id"]), message_id=int(payload["message_id"])
        )
    await websocket.send_json({"type": "ack", "for": "mark_read"})


async def _handle_mark_played(user_id: int, connection_id: str, payload: dict, websocket: WebSocket) -> None:
    async with session_scope() as session:
        await message_service.mark_as_played(
            session, user_id=user_id, chat_id=int(payload["chat_id"]), message_id=int(payload["message_id"])
        )
    await websocket.send_json({"type": "ack", "for": "mark_played"})


async def _handle_typing(user_id: int, connection_id: str, payload: dict, websocket: WebSocket) -> None:
    await _publish_typing(user_id, payload)


async def _handle_recording(user_id: int, connection_id: str, payload: dict, websocket: WebSocket) -> None:
    await _publish_typing(user_id, payload, kind="recording_audio")


async def _handle_unsubscribe_presence(user_id: int, connection_id: str, payload: dict, websocket: WebSocket) -> None:
    target_user_id = int(payload["user_id"])
    await connection_manager.unsubscribe_presence(connection_id, target_user_id)


async def _handle_send_message(user_id: int, connection_id: str, payload: dict, websocket: WebSocket) -> None:
    # Two-tier sliding window: a 3/s primary (no boundary burst) plus a
    # 40/60s ceiling for sustained spam. Both must pass. Replaces the old
    # single 20/10s fixed window.
    primary_ok = await rate_limit_service.check_sliding_window(
        user_id, "send_message", WS_SEND_MESSAGE_RATE_MAX, WS_SEND_MESSAGE_RATE_WINDOW_SECONDS
    )
    burst_ok = await rate_limit_service.check_sliding_window(
        user_id, "send_message_burst", WS_SEND_MESSAGE_BURST_MAX, WS_SEND_MESSAGE_BURST_WINDOW_SECONDS
    )
    if not (primary_ok and burst_ok):
        await websocket.send_json({"type": "error", "code": "rate_limited", "client_message_id": payload.get("client_message_id")})
        return

    chat_id = int(payload["chat_id"])
    client_message_id = payload["client_message_id"]
    reply_to_message_id = payload.get("reply_to_message_id")

    # Media message payload: {"media": {"key", "name"?, "duration_seconds"?,
    # "blur_hash"?}} plus message_type 2/3/4/5. The key is HEAD-verified against
    # storage in the fan-out worker - a raw client key is never trusted;
    # blur_hash is validated (charset + length) there too (ADR 0014).
    media = payload.get("media")
    if media is not None and not isinstance(media, dict):
        raise ValueError("media must be an object")

    # Authorization stays synchronous - it's a cheap participant check and a
    # non-participant must never get a "queued" ack. Everything else (persist,
    # fan-out, push) is deferred to the send worker.
    async with session_scope() as session:
        if not await is_participant(session, chat_id, user_id):
            raise message_service.NotAParticipantError(
                f"User {user_id} is not a participant of chat {chat_id}"
            )

    try:
        await send_queue.enqueue_outgoing_message(
            chat_id=chat_id,
            sender_id=user_id,
            client_message_id=client_message_id,
            content=payload.get("content"),
            type=payload.get("message_type", 1),
            reply_to_message_id=int(reply_to_message_id) if reply_to_message_id is not None else None,
            media_key=media.get("key") if media else None,
            media_name=media.get("name") if media else None,
            media_duration_seconds=media.get("duration_seconds") if media else None,
            media_blur_hash=media.get("blur_hash") if media else None,
        )
    except Exception:
        # A dropped enqueue loses the message while the sender thinks it sent -
        # never swallow it (unlike the receipt stream).
        logger.exception("send_message: enqueue failed for user %s chat %s", user_id, chat_id)
        await websocket.send_json({
            "type": "error", "code": "internal_error", "client_message_id": client_message_id,
        })
        return

    await websocket.send_json({
        "type": "ack",
        "for": "send_message",
        "client_message_id": client_message_id,
        "status": "queued",
    })


async def _presence_authorized(session, watcher_id: int, target_user_id: int) -> bool:
    """
    Whether `watcher_id` is allowed to see `target_user_id`'s presence -
    both the live online indicator AND the "last seen" timestamp, which are
    a single signal gated together by the target's `privacy.online` setting:
      - `nobody`   -> never.
      - `contacts` -> only if a PrivateChatPair between the two exists
                      (get_pair_chat_id) - "someone I have a chat with".
      - `everyone` (default) -> any authenticated user.
    """
    visibility = await settings_service.get_online_visibility(session, target_user_id)
    if visibility == "nobody":
        return False
    if visibility == "contacts":
        return await get_pair_chat_id(session, watcher_id, target_user_id) is not None
    return True


async def _handle_subscribe_presence(user_id: int, connection_id: str, payload: dict, websocket: WebSocket) -> None:
    """
    Subscribe-on-demand presence (see CLAUDE.md): the client sends this when
    it opens a private (1:1) chat, never for a group - there is no
    group-presence concept at all, by design.

    Authorization (_presence_authorized) is the target's `privacy.online`
    setting, resolved here at subscribe time - NOT on each presence push, so
    a user with thousands of watchers costs nothing extra per connect/
    disconnect. To make a later privacy change take effect without a
    per-push check or a fan-out of revokes, the client re-sends
    subscribe_presence periodically (on its heartbeat) for the chat it has
    open; this handler re-runs the gate, and if the watcher is no longer
    allowed it tears the subscription down and tells the client via
    `presence_revoked`. Worst-case staleness is one heartbeat interval.
    """
    target_user_id = int(payload["user_id"])
    if target_user_id == user_id:
        await websocket.send_json({"type": "error", "code": "bad_request", "message": "Cannot subscribe to your own presence"})
        return

    async with session_scope() as session:
        allowed = await _presence_authorized(session, user_id, target_user_id)

    if not allowed:
        # Covers both a first subscribe that's denied and a periodic
        # re-subscribe that's no longer allowed (privacy tightened / chat
        # gone). Drop any existing subscription and let the client clear the
        # stale "online" it may still be showing.
        await connection_manager.unsubscribe_presence(connection_id, target_user_id)
        await websocket.send_json({
            "type": "presence_revoked",
            "user_id": str(target_user_id),
            "message": "That user does not share their online status with you",
        })
        return

    await connection_manager.subscribe_presence(connection_id, target_user_id)
    status = await presence_service.get_status(target_user_id)
    await websocket.send_json({
        "type": "presence_status",
        "user_id": str(target_user_id),
        "status": status["status"],
        "last_seen_at": status["last_seen_at"],
    })


async def _publish_typing(user_id: int, payload: dict, kind: str = "typing") -> None:
    """
    Fully ephemeral - no DB persistence, no ack. Fanned out to the chat like
    any other chat event (new_message, receipts, ...) via the same
    realtime_service/connection_manager pipeline, so it reuses the existing
    per-chat Redis channel instead of a new mechanism. The client is
    responsible for expiring the state on its own (~5s) rather than the
    server ever sending a matching "stopped" event - see CLAUDE.md's
    typing-indicator section.

    Same helper covers both the text-composing ("typing") and the
    voice-recording ("recording_audio") activity kinds - identical
    authorization and fan-out, only the "kind" field differs. The wire
    event stays "typing" so existing clients keep working; a client that
    doesn't understand a kind can treat it as plain typing.
    """
    chat_id = int(payload["chat_id"])

    async with session_scope() as session:
        participants = await get_chat_participants(session, chat_id)
        participant_ids = {p.user_id for p in participants}
        if user_id not in participant_ids:
            raise message_service.NotAParticipantError(f"User {user_id} is not a participant of chat {chat_id}")

        # Privacy (1:1 only): the typing/recording indicator is a presence-like
        # signal, so it follows the sender's `privacy.online` setting. If the
        # sender hides their online status from the other participant, that
        # participant must not receive the sender's typing indicator either.
        # Checked live on every event (unlike the presence gate, which is
        # re-checked on the heartbeat) so a privacy change takes effect at once.
        # Groups have no presence concept - never gated.
        if len(participant_ids) == 2:
            other_user_id = next(uid for uid in participant_ids if uid != user_id)
            if not await _presence_authorized(session, watcher_id=other_user_id, target_user_id=user_id):
                return

    await realtime_service.publish_event(chat_id, {
        "event": "typing",
        "kind": kind,
        "chat_id": str(chat_id),
        "user_id": str(user_id),
    })


# Message type -> handler. Every handler shares the
# (user_id, connection_id, payload, websocket) signature; unknown types are
# handled inline in _dispatch.
_HANDLERS = {
    "heartbeat": _handle_heartbeat,
    "send_message": _handle_send_message,
    "edit_message": _handle_edit_message,
    "delete_message": _handle_delete_message,
    "restore_message": _handle_restore_message,
    "purge_message": _handle_purge_message,
    "mark_delivered": _handle_mark_delivered,
    "mark_read": _handle_mark_read,
    "mark_played": _handle_mark_played,
    "typing": _handle_typing,
    "recording": _handle_recording,
    "subscribe_presence": _handle_subscribe_presence,
    "unsubscribe_presence": _handle_unsubscribe_presence,
}

# message type -> (redis action key, MAX global name, WINDOW global name),
# checked in _dispatch (per user, sliding) before the handler runs. Types
# sharing a key share a bucket (all mark_* together, edit/delete/restore
# together, typing/recording together) so a flood of one can't dodge the limit
# by alternating with a sibling. send_message is NOT here - its handler runs
# its own two-tier check. heartbeat / unsubscribe_presence are intentionally
# unmetered (cheap, self-limiting).
_ACTION_LIMITS = {
    "mark_delivered": ("ws_receipts", "WS_RECEIPTS_RATE_MAX", "WS_RECEIPTS_RATE_WINDOW_SECONDS"),
    "mark_read": ("ws_receipts", "WS_RECEIPTS_RATE_MAX", "WS_RECEIPTS_RATE_WINDOW_SECONDS"),
    "mark_played": ("ws_receipts", "WS_RECEIPTS_RATE_MAX", "WS_RECEIPTS_RATE_WINDOW_SECONDS"),
    "edit_message": ("ws_edit", "WS_EDIT_RATE_MAX", "WS_EDIT_RATE_WINDOW_SECONDS"),
    "delete_message": ("ws_edit", "WS_EDIT_RATE_MAX", "WS_EDIT_RATE_WINDOW_SECONDS"),
    "restore_message": ("ws_edit", "WS_EDIT_RATE_MAX", "WS_EDIT_RATE_WINDOW_SECONDS"),
    "purge_message": ("ws_edit", "WS_EDIT_RATE_MAX", "WS_EDIT_RATE_WINDOW_SECONDS"),
    "typing": ("ws_typing", "WS_TYPING_RATE_MAX", "WS_TYPING_RATE_WINDOW_SECONDS"),
    "recording": ("ws_typing", "WS_TYPING_RATE_MAX", "WS_TYPING_RATE_WINDOW_SECONDS"),
    "subscribe_presence": ("ws_sub_presence", "WS_SUBSCRIBE_PRESENCE_RATE_MAX", "WS_SUBSCRIBE_PRESENCE_RATE_WINDOW_SECONDS"),
}
