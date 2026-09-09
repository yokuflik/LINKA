"""Chat / group / membership API models (ADR 0030)."""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, model_validator

from api.schemas import IdStr
from modules.media.media_service import public_avatar_url
from modules.users.schemas import UserOut


class ChatOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: IdStr
    is_group: bool
    title: Optional[str]
    about_text: Optional[str]
    profile_pic_url: Optional[str]
    # Inline group-avatar thumbnail data: URI (ADR 0016).
    profile_pic_preview: Optional[str] = None
    last_message_at: datetime

    @model_validator(mode="after")
    def _resolve_avatar_url(self):
        # Same as UserOut: profile_pic_url is stored as an avatars-bucket
        # object key; expose it as a public URL. Absolute URLs (legacy / seed
        # data) pass through untouched.
        key = self.profile_pic_url
        if key and not key.startswith(("http://", "https://")):
            self.profile_pic_url = public_avatar_url(key)
        return self
    last_message_id: Optional[IdStr]
    last_message_preview: Optional[str]

    # MessageStatus for last_message_id (1=sent, 2=delivered, 3=read; 4=played
    # is voice-recording-only and not computed here - see
    # database/models/message.py), for the chat list's own tick next to your
    # last sent message. Only ever set by chat_service.get_chat_list - the
    # create/update endpoints that also return a ChatOut don't have the
    # participant-watermark context to compute it, so it defaults to None
    # there rather than lying with a guessed value.
    last_message_status: Optional[int] = None


class ChatListItemOut(BaseModel):
    chat: ChatOut
    role: int
    last_read_message_id: Optional[IdStr]

    # Whether this viewer has pinned the chat to the top of their list.
    # Per-viewer (lives on Participant), so it belongs here, not on ChatOut.
    # Only set by chat_service.get_chat_list.
    pinned: bool = False

    # Absolute mute expiry for this viewer, or None if not muted. A past
    # value means the mute has lapsed - the client should treat it as
    # un-muted. Only set by chat_service.get_chat_list. See ADR 0004.
    muted_until: Optional[datetime] = None

    # How many of this chat's messages come after the viewer's own
    # last_read_message_id - genuinely per-viewer (unlike ChatOut.
    # last_message_status, which is chat-wide), so it lives here rather than
    # on ChatOut. Only ever set by chat_service.get_chat_list (see its
    # docstring) - not present on the create/update chat endpoints.
    unread_count: int = 0


class CreatePrivateChatIn(BaseModel):
    other_user_id: int


class CreateGroupChatIn(BaseModel):
    title: str
    initial_member_ids: list[int] = []
    about_text: Optional[str] = None
    # Object-storage key of a photo the client already uploaded via
    # POST /chats/{id}/avatar/upload-ticket's presigned PUT. Validated
    # (HEAD + limits) server-side before it's stored - a raw client key is
    # never trusted. Optional; omit for a photo-less group.
    avatar_storage_key: Optional[str] = None
    # Inline thumbnail (a ~64px JPEG data: URI) for the avatar above - ADR 0016.
    avatar_preview: Optional[str] = None


class UpdateGroupDetailsIn(BaseModel):
    title: Optional[str] = None
    about_text: Optional[str] = None
    # The group photo is set through the dedicated
    # /chats/{id}/avatar endpoints, not here - a raw client-supplied key/URL
    # can't be trusted or cleaned up (same rationale as UserProfileUpdateIn).


class AddMemberIn(BaseModel):
    user_id: int


class ChangeRoleIn(BaseModel):
    role: int


class MuteChatIn(BaseModel):
    # Absolute expiry chosen by the client. The client owns the duration
    # menu (8h / 1d / 1w / forever); "forever" is just a far-future
    # timestamp. The server stores this verbatim. See ADR 0004.
    muted_until: datetime


class ChatMemberOut(BaseModel):
    user: UserOut
    role: int


class ParticipantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    chat_id: IdStr
    user_id: IdStr
    role: int


__all__ = [
    "ChatOut",
    "ChatListItemOut",
    "CreatePrivateChatIn",
    "CreateGroupChatIn",
    "UpdateGroupDetailsIn",
    "AddMemberIn",
    "ChangeRoleIn",
    "MuteChatIn",
    "ChatMemberOut",
    "ParticipantOut",
]
