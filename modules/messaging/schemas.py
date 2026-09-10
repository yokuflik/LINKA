"""Message / receipt / scheduled-message API models (ADR 0030)."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict

from api.schemas import IdStr


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: IdStr
    chat_id: IdStr
    sender_id: Optional[IdStr]
    type: int
    content: Optional[str]
    reply_to_message_id: Optional[IdStr]

    # Media attachment (all None for a text / system message). media_url is a
    # short-lived presigned GET attached by message_service (get_message_history
    # / the live new_message event) - it is not a stored column.
    media_url: Optional[str] = None
    media_mime: Optional[str] = None
    media_size: Optional[int] = None
    media_name: Optional[str] = None
    media_duration_seconds: Optional[int] = None
    # Blurred placeholder (ThumbHash, base64) - a real stored column (ADR 0014).
    media_blur_hash: Optional[str] = None

    is_edited: bool
    edited_at: Optional[datetime]
    deleted_at: Optional[datetime]
    created_at: datetime

    # MessageStatus (1=sent, 2=delivered, 3=read, 4=played [voice recordings
    # only]) - see database/models/message.py; always attached by
    # message_service.get_message_history
    # before this model is built. Only meaningful for a message the
    # requesting user themselves sent - same as WhatsApp, a client should
    # only render the check marks on its own outgoing messages.
    status: int


class MessageReceiptEntryOut(BaseModel):
    """One participant's acknowledgement of a message, with its timestamp."""
    user_id: IdStr
    occurred_at: datetime


class MessageReceiptsOut(BaseModel):
    """
    The per-message "info" view (GET /chats/{id}/messages/{mid}/receipts).

    `counts` is always populated (delivered / read / played - the number of
    *other* participants, i.e. excluding the sender, who reached each state).
    The per-member `*_by` lists and `pending` are populated only when
    `truncated` is False - a group larger than
    config.RECEIPT_NAMED_LIST_MAX_MEMBERS returns counts only.

    Timestamps are "when that participant's watermark crossed this message"
    (the same semantics as WhatsApp's read time), accurate to the receipt
    log's batching window.
    """
    chat_id: IdStr
    message_id: IdStr
    is_group: bool
    message_type: int
    # Participants eligible to acknowledge (everyone but the sender).
    participant_count: int
    truncated: bool
    counts: dict[str, int]
    delivered_by: list[MessageReceiptEntryOut] = []
    read_by: list[MessageReceiptEntryOut] = []
    played_by: list[MessageReceiptEntryOut] = []
    # Current participants (excluding sender) with no read row yet.
    pending: list[IdStr] = []


# --- Scheduled messages (ADR 0031) ---

class ScheduledMediaIn(BaseModel):
    # Storage key the client got from an upload ticket and already PUT bytes to.
    key: str
    name: Optional[str] = None
    duration_seconds: Optional[int] = None
    blur_hash: Optional[str] = None


class ScheduledMessageIn(BaseModel):
    # Generated client-side; reused as the send idempotency key when the
    # message fires so a worker retry can't double-send.
    client_message_id: str
    # Absolute UTC instant to deliver at (client converts from local time).
    scheduled_for: datetime
    message_type: int = 1
    content: Optional[str] = None
    media: Optional[ScheduledMediaIn] = None
    reply_to_message_id: Optional[int] = None


class ScheduledMessagePatchIn(BaseModel):
    scheduled_for: Optional[datetime] = None
    # A *sent* `content` key (even null) sets/clears the caption; an absent key
    # leaves it untouched. `model_fields_set` tells the two apart in the router.
    content: Optional[str] = None


class ScheduledMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: IdStr
    chat_id: IdStr
    scheduled_for: datetime
    # 1=text…5=file (mirrors the model's `type` column).
    message_type: int
    content: Optional[str]
    reply_to_message_id: Optional[IdStr]
    # 0=pending, 1=sent, 2=cancelled, 3=failed.
    status: int
    last_error: Optional[str] = None
    # Short-lived presigned GET, attached by the router (not a stored column) so
    # the client can preview a scheduled photo. None for a text message.
    media_url: Optional[str] = None
    media_mime: Optional[str] = None
    media_size: Optional[int] = None
    media_name: Optional[str] = None
    media_duration_seconds: Optional[int] = None
    media_blur_hash: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None


__all__ = [
    "MessageOut",
    "MessageReceiptEntryOut",
    "MessageReceiptsOut",
    "ScheduledMediaIn",
    "ScheduledMessageIn",
    "ScheduledMessagePatchIn",
    "ScheduledMessageOut",
]
