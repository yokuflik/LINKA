from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from infra.db.connection import get_db
from modules.messaging.limits import (
    DEFAULT_MESSAGING_LIMITS,
    DEFAULT_SCHEDULED_LIMITS,
    MessagingLimits,
    ScheduledLimits,
)
from modules.media.crud import get_blob_by_hash
from modules.media.crud import reserve_blob
from modules.media.errors import StorageQuotaExceededError
from modules.users.crud import get_storage_bytes_used
from modules.chats.crud.crud_participant import is_participant
from api.dependencies import get_current_user_id
from modules.media.schemas import MediaUploadTicketIn
from modules.media.schemas import MediaUploadTicketOut
from modules.messaging.schemas import MessageOut
from modules.messaging.schemas import MessageReceiptsOut
from modules.messaging.schemas import ScheduledMessageIn
from modules.messaging.schemas import ScheduledMessageOut
from modules.messaging.schemas import ScheduledMessagePatchIn
from modules.messaging import service as message_service
from infra.ratelimit import service as rate_limit_service
from infra.ratelimit.service import RateLimited
from modules.media import media_service

router = APIRouter(prefix="/chats/{chat_id}/messages", tags=["messages"])


def get_messaging_limits() -> MessagingLimits:
    """FastAPI dependency (ADR 0033). Tests override via
    app.dependency_overrides[get_messaging_limits]."""
    return DEFAULT_MESSAGING_LIMITS


def get_scheduled_limits() -> ScheduledLimits:
    """FastAPI dependency (ADR 0033)."""
    return DEFAULT_SCHEDULED_LIMITS


@router.get("", response_model=list[MessageOut])
async def get_message_history(
    chat_id: int,
    before_id: Optional[int] = None,
    limit: int = 50,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
    limits: MessagingLimits = Depends(get_messaging_limits),
):
    await rate_limit_service.enforce_sliding_window(
        user_id, "msg_history", limits.msg_history_rate_max, limits.msg_history_rate_window_s
    )
    # Clamp pagination size server-side so a client can't ask for the whole chat.
    limit = max(1, min(limit, limits.msg_history_max_limit))
    return await message_service.get_message_history(session, user_id=user_id, chat_id=chat_id, before_id=before_id, limit=limit)


@router.get("/{message_id}/receipts", response_model=MessageReceiptsOut)
async def get_message_receipts(
    chat_id: int,
    message_id: int,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
    limits: MessagingLimits = Depends(get_messaging_limits),
):
    """
    Per-message "info": when each participant received / read / played this
    message, and (in a group at or below RECEIPT_NAMED_LIST_MAX_MEMBERS
    members) who has. Any participant may view it for any message.
    """
    await rate_limit_service.enforce_sliding_window(
        user_id, "detail_read", limits.detail_read_rate_max, limits.detail_read_rate_window_s
    )
    try:
        return await message_service.get_message_receipts(
            session, user_id=user_id, chat_id=chat_id, message_id=message_id, limits=limits
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
    limits: MessagingLimits = Depends(get_messaging_limits),
):
    """
    Presigned PUT for a message attachment. The client uploads its bytes
    straight to storage with this, then sends a media message over the
    WebSocket carrying the returned storage_key. Restricted to participants
    so a stranger can't mint upload URLs against a chat.
    """
    await rate_limit_service.enforce_sliding_window(
        user_id, "upload_ticket", limits.upload_ticket_rate_max, limits.upload_ticket_rate_window_s
    )
    ip = rate_limit_service.client_ip(request)
    if not await rate_limit_service.check_and_increment(
        ip, "upload_ticket_ip", max_per_window=limits.upload_ticket_ip_rate_max,
        window_seconds=limits.upload_ticket_ip_rate_window_s,
    ):
        raise RateLimited("upload_ticket_ip", retry_after=limits.upload_ticket_ip_rate_window_s)

    if not await is_participant(session, chat_id, user_id):
        raise message_service.NotAParticipantError(
            f"User {user_id} is not a participant of chat {chat_id}"
        )

    # Per-user hard storage quota (ADR 0028): checked before the dedup branch -
    # a deduped send still becomes a ref this user holds, so it still counts.
    used = await get_storage_bytes_used(session, user_id)
    if used + body.size_bytes > settings.STORAGE_QUOTA_BYTES:
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


# --- Scheduled messages (ADR 0031) ---
# Management state (like chat pin/mute), so REST not WS. Two mount points: the
# create route is chat-scoped, the rest key off the scheduled-message id.

scheduled_create_router = APIRouter(
    prefix="/chats/{chat_id}/scheduled-messages", tags=["scheduled-messages"]
)
scheduled_router = APIRouter(prefix="/scheduled-messages", tags=["scheduled-messages"])


def _scheduled_out(row) -> ScheduledMessageOut:
    """Build the response model, mapping `type` -> `message_type` and attaching
    a presigned media_url (not a stored column)."""
    return ScheduledMessageOut(
        id=row.id,
        chat_id=row.chat_id,
        scheduled_for=row.scheduled_for,
        message_type=row.type,
        content=row.content,
        reply_to_message_id=row.reply_to_message_id,
        status=row.status,
        last_error=row.last_error,
        media_url=media_service.message_media_download_url(row.media_key),
        media_mime=row.media_mime,
        media_size=row.media_size,
        media_name=row.media_name,
        media_duration_seconds=row.media_duration_seconds,
        media_blur_hash=row.media_blur_hash,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _enforce_scheduled_write(user_id: int, limits: ScheduledLimits) -> None:
    await rate_limit_service.enforce_sliding_window(
        user_id, "scheduled_write",
        limits.write_rate_max, limits.write_rate_window_s,
    )


@scheduled_create_router.post("", response_model=ScheduledMessageOut, status_code=201)
async def schedule_message(
    chat_id: int,
    body: ScheduledMessageIn,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
    limits: ScheduledLimits = Depends(get_scheduled_limits),
):
    await _enforce_scheduled_write(user_id, limits)
    row = await message_service.schedule_message(
        session,
        sender_id=user_id,
        chat_id=chat_id,
        client_message_id=body.client_message_id,
        scheduled_for=body.scheduled_for,
        message_type=body.message_type,
        content=body.content,
        media=body.media.model_dump() if body.media else None,
        reply_to_message_id=body.reply_to_message_id,
        limits=limits,
    )
    return _scheduled_out(row)


@scheduled_router.get("", response_model=list[ScheduledMessageOut])
async def list_scheduled_messages(
    chat_id: Optional[int] = None,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    rows = await message_service.list_scheduled(session, sender_id=user_id, chat_id=chat_id)
    return [_scheduled_out(r) for r in rows]


@scheduled_router.patch("/{scheduled_message_id}", response_model=ScheduledMessageOut)
async def patch_scheduled_message(
    scheduled_message_id: int,
    body: ScheduledMessagePatchIn,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
    limits: ScheduledLimits = Depends(get_scheduled_limits),
):
    await _enforce_scheduled_write(user_id, limits)
    set_content = "content" in body.model_fields_set
    row = await message_service.reschedule(
        session,
        sender_id=user_id,
        scheduled_message_id=scheduled_message_id,
        scheduled_for=body.scheduled_for,
        content=body.content,
        set_content=set_content,
        limits=limits,
    )
    return _scheduled_out(row)


@scheduled_router.delete("/{scheduled_message_id}", status_code=204)
async def cancel_scheduled_message(
    scheduled_message_id: int,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
    limits: ScheduledLimits = Depends(get_scheduled_limits),
):
    await _enforce_scheduled_write(user_id, limits)
    await message_service.cancel_scheduled(
        session, sender_id=user_id, scheduled_message_id=scheduled_message_id
    )
