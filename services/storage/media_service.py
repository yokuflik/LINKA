"""
Public object-storage API: upload tickets, download URLs, object checks.

This is the only module routers / other services import from
``services.storage``. It never streams bytes - it mints presigned URLs the
client uses to talk to storage directly, and reads/deletes object metadata.

Sync vs async (see client.py):
  * ``create_upload_ticket`` / ``download_url`` / ``public_avatar_url`` are
    synchronous - pure local signing / string building, no network.
  * ``object_metadata`` / ``object_exists`` / ``delete_object`` /
    ``ensure_buckets`` do network I/O and are async.
"""

import json
from dataclasses import dataclass

from botocore.exceptions import ClientError

from config import (
    ALLOWED_UPLOAD_MIME,
    DOWNLOAD_URL_EXPIRY_SECONDS,
    MAX_UPLOAD_BYTES_BY_KIND,
    MIN_UPLOAD_BYTES_BY_KIND,
    S3_AVATARS_PUBLIC_BASE_URL,
    S3_BUCKET_AVATARS,
    S3_BUCKET_MEDIA,
    UPLOAD_BUCKET_BY_KIND,
    UPLOAD_URL_EXPIRY_SECONDS,
)
from services.storage.client import (
    async_session,
    build_object_key,
    client_kwargs,
    signing_client,
)
from services.storage.errors import (
    MediaNotFoundError,
    MediaValidationError,
    StorageUnavailableError,
)

VALID_KINDS = frozenset(MAX_UPLOAD_BYTES_BY_KIND.keys())


@dataclass(frozen=True)
class UploadTicket:
    """
    Everything the client needs to upload one object directly to storage.

    ``upload_url`` is a presigned PUT. ``required_headers`` MUST be sent on
    that PUT exactly as given - ``Content-Type`` and ``Content-Length`` are
    baked into the signature, so storage itself rejects a request that
    declares a different type or a larger size than was authorised here.
    ``storage_key`` is what the client hands back later when it sends the
    message (or sets the avatar).
    """

    storage_key: str
    bucket: str
    upload_url: str
    required_headers: dict
    expires_in: int


@dataclass(frozen=True)
class ObjectMetadata:
    """Result of a HEAD against a stored object - the authoritative type/size."""

    key: str
    content_type: str
    size: int


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
def _validate_upload_request(kind: str, mime: str, size_bytes: int) -> None:
    if kind not in VALID_KINDS:
        raise MediaValidationError(
            f"unknown upload kind {kind!r}; expected one of {sorted(VALID_KINDS)}"
        )
    allowed = ALLOWED_UPLOAD_MIME.get(kind, set())
    # An empty allow-set is the "any non-empty MIME" sentinel (kind 'file').
    if not mime:
        raise MediaValidationError(f"a content type is required for {kind!r} uploads")
    if allowed and mime not in allowed:
        raise MediaValidationError(
            f"content type {mime!r} is not allowed for {kind!r} uploads"
        )
    if size_bytes <= 0:
        raise MediaValidationError("declared upload size must be positive")
    floor = MIN_UPLOAD_BYTES_BY_KIND.get(kind, 1)
    if size_bytes < floor:
        raise MediaValidationError(
            f"declared size {size_bytes} is below the {floor}-byte minimum for {kind!r}"
        )
    ceiling = MAX_UPLOAD_BYTES_BY_KIND[kind]
    if size_bytes > ceiling:
        raise MediaValidationError(
            f"declared size {size_bytes} exceeds the {ceiling}-byte limit for {kind!r}"
        )


# --------------------------------------------------------------------------
# Upload tickets (sync - local signing only)
# --------------------------------------------------------------------------
def create_upload_ticket(kind: str, mime: str, size_bytes: int) -> UploadTicket:
    """
    Validate an upload request and return a presigned PUT for it.

    Raises MediaValidationError (-> HTTP 400) for a bad kind / disallowed
    MIME / oversize declaration, before any URL is generated.
    """
    _validate_upload_request(kind, mime, size_bytes)

    bucket = UPLOAD_BUCKET_BY_KIND[kind]
    key = build_object_key(kind, mime)

    # ContentType and ContentLength in Params are signed into the URL: the
    # PUT must carry matching headers or storage returns 403. This is the
    # real server-side enforcement of the declared size / type.
    url = signing_client().generate_presigned_url(
        "put_object",
        Params={
            "Bucket": bucket,
            "Key": key,
            "ContentType": mime,
            "ContentLength": size_bytes,
        },
        ExpiresIn=UPLOAD_URL_EXPIRY_SECONDS,
    )
    return UploadTicket(
        storage_key=key,
        bucket=bucket,
        upload_url=url,
        required_headers={"Content-Type": mime, "Content-Length": str(size_bytes)},
        expires_in=UPLOAD_URL_EXPIRY_SECONDS,
    )


# --------------------------------------------------------------------------
# Download URLs (sync - local signing / string building only)
# --------------------------------------------------------------------------
def download_url(storage_key: str, bucket: str = S3_BUCKET_MEDIA) -> str:
    """
    Presigned GET for a private media object. Short-lived (see
    DOWNLOAD_URL_EXPIRY_SECONDS). Callers that serve many of these (message
    history) should cache the result per key for its TTL rather than
    re-signing per request - see the spec's read-path task.
    """
    return signing_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": storage_key},
        ExpiresIn=DOWNLOAD_URL_EXPIRY_SECONDS,
    )


def message_media_download_url(storage_key: str | None) -> str | None:
    """
    Presigned GET for a media-message attachment (private media bucket).
    Returns None for a message with no attachment so callers can pass
    ``message.media_key`` straight through.
    """
    if not storage_key:
        return None
    return download_url(storage_key, bucket=S3_BUCKET_MEDIA)


def public_avatar_url(storage_key: str | None) -> str | None:
    """
    Public URL for an avatar object. No signing - the avatars bucket is
    public-read (CDN-fronted in prod). Returns None for a missing key so
    callers can pass ``user.profile_pic_url`` straight through.
    """
    if not storage_key:
        return None
    return f"{S3_AVATARS_PUBLIC_BASE_URL.rstrip('/')}/{storage_key.lstrip('/')}"


# --------------------------------------------------------------------------
# Object metadata / lifecycle (async - real network I/O)
# --------------------------------------------------------------------------
async def object_metadata(storage_key: str, bucket: str = S3_BUCKET_MEDIA) -> ObjectMetadata:
    """
    HEAD an object and return its authoritative content-type and size.

    Raises MediaNotFoundError if the object isn't there (e.g. a message
    references a key that was never actually uploaded).

    NOT for the message hot path - see the async-verification task in the
    spec. Call this from a worker / an out-of-band check, not from
    send_message.
    """
    session = async_session()
    try:
        async with session.client("s3", **client_kwargs()) as s3:
            head = await s3.head_object(Bucket=bucket, Key=storage_key)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            raise MediaNotFoundError(f"no object at {bucket}/{storage_key}") from exc
        raise StorageUnavailableError(str(exc)) from exc
    return ObjectMetadata(
        key=storage_key,
        content_type=head.get("ContentType", "application/octet-stream"),
        size=int(head.get("ContentLength", 0)),
    )


async def object_exists(storage_key: str, bucket: str = S3_BUCKET_MEDIA) -> bool:
    try:
        await object_metadata(storage_key, bucket)
        return True
    except MediaNotFoundError:
        return False


async def delete_object(storage_key: str, bucket: str = S3_BUCKET_MEDIA) -> None:
    """Delete one object. Used for avatar replacement cleanup. Idempotent."""
    session = async_session()
    try:
        async with session.client("s3", **client_kwargs()) as s3:
            await s3.delete_object(Bucket=bucket, Key=storage_key)
    except ClientError as exc:
        raise StorageUnavailableError(str(exc)) from exc


# --------------------------------------------------------------------------
# Bucket bootstrap (async - dev convenience, called from app lifespan)
# --------------------------------------------------------------------------
_AVATARS_PUBLIC_READ_POLICY = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {"AWS": ["*"]},
            "Action": ["s3:GetObject"],
            "Resource": [f"arn:aws:s3:::{S3_BUCKET_AVATARS}/*"],
        }
    ],
}


async def ensure_buckets() -> None:
    """
    Create the media and avatars buckets if missing and make the avatars
    bucket public-read. Dev / local convenience only - production buckets are
    provisioned by infrastructure-as-code, and this is a no-op against them
    (the buckets already exist, the policy already set).
    """
    session = async_session()
    async with session.client("s3", **client_kwargs()) as s3:
        for bucket in (S3_BUCKET_MEDIA, S3_BUCKET_AVATARS):
            try:
                await s3.head_bucket(Bucket=bucket)
            except ClientError:
                try:
                    await s3.create_bucket(Bucket=bucket)
                except ClientError as exc:
                    code = exc.response.get("Error", {}).get("Code", "")
                    if code not in ("BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                        raise StorageUnavailableError(
                            f"could not create bucket {bucket}: {exc}"
                        ) from exc

        try:
            await s3.put_bucket_policy(
                Bucket=S3_BUCKET_AVATARS,
                Policy=json.dumps(_AVATARS_PUBLIC_READ_POLICY),
            )
        except ClientError as exc:
            raise StorageUnavailableError(
                f"could not set public-read policy on {S3_BUCKET_AVATARS}: {exc}"
            ) from exc
