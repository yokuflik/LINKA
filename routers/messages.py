from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from sqlalchemy.ext.asyncio import AsyncSession

from database.connection import get_db
from database.crud.crud_media_blob import get_blob_by_hash, reserve_blob
from database.crud.crud_participant import is_participant
from routers.dependencies import get_current_user_id
from routers.schemas import (
    MediaUploadTicketIn,
    MediaUploadTicketOut,
    MessageOut,
    MessageReceiptsOut,
)
from services import message_service
from services.storage import media_service

router = APIRouter(prefix="/chats/{chat_id}/messages", tags=["messages"])


@router.get("", response_model=list[MessageOut])
async def get_message_history(
    chat_id: int,
    before_id: Optional[int] = None,
    limit: int = 50,
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
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
    user_id: int = Depends(get_current_user_id),
    session: AsyncSession = Depends(get_db),
):
    """
    Presigned PUT for a message attachment. The client uploads its bytes
    straight to storage with this, then sends a media message over the
    WebSocket carrying the returned storage_key. Restricted to participants
    so a stranger can't mint upload URLs against a chat.
    """
    if not await is_participant(session, chat_id, user_id):
        raise message_service.NotAParticipantError(
            f"User {user_id} is not a participant of chat {chat_id}"
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
