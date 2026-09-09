"""Media / avatar upload-ticket API models (ADR 0030).

Shared by the users and chats routers (avatars) and the messaging router
(message media), so they live in this leaf package rather than any one feature.
"""

from typing import Optional

from pydantic import BaseModel, field_validator, model_validator

from config import settings


class AvatarUploadTicketIn(BaseModel):
    mime_type: str
    size_bytes: int

    @field_validator("mime_type")
    @classmethod
    def _mime_allowed(cls, v: str) -> str:
        if v not in settings.ALLOWED_UPLOAD_MIME["avatar"]:
            raise ValueError(f"content type {v!r} is not allowed for profile pictures")
        return v

    @field_validator("size_bytes")
    @classmethod
    def _size_in_range(cls, v: int) -> int:
        ceiling = settings.MAX_UPLOAD_BYTES_BY_KIND["avatar"]
        floor = settings.MIN_UPLOAD_BYTES_BY_KIND.get("avatar", 1)
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
    # Optional inline avatar thumbnail (a ~64px JPEG data: URI) computed by the
    # uploader's browser - ADR 0016. Validated and dropped-if-bad in avatar_service.
    preview: Optional[str] = None


class MediaUploadTicketIn(BaseModel):
    # 'image' | 'video' | 'audio' | 'file'
    kind: str
    mime_type: str
    size_bytes: int
    # sha256 of the raw file bytes (64 lowercase hex), computed client-side.
    # Drives content-addressed dedup - see ADR 0010.
    sha256: str

    @field_validator("sha256")
    @classmethod
    def _sha256_hex(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if len(v) != 64 or any(c not in "0123456789abcdef" for c in v):
            raise ValueError("sha256 must be 64 lowercase hex characters")
        return v

    @model_validator(mode="after")
    def _validate(self):
        kind = self.kind
        if kind not in ("image", "video", "audio", "file"):
            raise ValueError(f"unknown media kind {kind!r}")
        allowed = settings.ALLOWED_UPLOAD_MIME.get(kind, set())
        if not self.mime_type:
            raise ValueError(f"a content type is required for {kind!r}")
        # An empty allow-set is the "any non-empty MIME" sentinel (kind 'file').
        if allowed and self.mime_type not in allowed:
            raise ValueError(f"content type {self.mime_type!r} is not allowed for {kind!r}")
        ceiling = settings.MAX_UPLOAD_BYTES_BY_KIND[kind]
        floor = settings.MIN_UPLOAD_BYTES_BY_KIND.get(kind, 1)
        if self.size_bytes < floor:
            raise ValueError(f"declared size {self.size_bytes} is below the {floor}-byte minimum")
        if self.size_bytes > ceiling:
            raise ValueError(
                f"declared size {self.size_bytes} exceeds the {ceiling}-byte limit for {kind!r}"
            )
        return self


class MediaUploadTicketOut(BaseModel):
    storage_key: str
    # True => the bytes are already in storage (a previous upload of the same
    # file); the client skips the PUT and sends the message straight away.
    # upload_url / required_headers are then empty. See ADR 0010.
    already_uploaded: bool = False
    upload_url: str = ""
    required_headers: dict = {}
    expires_in: int = 0


__all__ = [
    "AvatarUploadTicketIn",
    "AvatarUploadTicketOut",
    "AvatarCommitIn",
    "MediaUploadTicketIn",
    "MediaUploadTicketOut",
]
