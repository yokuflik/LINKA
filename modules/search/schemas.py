"""Search API models (ADR 0030 / ADR 0040)."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict

from api.schemas import IdStr


class SearchResultOut(BaseModel):
    """One message that matched the query.

    Deliberately lighter than `MessageOut`: no derived tick `status` (it would
    need a per-chat fetch per hit and is meaningless in a results list) and no
    presigned `media_url` (the bytes load via the normal history path when the
    user jumps to context). `media_blur_hash` is included so a media hit can
    still render a placeholder.
    """
    model_config = ConfigDict(from_attributes=True)

    id: IdStr
    chat_id: IdStr
    sender_id: Optional[IdStr]
    type: int
    content: Optional[str]
    # Content window around the first match, computed app-side.
    snippet: Optional[str] = None
    reply_to_message_id: Optional[IdStr]

    media_mime: Optional[str] = None
    media_size: Optional[int] = None
    media_name: Optional[str] = None
    media_duration_seconds: Optional[int] = None
    media_blur_hash: Optional[str] = None

    is_edited: bool
    edited_at: Optional[datetime]
    deleted_at: Optional[datetime]
    created_at: datetime


class SearchResponseOut(BaseModel):
    results: list[SearchResultOut]
    # Opaque; pass back as `cursor` (global) / `before_id` (in-chat) for the
    # next page. None when this was the last page.
    next_cursor: Optional[str] = None
    has_more: bool = False


__all__ = ["SearchResultOut", "SearchResponseOut"]
