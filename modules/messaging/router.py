from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from sqlalchemy.ext.asyncio import AsyncSession

from config import (
    DETAIL_READ_RATE_MAX,
    DETAIL_READ_RATE_WINDOW_SECONDS,
    MSG_HISTORY_MAX_LIMIT,
    MSG_HISTORY_RATE_MAX,
    MSG_HISTORY_RATE_WINDOW_SECONDS,
    UPLOAD_TICKET_IP_RATE_LIMIT_MAX,
    UPLOAD_TICKET_IP_RATE_LIMIT_WINDOW_SECONDS,
    UPLOAD_TICKET_RATE_MAX,
    UPLOAD_TICKET_RATE_WINDOW_SECONDS,
    STORAGE_QUOTA_BYTES,
)
from infra.db.connection import get_db
from modules.media.crud import get_blob_by_hash
from modules.media.crud import reserve_blob
from modules.media.errors import StorageQuotaExceededError
from modules.users.crud import get_storage_bytes_used
from modules.chats.crud.crud_participant import is_participant
from api.dependencies import get_current_user_id
from api.schemas import MediaUploadTicketIn
from api.schemas import MediaUploadTicketOut
from api.schemas import MessageOut
from api.schemas import MessageReceiptsOut
from modules.messaging import service as message_service
from infra.ratelimit import service as rate_limit_service
from infra.ratelimit.service import RateLimited
from modules.media import media_service

router = APIRouter(prefix="/chats/{chat_id}/messages", tags=["messages"])


@router.get("", response_model=list[MessageOut])
async def get_message_history(
    chat_id: int,
    before_id: Optional[int] = None,
    limit: int = 50,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    await rate_limit_service.enforce_sliding_window(
        user_id, "msg_history", MSG_HISTORY_RATE_MAX, MSG_HISTORY_RATE_WINDOW_SECONDS
    )
    # Clamp pagination size server-side so a client can't ask for the whole chat.
    limit = max(1, min(limit, MSG_HISTORY_MAX_LIMIT))
    return await message_service.get_message_history(session, user_id=user_id, chat_id=chat_id, before_id=before_id, limit=limit)


@router.get("/{message_id}/receipts", response_model=MessageReceiptsOut)
async def get_message_receipts(
    chat_id: int,
    message_id: int,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """
    Per-message "info": when each participant received / read / played this
    message, and (in a group at or below RECEIPT_NAMED_LIST_MAX_MEMBERS
    members) who has. Any participant may view it for any message.
    """
    await rate_limit_service.enforce_sliding_window(
        user_id, "detail_read", DETAIL_READ_RATE_MAX, DETAIL_READ_RATE_WINDOW_SECONDS
    )
    try:
        return await message_service.get_message_receipts(
            session, user_id=user_id, chat_id=chat_id, message_id=message_id
        )
    except message_service.MessageNotFoundError:
        raise HTTPException(status_code=404, detail="Message not found")


@router.post("/upload-ticket", response_model=MediaUploadTicketOut)
async def create_media_upload_ticket(
    chat_id: int,
    body: MediaUploadTicketIn,
    request: Request,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """
    Presigned PUT for a message attachment. The client uploads its bytes
    straight to storage with this, then sends a media message over the
    WebSocket carrying the returned storage_key. Restricted to participants
    so a stranger can't mint upload URLs against a chat.
    """
    await rate_limit_service.enforce_sliding_window(
        user_id, "upload_ticket", UPLOAD_TICKET_RATE_MAX, UPLOAD_TICKET_RATE_WINDOW_SECONDS
    )
    ip = rate_limit_service.client_ip(request)
    if not await rate_limit_service.check_and_increment(
        ip, "upload_ticket_ip", max_per_window=UPLOAD_TICKET_IP_RATE_LIMIT_MAX,
        window_seconds=UPLOAD_TICKET_IP_RATE_LIMIT_WINDOW_SECONDS,
    ):
        raise RateLimited("upload_ticket_ip", retry_after=UPLOAD_TICKET_IP_RATE_LIMIT_WINDOW_SECONDS)

    if not await is_participant(session, chat_id, user_id):
        raise message_service.NotAParticipantError(
            f"User {user_id} is not a participant of chat {chat_id}"
        )

    # Per-user hard storage quota (ADR 0028): checked before the dedup branch -
    # a deduped send still becomes a ref this user holds, so it still counts.
    used = await get_storage_bytes_used(session, user_id)
    if used + body.size_bytes > STORAGE_QUOTA_BYTES:
        raise StorageQuotaExceededError(
            "storage quota exceeded - delete some files to upload more"
        )

    # Content-addressed dedup (ADR 0010): if this exact file was already
    # uploaded and confirmed, hand the client the existing key and skip the
    # upload entirely. Otherwise reserve a blob row and mint a presigned PUT.
    existing = await get_blob_by_hash(session, body.sha256)
    already_uploaded = existing is not None and existing.uploaded_at is not None

    ticket = media_service.build_media_upload_ticket(
        body.kind, body.mime_type, body.size_bytes, body.sha256,
        already_uploaded=already_uploaded,
    )
    if not already_uploaded:
        await reserve_blob(
            session,
            sha256=body.sha256,
            storage_key=ticket.storage_key,
            bucket=ticket.bucket,
            kind=body.kind,
            mime=body.mime_type,
            size=body.size_bytes,
        )
    return MediaUploadTicketOut(
        storage_key=ticket.storage_key,
        already_uploaded=ticket.already_uploaded,
        upload_url=ticket.upload_url,
        required_headers=ticket.required_headers,
        expires_in=ticket.expires_in,
    )
