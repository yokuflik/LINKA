"""Validation of the media payload for a media-type message."""

import re
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from config import (
    MAX_MEDIA_BLUR_HASH_LENGTH,
    MAX_MEDIA_FILENAME_LENGTH,
    MEDIA_KIND_BY_MESSAGE_TYPE,
    MEDIA_MESSAGE_TYPES,
)

# base64 alphabet only - the client sends a ThumbHash as standard base64.
_BLUR_HASH_RE = re.compile(r"^[A-Za-z0-9+/=]*$")
from database.crud.crud_media_blob import confirm_and_ref, get_blob_by_key
from services.storage import media_service
from services.storage.errors import MediaValidationError


class MediaAttachment:
    """Validated media fields for one outgoing message (see _validate_media)."""

    __slots__ = ("key", "mime", "size", "name", "duration_seconds", "blur_hash")

    def __init__(self, key, mime, size, name, duration_seconds, blur_hash=None):
        self.key = key
        self.mime = mime
        self.size = size
        self.name = name
        self.duration_seconds = duration_seconds
        self.blur_hash = blur_hash


def _clean_blur_hash(raw) -> Optional[str]:
    """Validate the client-supplied ThumbHash placeholder (ADR 0014). A bad
    value is dropped rather than fatal - the placeholder is cosmetic."""
    if raw is None:
        return None
    value = str(raw)
    if not value or len(value) > MAX_MEDIA_BLUR_HASH_LENGTH or not _BLUR_HASH_RE.match(value):
        return None
    return value


async def _validate_media(
    session: AsyncSession, type: int, media: Optional[dict]
) -> Optional[MediaAttachment]:
    """
    Validate the media payload for a media-type message (2=image/3=video/
    4=audio/5=file). Confirms the object actually exists in storage (HEAD)
    and that its real content-type/size are within the per-kind limits -
    a client-declared key is never trusted. The key must also correspond to a
    media_blob row (minted by the upload-ticket endpoint); on success the
    blob's ref_count is bumped and uploaded_at stamped on first use (ADR 0010).
    Returns None for a text/system message. Raises MediaValidationError
    (-> 400) / MediaNotFoundError (-> 404).
    """
    if type not in MEDIA_MESSAGE_TYPES:
        if media:
            raise MediaValidationError("media payload is only valid for a media-type message")
        return None

    if not media or not media.get("key"):
        raise MediaValidationError("a media-type message requires a media.key")

    kind = MEDIA_KIND_BY_MESSAGE_TYPE[type]
    key = str(media["key"])

    # The key must be one we minted an upload ticket for (ADR 0010) - a client
    # can't attach an arbitrary object.
    blob = await get_blob_by_key(session, key)
    if blob is None:
        raise MediaValidationError("unknown media key; request an upload ticket first")

    # Authoritative type/size from storage - not the client's declared values.
    meta = await media_service.object_metadata(key)

    allowed = media_service.ALLOWED_UPLOAD_MIME.get(kind, set())
    # An empty allow-set is the "any non-empty MIME" sentinel (kind 'file').
    if allowed and meta.content_type not in allowed:
        raise MediaValidationError(
            f"stored object type {meta.content_type!r} is not allowed for {kind!r}"
        )
    ceiling = media_service.MAX_UPLOAD_BYTES_BY_KIND[kind]
    if meta.size <= 0 or meta.size > ceiling:
        raise MediaValidationError(
            f"stored object size {meta.size} is outside the limit for {kind!r}"
        )

    name = media.get("name")
    if name is not None:
        name = str(name)[:MAX_MEDIA_FILENAME_LENGTH]

    duration = media.get("duration_seconds")
    duration = int(duration) if duration is not None else None

    blur_hash = _clean_blur_hash(media.get("blur_hash"))

    # Stamp uploaded_at / authoritative mime+size on first confirmed use and
    # bump the reference count. Also persist the blur placeholder on the blob
    # the first time one is seen, and reuse a previously-stored one when this
    # sender didn't compute it (deduped re-send / forward) - ADR 0014.
    if blur_hash is None and blob.blur_hash:
        blur_hash = blob.blur_hash
    await confirm_and_ref(
        session, storage_key=key, mime=meta.content_type, size=meta.size, blur_hash=blur_hash
    )

    return MediaAttachment(
        key=key, mime=meta.content_type, size=meta.size,
        name=name, duration_seconds=duration, blur_hash=blur_hash,
    )
