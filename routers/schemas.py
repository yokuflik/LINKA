from datetime import datetime
from typing import Annotated, Optional

from pydantic import BaseModel, BeforeValidator, ConfigDict, field_validator, model_validator

from config import ALLOWED_UPLOAD_MIME, MAX_UPLOAD_BYTES_BY_KIND, MIN_UPLOAD_BYTES_BY_KIND
from services.storage.media_service import public_avatar_url

# Every id in this codebase is a 64-bit Snowflake. JavaScript's JSON.parse
# (and fetch().json(), and JSON.parse on a WebSocket message) decodes JSON
# numbers as IEEE-754 doubles, which only represent integers exactly up to
# 2^53-1 - about 9 quadrillion. Our ids are ~3.5 * 10^17, so roughly 95% of
# them get silently corrupted the instant a browser parses one (confirmed:
# this is exactly what broke chat creation - a corrupted other_user_id
# pointed at a nonexistent user, and the participant insert failed silently).
# Emitting ids as JSON strings instead means the client never runs them
# through Number at all - request bodies still parse a numeric string back
# to an exact int with no special handling needed (Pydantic does that natively).
IdStr = Annotated[str, BeforeValidator(str)]


class OTPRequestIn(BaseModel):
    phone_number: str
    # 'login' | 'register' - lets the server reject "register an existing
    # number" / "log in with an unknown number" before an OTP is even sent.
    # Optional so existing callers keep working (no intent = no pre-check).
    intent: str | None = None


class OTPVerifyIn(BaseModel):
    phone_number: str
    code: str


class RefreshTokenIn(BaseModel):
    refresh_token: str


class TokenPairOut(BaseModel):
    access_token: str
    refresh_token: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: IdStr
    phone_number: str
    display_name: Optional[str]
    about_text: Optional[str]
    profile_pic_url: Optional[str]

    @model_validator(mode="after")
    def _resolve_avatar_url(self):
        # profile_pic_url is stored as a storage key; expose it as a public
        # URL. Values that are already absolute URLs (legacy / seed data)
        # pass through untouched.
        key = self.profile_pic_url
        if key and not key.startswith(("http://", "https://")):
            self.profile_pic_url = public_avatar_url(key)
        return self


class LoginOut(BaseModel):
    user: UserOut
    access_token: str
    refresh_token: str


class UserProfileUpdateIn(BaseModel):
    display_name: Optional[str] = None
    about_text: Optional[str] = None
    # The avatar is set through the dedicated /users/me/avatar endpoints, not
    # here - a raw client-supplied URL/key can't be trusted or cleaned up.


class AvatarUploadTicketIn(BaseModel):
    mime_type: str
    size_bytes: int

    @field_validator("mime_type")
    @classmethod
    def _mime_allowed(cls, v: str) -> str:
        if v not in ALLOWED_UPLOAD_MIME["avatar"]:
            raise ValueError(f"content type {v!r} is not allowed for profile pictures")
        return v

    @field_validator("size_bytes")
    @classmethod
    def _size_in_range(cls, v: int) -> int:
        ceiling = MAX_UPLOAD_BYTES_BY_KIND["avatar"]
        floor = MIN_UPLOAD_BYTES_BY_KIND.get("avatar", 1)
        if v < floor:
            raise ValueError(f"declared size {v} is below the {floor}-byte minimum")
        if v > ceiling:
            raise ValueError(
                f"profile picture must be at most {ceiling} bytes ({ceiling // 1024} KB)"
            )
        return v


class AvatarUploadTicketOut(BaseModel):
    storage_key: str
    upload_url: str
    required_headers: dict
    expires_in: int


class AvatarCommitIn(BaseModel):
    storage_key: str


class ChatOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: IdStr
    is_group: bool
    title: Optional[str]
    about_text: Optional[str]
    profile_pic_url: Optional[str]
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

    # MessageStatus for last_message_id (1=sent, 2=delivered, 3=read - see
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


class ChatMemberOut(BaseModel):
    user: UserOut
    role: int


class ParticipantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    chat_id: IdStr
    user_id: IdStr
    role: int


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: IdStr
    chat_id: IdStr
    sender_id: Optional[IdStr]
    type: int
    content: Optional[str]
    reply_to_message_id: Optional[IdStr]
    is_edited: bool
    edited_at: Optional[datetime]
    deleted_at: Optional[datetime]
    created_at: datetime

    # MessageStatus (1=sent, 2=delivered, 3=read - see database/models/
    # message.py), always attached by message_service.get_message_history
    # before this model is built. Only meaningful for a message the
    # requesting user themselves sent - same as WhatsApp, a client should
    # only render the check marks on its own outgoing messages.
    status: int
