import os
import random
import uuid

# --- JWT / Auth ---
JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "dev-secret-change-me")
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.environ.get("ACCESS_TOKEN_EXPIRE_MINUTES", "15"))
REFRESH_TOKEN_EXPIRE_DAYS = int(os.environ.get("REFRESH_TOKEN_EXPIRE_DAYS", "30"))

# --- Redis (presence, pub/sub fanout, rate limiting, OTP, idempotency) ---
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# redis-py defaults to 100 if left unset - too low for a single fan-out to a
# large group (each recipient's presence check + push is its own command) or
# a burst of concurrent logins/registrations. Sized per app instance, same
# caveat as database.connection.POOL_SIZE.
REDIS_MAX_CONNECTIONS = int(os.environ.get("REDIS_MAX_CONNECTIONS", "500"))

# --- Snowflake ID generation ---
# Must be unique per running app instance/process in production (ideally
# derived from a pod ordinal or a Redis-issued lease). A *fixed* default
# (e.g. always "1") would be actively dangerous here: forgetting to set this
# when scaling out to multiple instances - an easy mistake, since a single
# instance works fine either way - would make every instance mint colliding
# ids in lockstep. A random per-process default (10 bits => 1024 possible
# values) doesn't guarantee uniqueness across instances either, but turns a
# guaranteed collision into a low-probability one instead of the worst case.
SNOWFLAKE_MACHINE_ID = int(os.environ.get("SNOWFLAKE_MACHINE_ID", str(random.randint(0, 1023))))

# --- Server instance identity ---
# Used to tag presence entries with which instance a connection is on.
# Falls back to a random id per process start when not set (e.g. by the
# orchestrator/pod name in production).
SERVER_ID = os.environ.get("SERVER_ID", str(uuid.uuid4()))

# --- WebSocket rate limiting ---
SEND_MESSAGE_RATE_LIMIT_MAX = int(os.environ.get("SEND_MESSAGE_RATE_LIMIT_MAX", "20"))
SEND_MESSAGE_RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("SEND_MESSAGE_RATE_LIMIT_WINDOW_SECONDS", "10"))

# --- OTP abuse protection ---
# A 6-digit code (1M possibilities) with no attempt cap is brute-forceable
# well within its own TTL by any reasonably fast script - these two limits
# are what actually make that TTL meaningful.
OTP_REQUEST_RATE_LIMIT_MAX = int(os.environ.get("OTP_REQUEST_RATE_LIMIT_MAX", "20"))
OTP_REQUEST_RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("OTP_REQUEST_RATE_LIMIT_WINDOW_SECONDS", "600"))
OTP_VERIFY_MAX_ATTEMPTS = int(os.environ.get("OTP_VERIFY_MAX_ATTEMPTS", "5"))

# --- Message content size cap ---
# Applies to both new messages and edits. Without this, a single message is
# bounded only by Postgres's TEXT column (~1GB) and whatever the ASGI
# server's own WebSocket frame-size default happens to be - both are
# accidents of infrastructure, not a real limit, and a huge payload here
# gets replicated to every subscriber via Redis PUBLISH and to every
# recipient's WebSocket, in addition to bloating storage at billion-row scale.
MAX_MESSAGE_CONTENT_LENGTH = int(os.environ.get("MAX_MESSAGE_CONTENT_LENGTH", "4096"))

# --- Group creation cap ---
# create_group_chat() adds members one at a time (one DB round trip each);
# an unbounded initial_member_ids list is an easy way to turn one API call
# into millions of sequential inserts. Bulk-importing a huge membership list
# needs its own batched/background flow, not this one.
MAX_INITIAL_GROUP_MEMBERS = int(os.environ.get("MAX_INITIAL_GROUP_MEMBERS", "256"))

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
DOWNLOAD_URL_EXPIRY_SECONDS = int(os.environ.get("DOWNLOAD_URL_EXPIRY_SECONDS", "3600"))

# Per-kind upload size ceilings, in bytes. These are pinned into the
# presigned PUT signature (Content-Length), so storage itself rejects an
# upload that exceeds what the client declared - not just an app-layer check.
MAX_UPLOAD_BYTES_IMAGE = int(os.environ.get("MAX_UPLOAD_BYTES_IMAGE", str(5 * 1024 * 1024)))
MAX_UPLOAD_BYTES_VIDEO = int(os.environ.get("MAX_UPLOAD_BYTES_VIDEO", str(20 * 1024 * 1024)))
MAX_UPLOAD_BYTES_AUDIO = int(os.environ.get("MAX_UPLOAD_BYTES_AUDIO", str(20 * 1024 * 1024)))
MAX_UPLOAD_BYTES_FILE = int(os.environ.get("MAX_UPLOAD_BYTES_FILE", str(100 * 1024 * 1024)))
# Profile pictures (user + group avatars): 0.5 MB.
MAX_UPLOAD_BYTES_AVATAR = int(os.environ.get("MAX_UPLOAD_BYTES_AVATAR", str(512 * 1024)))

# Allowed upload content types, per kind. A ticket request for a kind with a
# mime outside its set is rejected before any URL is minted. Kept
# deliberately narrow for launch - widen via env / this list, not code.
ALLOWED_UPLOAD_MIME = {
    "image": {"image/jpeg", "image/png", "image/webp", "image/gif"},
    "video": {"video/mp4", "video/webm", "video/quicktime"},
    "audio": {"audio/mpeg", "audio/ogg", "audio/mp4", "audio/webm", "audio/aac"},
    "file": {
        "application/pdf",
        "text/plain",
        "application/zip",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    },
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

# Which bucket each kind lands in.
UPLOAD_BUCKET_BY_KIND = {
    "image": S3_BUCKET_MEDIA,
    "video": S3_BUCKET_MEDIA,
    "audio": S3_BUCKET_MEDIA,
    "file": S3_BUCKET_MEDIA,
    "avatar": S3_BUCKET_AVATARS,
}
