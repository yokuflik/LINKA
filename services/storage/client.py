"""
S3 / MinIO client factories and object-key generation.

Two clients, on purpose:

  * ``signing_client()`` - a plain boto3 client. ``generate_presigned_url`` is
    a *local* operation (HMAC over the request, no network I/O), so running it
    synchronously from an async handler is fine and avoids the overhead of an
    async context per call.

  * ``async_client()`` - an aioboto3 client context manager for every call
    that actually hits the network (``head_object``, ``delete_object``,
    ``copy_object``, bucket creation). A blocking boto3 network call inside the
    event loop stalls every connection on the worker, so those must be async.

Nothing here holds a long-lived network connection; the async client is opened
per operation via ``async with``.
"""

import hashlib
from functools import lru_cache

import aioboto3
import boto3
from botocore.config import Config as BotoConfig

from config import (
    S3_ACCESS_KEY,
    S3_ENDPOINT_URL,
    S3_REGION,
    S3_SECRET_KEY,
)
from utils.snowflake import next_id

# MinIO needs path-style addressing (``endpoint/bucket/key``); the default
# virtual-host style (``bucket.endpoint/key``) doesn't resolve against a local
# container. Harmless against real S3, which accepts both.
_BOTO_CONFIG = BotoConfig(
    signature_version="s3v4",
    s3={"addressing_style": "path"},
    retries={"max_attempts": 3, "mode": "standard"},
)

_COMMON_KWARGS = dict(
    endpoint_url=S3_ENDPOINT_URL or None,
    region_name=S3_REGION,
    aws_access_key_id=S3_ACCESS_KEY,
    aws_secret_access_key=S3_SECRET_KEY,
    config=_BOTO_CONFIG,
)


@lru_cache(maxsize=1)
def signing_client():
    """
    Process-wide boto3 S3 client used only for ``generate_presigned_url``.
    Cached: it's stateless and thread-safe for signing, and creating one per
    request is wasteful.
    """
    return boto3.client("s3", **_COMMON_KWARGS)


def async_session() -> aioboto3.Session:
    """
    An aioboto3 session; callers open a client from it per operation:

        async with async_session().client("s3", **client_kwargs()) as s3:
            await s3.head_object(...)
    """
    return aioboto3.Session()


def client_kwargs() -> dict:
    """kwargs for ``async_session().client("s3", **client_kwargs())``."""
    return dict(_COMMON_KWARGS)


# Extension kept on the key purely so a browser / CDN serves a sensible
# Content-Type on GET and downloads land with a reasonable filename. The
# authoritative type is always re-checked against the stored object's
# metadata, never inferred from this.
_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "audio/webm": ".weba",
    "audio/aac": ".aac",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "application/zip": ".zip",
}


def build_object_key(kind: str, mime: str) -> str:
    """
    Generate a fresh, unguessable object key for an upload.

    Shape: ``{h2}/{kind}/{snowflake}{ext}`` where ``h2`` is the first two hex
    chars of a hash of the id. The high-entropy leading prefix spreads writes
    across storage partitions from day one instead of concentrating them under
    a single dated prefix - which matters once this is real traffic at scale.
    The Snowflake id keeps the key both time-sortable and impossible to guess.
    """
    new_id = next_id()
    h2 = hashlib.sha1(str(new_id).encode()).hexdigest()[:2]
    ext = _MIME_EXT.get(mime, "")
    return f"{h2}/{kind}/{new_id}{ext}"
