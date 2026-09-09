import os

# --- Object storage (message attachments + avatars) ---
# The app server never handles file bytes: clients upload/download directly
# against this storage using short-lived presigned URLs. In dev this points
# at the local MinIO container (docker-compose `test_minio`); in production
# S3_ENDPOINT_URL is left unset so boto3 talks to real AWS S3.
S3_ENDPOINT_URL = os.environ.get("S3_ENDPOINT_URL", "http://localhost:9100")
S3_REGION = os.environ.get("S3_REGION", "us-east-1")
S3_ACCESS_KEY = os.environ.get("S3_ACCESS_KEY", "linka_dev")
S3_SECRET_KEY = os.environ.get("S3_SECRET_KEY", "linka_dev_secret")

# Two buckets, different visibility: media is private (presigned GET only),
# avatars is public-read (fronted by a CDN in prod, served straight from
# MinIO in dev).
S3_BUCKET_MEDIA = os.environ.get("S3_BUCKET_MEDIA", "linka-media")
S3_BUCKET_AVATARS = os.environ.get("S3_BUCKET_AVATARS", "linka-avatars")

# Base URL the client uses to GET a public avatar object. In dev that's the
# MinIO endpoint + bucket; in prod it's the CDN distribution domain.
S3_AVATARS_PUBLIC_BASE_URL = os.environ.get(
    "S3_AVATARS_PUBLIC_BASE_URL", f"{S3_ENDPOINT_URL}/{S3_BUCKET_AVATARS}"
)

# Presigned URL lifetimes. Upload is short (the client PUTs immediately);
# download is longer so an open chat keeps working without re-signing every
# item on every scroll.
UPLOAD_URL_EXPIRY_SECONDS = int(os.environ.get("UPLOAD_URL_EXPIRY_SECONDS", "900"))

# Content-addressed media dedup (ADR 0010). When true, the client-supplied
# sha256 is pinned into the presigned PUT as x-amz-checksum-sha256, so storage
# itself rejects a body whose bytes don't match the claimed hash. Disable only
# against a storage backend that doesn't support SHA-256 checksums - integrity
# then falls back to the size check done via HEAD at send time.
S3_ENFORCE_UPLOAD_CHECKSUM = os.environ.get(
    "S3_ENFORCE_UPLOAD_CHECKSUM", "true"
).lower() in ("1", "true", "yes")
DOWNLOAD_URL_EXPIRY_SECONDS = int(os.environ.get("DOWNLOAD_URL_EXPIRY_SECONDS", "3600"))

# Per-kind upload size ceilings, in bytes. These are pinned into the
# presigned PUT signature (Content-Length), so storage itself rejects an
# upload that exceeds what the client declared - not just an app-layer check.
MAX_UPLOAD_BYTES_IMAGE = int(os.environ.get("MAX_UPLOAD_BYTES_IMAGE", str(5 * 1024 * 1024)))
MAX_UPLOAD_BYTES_VIDEO = int(os.environ.get("MAX_UPLOAD_BYTES_VIDEO", str(20 * 1024 * 1024)))
MAX_UPLOAD_BYTES_AUDIO = int(os.environ.get("MAX_UPLOAD_BYTES_AUDIO", str(5 * 1024 * 1024)))
MAX_UPLOAD_BYTES_FILE = int(os.environ.get("MAX_UPLOAD_BYTES_FILE", str(20 * 1024 * 1024)))
# Profile pictures (user + group avatars): 0.5 MB.
MAX_UPLOAD_BYTES_AVATAR = int(os.environ.get("MAX_UPLOAD_BYTES_AVATAR", str(512 * 1024)))

# Allowed upload content types, per kind. A ticket request for a kind with a
# mime outside its set is rejected before any URL is minted. Kept
# deliberately narrow for launch - widen via env / this list, not code.
ALLOWED_UPLOAD_MIME = {
    "image": {"image/jpeg", "image/png", "image/webp", "image/gif"},
    "video": {"video/mp4", "video/webm", "video/quicktime"},
    "audio": {"audio/mpeg", "audio/ogg", "audio/mp4", "audio/webm", "audio/aac"},
    # Documents: any content type. A generic file attachment can be anything
    # the user has on disk; images/video/audio stay locked to their own kinds
    # above (those render inline and must be a known format). An empty set is
    # the "allow any non-empty MIME" sentinel - see _validate_upload_request /
    # message_service._validate_media.
    "file": set(),
    # Avatars must be images; reuse the image set.
    "avatar": {"image/jpeg", "image/png", "image/webp"},
}

# Maps an upload kind to its size ceiling. media_service reads this rather
# than branching on kind in several places.
MAX_UPLOAD_BYTES_BY_KIND = {
    "image": MAX_UPLOAD_BYTES_IMAGE,
    "video": MAX_UPLOAD_BYTES_VIDEO,
    "audio": MAX_UPLOAD_BYTES_AUDIO,
    "file": MAX_UPLOAD_BYTES_FILE,
    "avatar": MAX_UPLOAD_BYTES_AVATAR,
}

# Smallest accepted upload per kind, in bytes. Guards against zero-byte /
# truncated uploads and obviously-bogus tickets. Enforced alongside the
# ceiling in media_service._validate_upload_request.
MIN_UPLOAD_BYTES_BY_KIND = {
    "image": 1,
    "video": 1,
    "audio": 1,
    "file": 1,
    "avatar": 1,
}

# Per-user hard storage quota, in bytes (ADR 0028). A media upload ticket is
# refused (HTTP 413, reason "storage_quota_exceeded") once the user's running
# total of sent-media sizes plus the new file would exceed this. Counted
# per-ref, not per-object - dedup does not discount it. Freed only by an
# irreversible message purge (ADR 0021), not a soft delete. Default 1 GiB.
STORAGE_QUOTA_BYTES = int(os.environ.get("STORAGE_QUOTA_BYTES", str(1 * 1024**3)))

# --- Message media <-> upload-kind mapping ---
# Message.type integer -> the storage upload kind it corresponds to.
# 2=image, 3=video, 4=audio, 5=file (1=text, 6=system carry no media).
MEDIA_MESSAGE_TYPES = {2, 3, 4, 5}
MEDIA_KIND_BY_MESSAGE_TYPE = {2: "image", 3: "video", 4: "audio", 5: "file"}
MESSAGE_TYPE_BY_MEDIA_KIND = {v: k for k, v in MEDIA_KIND_BY_MESSAGE_TYPE.items()}

# Cap on the client-supplied original filename kept on a media message.
MAX_MEDIA_FILENAME_LENGTH = int(os.environ.get("MAX_MEDIA_FILENAME_LENGTH", "255"))

# Cap on the client-supplied media blur placeholder (ThumbHash, base64) - ADR 0014.
# A real ThumbHash is ~28-44 chars; 64 is generous headroom. Untrusted input.
MAX_MEDIA_BLUR_HASH_LENGTH = int(os.environ.get("MAX_MEDIA_BLUR_HASH_LENGTH", "64"))

# Cap on the client-supplied inline avatar thumbnail (a ~64px JPEG data: URI) -
# ADR 0016. Typically 1-3 KB; 8192 is headroom. Untrusted input.
MAX_AVATAR_PREVIEW_LENGTH = int(os.environ.get("MAX_AVATAR_PREVIEW_LENGTH", "8192"))

# Which bucket each kind lands in.
UPLOAD_BUCKET_BY_KIND = {
    "image": S3_BUCKET_MEDIA,
    "video": S3_BUCKET_MEDIA,
    "audio": S3_BUCKET_MEDIA,
    "file": S3_BUCKET_MEDIA,
    "avatar": S3_BUCKET_AVATARS,
}

__all__ = [
    "S3_ENDPOINT_URL",
    "S3_REGION",
    "S3_ACCESS_KEY",
    "S3_SECRET_KEY",
    "S3_BUCKET_MEDIA",
    "S3_BUCKET_AVATARS",
    "S3_AVATARS_PUBLIC_BASE_URL",
    "UPLOAD_URL_EXPIRY_SECONDS",
    "S3_ENFORCE_UPLOAD_CHECKSUM",
    "STORAGE_QUOTA_BYTES",
    "DOWNLOAD_URL_EXPIRY_SECONDS",
    "MAX_UPLOAD_BYTES_IMAGE",
    "MAX_UPLOAD_BYTES_VIDEO",
    "MAX_UPLOAD_BYTES_AUDIO",
    "MAX_UPLOAD_BYTES_FILE",
    "MAX_UPLOAD_BYTES_AVATAR",
    "ALLOWED_UPLOAD_MIME",
    "MAX_UPLOAD_BYTES_BY_KIND",
    "MIN_UPLOAD_BYTES_BY_KIND",
    "MEDIA_MESSAGE_TYPES",
    "MEDIA_KIND_BY_MESSAGE_TYPE",
    "MESSAGE_TYPE_BY_MEDIA_KIND",
    "MAX_MEDIA_FILENAME_LENGTH",
    "MAX_MEDIA_BLUR_HASH_LENGTH",
    "MAX_AVATAR_PREVIEW_LENGTH",
    "UPLOAD_BUCKET_BY_KIND",
]
