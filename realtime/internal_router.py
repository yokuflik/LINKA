"""
Internal-only endpoints consumed by the Rust `ws_gateway` (ADR 0033 / 0036).

NEVER routed from the edge: Caddy returns 404 for `/internal*` (see
`.claude_docs/deployment.md`). The JWT is still verified here so the endpoint is
not an unauthenticated data tap even on the internal network.
"""
import logging

import jwt
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from config import WS_MAX_CHAT_IDS_ON_CONNECT
from infra.db.connection import session_scope
from modules.auth import service as auth_service
from modules.chats.crud.crud_participant import get_all_chat_ids_for_user
from modules.chats.crud.crud_participant import get_chat_participants
from modules.messaging import service as message_service
from modules.messaging.errors import MessageTooLongError
from modules.messaging.errors import NotAParticipantError
from realtime.presence_authz import presence_authorized

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])


@router.get("/ws-bootstrap")
async def ws_bootstrap(token: str = Query(...)) -> dict:
    """
    Everything the gateway needs to populate its local routing table on connect:
    the caller's user id and the chat ids it belongs to. Mirrors exactly what
    the Python `/ws` endpoint resolves via `get_all_chat_ids_for_user`.
    """
    try:
        user_id = auth_service.verify_access_token(token)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="invalid token")

    async with session_scope() as session:
        chat_ids = list(
            await get_all_chat_ids_for_user(session, user_id, limit=WS_MAX_CHAT_IDS_ON_CONNECT)
        )

    return {"user_id": str(user_id), "chat_ids": [str(c) for c in chat_ids]}


@router.get("/presence-authorized")
async def presence_authorized_check(watcher_id: int = Query(...), target_user_id: int = Query(...)) -> dict:
    """Whether `watcher_id` may see `target_user_id`'s presence (`privacy.online`)."""
    async with session_scope() as session:
        allowed = await presence_authorized(session, watcher_id, target_user_id)
    return {"authorized": allowed}


@router.get("/typing-allowed")
async def typing_allowed(chat_id: int = Query(...), sender_id: int = Query(...)) -> dict:
    """
    The full server-side gate for a `typing`/`recording` event: the sender must
    be a participant, and in a 1:1 chat the sender's `privacy.online` must let
    the other participant see them (mirrors `_publish_typing`).
    """
    async with session_scope() as session:
        participants = await get_chat_participants(session, chat_id)
        participant_ids = {p.user_id for p in participants}
        if sender_id not in participant_ids:
            return {"allowed": False}
        if len(participant_ids) == 2:
            other_id = next(uid for uid in participant_ids if uid != sender_id)
            if not await presence_authorized(session, watcher_id=other_id, target_user_id=sender_id):
                return {"allowed": False}
    return {"allowed": True}


# --- Message mutations (ADR 0038) — WS-only actions the gateway relays here ---
# The service layer does its own fan-out (`message_edited` / `_deleted` /
# `_restored` / `_purged` via `publish_event`). These endpoints only run the
# op and map the domain errors to HTTP so the gateway can turn them into the
# WS error frame the client expects.

class _MessageOp(BaseModel):
    user_id: int
    chat_id: int
    message_id: int


class _EditOp(_MessageOp):
    content: str


def _op_http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, NotAParticipantError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, MessageTooLongError):
        return HTTPException(status_code=400, detail=str(exc))
    raise exc  # unexpected -> 500 via the app's generic handler


@router.post("/message/edit")
async def message_edit(body: _EditOp) -> dict:
    try:
        async with session_scope() as session:
            message = await message_service.edit_message(
                session,
                user_id=body.user_id,
                chat_id=body.chat_id,
                message_id=body.message_id,
                new_content=body.content,
            )
    except Exception as exc:
        raise _op_http_error(exc)
    return {
        "message_id": str(message.id),
        "edited_at": message.edited_at.isoformat() if message and message.edited_at else None,
    }


@router.post("/message/delete")
async def message_delete(body: _MessageOp) -> dict:
    try:
        async with session_scope() as session:
            deleted = await message_service.delete_message(
                session, user_id=body.user_id, chat_id=body.chat_id, message_id=body.message_id
            )
    except Exception as exc:
        raise _op_http_error(exc)
    return {"deleted": deleted}


@router.post("/message/restore")
async def message_restore(body: _MessageOp) -> dict:
    try:
        async with session_scope() as session:
            message = await message_service.restore_message(
                session, user_id=body.user_id, chat_id=body.chat_id, message_id=body.message_id
            )
    except Exception as exc:
        raise _op_http_error(exc)
    return {"restored": message is not None}


@router.post("/message/purge")
async def message_purge(body: _MessageOp) -> dict:
    try:
        async with session_scope() as session:
            purged = await message_service.purge_message(
                session, user_id=body.user_id, chat_id=body.chat_id, message_id=body.message_id
            )
    except Exception as exc:
        raise _op_http_error(exc)
    return {"purged": purged}
