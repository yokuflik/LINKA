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

# --- Rust ID service (ADR 0011) ---
# gRPC address of the standalone Rust Snowflake service, e.g. "id_service:50051".
# Empty (the default) => keep minting ids in-process with SNOWFLAKE_MACHINE_ID.
# When set, utils.id_client.next_id() calls the service (unary, one id per RPC -
# never batch, the id timestamp is Postgres' created_at partition-routing key).
ID_SERVICE_ADDR = os.environ.get("ID_SERVICE_ADDR", "")
# Per-call deadline; on timeout/unavailable id_client falls back to the local
# generator so id minting never hard-stops.
ID_SERVICE_TIMEOUT_SECONDS = float(os.environ.get("ID_SERVICE_TIMEOUT_SECONDS", "0.5"))

# --- Server instance identity ---
# Used to tag presence entries with which instance a connection is on.
# Falls back to a random id per process start when not set (e.g. by the
# orchestrator/pod name in production).
SERVER_ID = os.environ.get("SERVER_ID", str(uuid.uuid4()))

# --- WebSocket rate limiting ---
# Legacy fixed-window send limit (still read as a coarse fallback ceiling by
# nothing now that step 6 landed - kept only so an old env file doesn't break).
SEND_MESSAGE_RATE_LIMIT_MAX = int(os.environ.get("SEND_MESSAGE_RATE_LIMIT_MAX", "20"))
SEND_MESSAGE_RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("SEND_MESSAGE_RATE_LIMIT_WINDOW_SECONDS", "10"))

# --- WebSocket per-frame + per-action limits (COMMS_SECURITY_PLAN step 6) ---
# All sliding-window, all keyed per-user (or per-connection for the frame rate)
# so one client's flood can't starve another and can't wedge the send stream.
#
# Global inbound frame rate, per connection, checked before dispatch. Over ->
# one rate_limited error + the frame is dropped (NOT a close - a laggy client
# that batches is not an attacker). Staying over for WS_FRAME_FLOOD_STRIKES
# consecutive over-limit frames -> close 4429.
WS_FRAME_RATE_MAX = int(os.environ.get("WS_FRAME_RATE_MAX", "30"))
WS_FRAME_RATE_WINDOW_SECONDS = int(os.environ.get("WS_FRAME_RATE_WINDOW_SECONDS", "10"))
WS_FRAME_FLOOD_STRIKES = int(os.environ.get("WS_FRAME_FLOOD_STRIKES", "60"))

# send_message: 3 / second / user (primary), plus a 40 / 60 s sustained-spam
# ceiling. Both must pass.
WS_SEND_MESSAGE_RATE_MAX = int(os.environ.get("WS_SEND_MESSAGE_RATE_MAX", "3"))
WS_SEND_MESSAGE_RATE_WINDOW_SECONDS = float(os.environ.get("WS_SEND_MESSAGE_RATE_WINDOW_SECONDS", "1"))
WS_SEND_MESSAGE_BURST_MAX = int(os.environ.get("WS_SEND_MESSAGE_BURST_MAX", "40"))
WS_SEND_MESSAGE_BURST_WINDOW_SECONDS = int(os.environ.get("WS_SEND_MESSAGE_BURST_WINDOW_SECONDS", "60"))

# Per-action buckets, per user. mark_delivered/read/played share one bucket;
# edit_message/delete_message/restore_message share one; typing/recording share
# one (the client already self-throttles to 1/3s - this just enforces it).
WS_RECEIPTS_RATE_MAX = int(os.environ.get("WS_RECEIPTS_RATE_MAX", "60"))
WS_RECEIPTS_RATE_WINDOW_SECONDS = int(os.environ.get("WS_RECEIPTS_RATE_WINDOW_SECONDS", "10"))
WS_SUBSCRIBE_PRESENCE_RATE_MAX = int(os.environ.get("WS_SUBSCRIBE_PRESENCE_RATE_MAX", "20"))
WS_SUBSCRIBE_PRESENCE_RATE_WINDOW_SECONDS = int(os.environ.get("WS_SUBSCRIBE_PRESENCE_RATE_WINDOW_SECONDS", "10"))
WS_TYPING_RATE_MAX = int(os.environ.get("WS_TYPING_RATE_MAX", "10"))
WS_TYPING_RATE_WINDOW_SECONDS = int(os.environ.get("WS_TYPING_RATE_WINDOW_SECONDS", "10"))
WS_EDIT_RATE_MAX = int(os.environ.get("WS_EDIT_RATE_MAX", "20"))
WS_EDIT_RATE_WINDOW_SECONDS = int(os.environ.get("WS_EDIT_RATE_WINDOW_SECONDS", "60"))

# --- WebSocket connection cap + handshake churn (COMMS_SECURITY_PLAN step 5) ---
# Hard cap on concurrent WS connections per user, cross-process. On the
# (cap+1)th connect the *oldest* connection is evicted (WhatsApp-style) - the
# new one is never rejected.
WS_CONN_MAX_CONNECTIONS = int(os.environ.get("WS_CONN_MAX_CONNECTIONS", "5"))
# A connection whose zset entry is older than this is swept on the next
# connect for the same user - covers a process that crashed without running
# its unregister. Must comfortably exceed any real session length; 26h leaves
# room for a day-long session plus clock skew.
WS_CONN_MAX_AGE_SECONDS = int(os.environ.get("WS_CONN_MAX_AGE_SECONDS", str(26 * 3600)))
# Handshake churn: cap on *successful* /ws upgrades, checked right after auth
# (before the per-connect DB query). Over the limit closes 4429 without
# accepting. Sliding window so a boundary burst can't slip through.
WS_UPGRADE_IP_RATE_LIMIT_MAX = int(os.environ.get("WS_UPGRADE_IP_RATE_LIMIT_MAX", "20"))
WS_UPGRADE_IP_RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("WS_UPGRADE_IP_RATE_LIMIT_WINDOW_SECONDS", "10"))
WS_UPGRADE_USER_RATE_LIMIT_MAX = int(os.environ.get("WS_UPGRADE_USER_RATE_LIMIT_MAX", "10"))
WS_UPGRADE_USER_RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("WS_UPGRADE_USER_RATE_LIMIT_WINDOW_SECONDS", "10"))
# Defensive ceiling on the per-connect "every chat this user is in" query.
WS_MAX_CHAT_IDS_ON_CONNECT = int(os.environ.get("WS_MAX_CHAT_IDS_ON_CONNECT", "2000"))

# --- Transport hardening & rate limiting (ADR 0012, COMMS_SECURITY_PLAN) ---
# Direct-peer IPs/CIDRs whose `X-Forwarded-For` first hop we trust as the real
# client IP. In the compose deploy the only thing in front of the app is the
# Caddy container on the default docker bridge. Anything not from here has its
# XFF ignored (a client could otherwise forge it).
TRUSTED_PROXY_IPS = [
    x.strip() for x in os.environ.get("TRUSTED_PROXY_IPS", "172.16.0.0/12,127.0.0.1/32").split(",") if x.strip()
]
# Hostnames the app will answer to (Starlette TrustedHostMiddleware).
# "*" disables the check. Default covers local dev only.
ALLOWED_HOSTS = [
    x.strip() for x in os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1,testserver,test").split(",") if x.strip()
]
# Allowed browser Origins for CORS and the WebSocket handshake. Prod is
# same-origin (Caddy serves the PoC + API together) so this is normally the
# single site origin. "*" is dev-only and forces allow_credentials=False
# (the "*" + credentials combination is invalid and silently unsafe).
CORS_ALLOW_ORIGINS = [
    x.strip() for x in os.environ.get("CORS_ALLOW_ORIGINS", "*").split(",") if x.strip()
]
# Coarse per-IP ceiling across all REST (a backstop, not the primary control).
# Business answer 1 (2026-09-06): 1000 requests / 3 min / IP.
API_IP_BACKSTOP_MAX = int(os.environ.get("API_IP_BACKSTOP_MAX", "1000"))
API_IP_BACKSTOP_WINDOW_SECONDS = int(os.environ.get("API_IP_BACKSTOP_WINDOW_SECONDS", "180"))

# --- OTP abuse protection ---
# A 6-digit code (1M possibilities) with no attempt cap is brute-forceable
# well within its own TTL by any reasonably fast script - these limits
# are what actually make that TTL meaningful.
#
# Two axes (COMMS_SECURITY_PLAN step 4): per-PHONE limits protect a single
# number's owner from SMS-bombing; per-IP limits stop one host spraying OTPs
# at thousands of different numbers. The per-phone request limit was retuned
# from 20/10min down to 5/30min.
OTP_REQUEST_RATE_LIMIT_MAX = int(os.environ.get("OTP_REQUEST_RATE_LIMIT_MAX", "5"))
OTP_REQUEST_RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("OTP_REQUEST_RATE_LIMIT_WINDOW_SECONDS", "1800"))
OTP_VERIFY_MAX_ATTEMPTS = int(os.environ.get("OTP_VERIFY_MAX_ATTEMPTS", "5"))

# Per-IP OTP ceilings.
OTP_REQUEST_IP_RATE_LIMIT_MAX = int(os.environ.get("OTP_REQUEST_IP_RATE_LIMIT_MAX", "15"))
OTP_REQUEST_IP_RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("OTP_REQUEST_IP_RATE_LIMIT_WINDOW_SECONDS", "3600"))
OTP_VERIFY_IP_RATE_LIMIT_MAX = int(os.environ.get("OTP_VERIFY_IP_RATE_LIMIT_MAX", "30"))
OTP_VERIFY_IP_RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("OTP_VERIFY_IP_RATE_LIMIT_WINDOW_SECONDS", "3600"))

# Per-IP /auth/refresh ceiling, plus a per-refresh-token-jti throttle so a
# single leaked refresh token can't be spun into unlimited access tokens.
REFRESH_IP_RATE_LIMIT_MAX = int(os.environ.get("REFRESH_IP_RATE_LIMIT_MAX", "60"))
REFRESH_IP_RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("REFRESH_IP_RATE_LIMIT_WINDOW_SECONDS", "3600"))
REFRESH_JTI_RATE_LIMIT_MAX = int(os.environ.get("REFRESH_JTI_RATE_LIMIT_MAX", "10"))
REFRESH_JTI_RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("REFRESH_JTI_RATE_LIMIT_WINDOW_SECONDS", "3600"))

# New accounts (first successful verify for a previously-unknown phone) per IP
# per day. Strict on purpose (business answer 5, 2026-09-06): no relaxation.
ACCOUNT_CREATE_IP_RATE_LIMIT_MAX = int(os.environ.get("ACCOUNT_CREATE_IP_RATE_LIMIT_MAX", "5"))
ACCOUNT_CREATE_IP_RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("ACCOUNT_CREATE_IP_RATE_LIMIT_WINDOW_SECONDS", "86400"))

# --- REST feature limits (COMMS_SECURITY_PLAN step 7) ---
# All per-user sliding windows (rlsw:) except the upload-ticket per-IP gate
# (fixed window). Raise RateLimited -> HTTP 429. The global per-IP REST
# backstop (API_IP_BACKSTOP_*) landed in step 3 and is the coarse ceiling
# above all of these.
#
# GET /chats/{id}/messages - history pagination. `limit` is clamped server-side.
MSG_HISTORY_RATE_MAX = int(os.environ.get("MSG_HISTORY_RATE_MAX", "30"))
MSG_HISTORY_RATE_WINDOW_SECONDS = int(os.environ.get("MSG_HISTORY_RATE_WINDOW_SECONDS", "60"))
MSG_HISTORY_MAX_LIMIT = int(os.environ.get("MSG_HISTORY_MAX_LIMIT", "100"))
# POST /chats/{id}/messages/upload-ticket - media upload ticket (no volume quota;
# size caps + MIME whitelist + checksum pinning already enforced, ADR 0010).
UPLOAD_TICKET_RATE_MAX = int(os.environ.get("UPLOAD_TICKET_RATE_MAX", "5"))
UPLOAD_TICKET_RATE_WINDOW_SECONDS = int(os.environ.get("UPLOAD_TICKET_RATE_WINDOW_SECONDS", "60"))
UPLOAD_TICKET_IP_RATE_LIMIT_MAX = int(os.environ.get("UPLOAD_TICKET_IP_RATE_LIMIT_MAX", "20"))
UPLOAD_TICKET_IP_RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("UPLOAD_TICKET_IP_RATE_LIMIT_WINDOW_SECONDS", "60"))
# Per-message detail reads (GET .../{message_id}/receipts and similar).
DETAIL_READ_RATE_MAX = int(os.environ.get("DETAIL_READ_RATE_MAX", "60"))
DETAIL_READ_RATE_WINDOW_SECONDS = int(os.environ.get("DETAIL_READ_RATE_WINDOW_SECONDS", "60"))
# List reads (GET /chats, GET /users/*).
LIST_READ_RATE_MAX = int(os.environ.get("LIST_READ_RATE_MAX", "120"))
LIST_READ_RATE_WINDOW_SECONDS = int(os.environ.get("LIST_READ_RATE_WINDOW_SECONDS", "60"))

# --- Firebase Phone Auth (ADR 0009) ---
# Real phone verification runs client-side (Firebase JS SDK + reCAPTCHA); the
# server only verifies the resulting ID token against Google's public JWKS.
# FIREBASE_PROJECT_ID doubles as the expected token `aud` / `iss` suffix.
FIREBASE_PROJECT_ID = os.environ.get("FIREBASE_PROJECT_ID", "")
FIREBASE_AUTH_ENABLED = bool(FIREBASE_PROJECT_ID)
# Exact phone-number strings that skip all verification (dev/PoC only). These
# are not real numbers - the client routes them through the legacy OTP stub.
DEV_AUTH_WHITELIST = set(
    x.strip() for x in os.environ.get("DEV_AUTH_WHITELIST", "1,2,3,4,5").split(",") if x.strip()
)

# --- Usernames (ADR 0017) ---
# Unique, lowercase-canonical handle on every user. A plain unique btree index
# on users.username is the uniqueness authority; values are normalised to
# lowercase before every write. Format is validated as untrusted input and each
# rejection carries a machine `reason` code so the frontend can show a precise
# hint (too_short / too_long / bad_chars / must_start_letter / reserved / taken
# / grace_hold / cooldown).
USERNAME_MIN_LEN = int(os.environ.get("USERNAME_MIN_LEN", "3"))
USERNAME_MAX_LEN = int(os.environ.get("USERNAME_MAX_LEN", "32"))
# 3-32 chars, starts with a lowercase letter, then lowercase letters / digits /
# underscore. Kept here (not hard-coded in the validator) so the bounds and the
# regex can be tuned from one place.
USERNAME_REGEX = os.environ.get(
    "USERNAME_REGEX", r"^[a-z][a-z0-9_]{%d,%d}$" % (USERNAME_MIN_LEN - 1, USERNAME_MAX_LEN - 1)
)
# Handles nobody may register (impersonation / routing collisions).
USERNAME_RESERVED = set(
    x.strip().lower()
    for x in os.environ.get(
        "USERNAME_RESERVED",
        "admin,support,linka,me,null,system,help,info,root,about,search,settings",
    ).split(",")
    if x.strip()
)
# A user-initiated username change is refused (`cooldown`) until this many days
# after the last change. The initial auto-assignment at signup does NOT start
# this clock (users.username_changed_at stays NULL), so the first chosen
# username is free.
USERNAME_CHANGE_COOLDOWN_DAYS = int(os.environ.get("USERNAME_CHANGE_COOLDOWN_DAYS", "14"))
# When a username is released (its owner changed it), it is held in
# reserved_usernames for this many days: nobody else may take it (`grace_hold`),
# the original owner may reclaim it. Anti-impersonation.
USERNAME_RESERVED_GRACE_DAYS = int(os.environ.get("USERNAME_RESERVED_GRACE_DAYS", "14"))
# Advisory availability endpoint (GET /users/username-available): tight per-user
# sliding window so it can't be used to enumerate the users table.
USERNAME_CHECK_RATE_MAX = int(os.environ.get("USERNAME_CHECK_RATE_MAX", "20"))
USERNAME_CHECK_RATE_WINDOW_SECONDS = int(os.environ.get("USERNAME_CHECK_RATE_WINDOW_SECONDS", "60"))
# Exact-match user search (endpoint deferred, ADR 0017). Dedicated bucket for
# when it lands - exact match only, never a prefix/LIKE scan.
USERNAME_SEARCH_RATE_MAX = int(os.environ.get("USERNAME_SEARCH_RATE_MAX", "15"))
USERNAME_SEARCH_RATE_WINDOW_SECONDS = int(os.environ.get("USERNAME_SEARCH_RATE_WINDOW_SECONDS", "60"))
# How many candidate handles generate_free_username tries before widening the
# random digit suffix. Each attempt is one indexed existence check.
USERNAME_GENERATE_ATTEMPTS = int(os.environ.get("USERNAME_GENERATE_ATTEMPTS", "6"))

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

# --- Detailed receipt log (per-user delivered/read/played history) ---
# On top of the O(1) watermark rollup that drives the sent/delivered/read/
# played tick (see database/models/message.py MessageStatus), every genuine
# watermark advance is also appended to message_receipt_log with its
# timestamp. That log answers "when exactly did user U read message X" and,
# in a group, "who has read/played message X" - neither of which the
# watermark model can. It is never touched by the chat list or the
# per-bubble check mark.
#
# Integer kind values deliberately mirror MessageStatus (2/3/4).
RECEIPT_KIND_DELIVERED = 2
RECEIPT_KIND_READ = 3
RECEIPT_KIND_PLAYED = 4
RECEIPT_KINDS = {RECEIPT_KIND_DELIVERED, RECEIPT_KIND_READ, RECEIPT_KIND_PLAYED}

# A message's "seen by" / "played by" detail view returns a per-member name
# list only for chats at or below this participant count; above it, only
# aggregate counts ("read by 812 of 1200"). Keeps the detail query and its
# payload bounded for very large groups.
RECEIPT_NAMED_LIST_MAX_MEMBERS = int(os.environ.get("RECEIPT_NAMED_LIST_MAX_MEMBERS", "256"))

# How long the append-only detailed log is retained. Older rows are dropped
# a whole partition at a time (scripts/prune_receipt_log.py); the coarse
# Participant.last_delivered_at/last_read_at/last_played_at columns are not
# on this clock and stay as a "last activity" fallback for old messages.
RECEIPT_LOG_RETENTION_DAYS = int(os.environ.get("RECEIPT_LOG_RETENTION_DAYS", "30"))

# The write path never INSERTs into message_receipt_log inline: it XADDs a
# tiny event onto this Redis Stream, and a background worker
# (services/receipts/worker.py) batch-drains it, collapsing many events for
# the same (chat, user, kind) into one row - so a 1000-member group opening
# a chat is a single multi-row INSERT, not 1000 transactions on a
# billion-row-scale table.
RECEIPT_STREAM_KEY = os.environ.get("RECEIPT_STREAM_KEY", "receipt_log_stream")
RECEIPT_STREAM_GROUP = os.environ.get("RECEIPT_STREAM_GROUP", "receipt_writers")
# Approximate MAXLEN cap (backpressure safety valve - a wedged worker can't
# grow the stream without bound).
RECEIPT_STREAM_MAXLEN = int(os.environ.get("RECEIPT_STREAM_MAXLEN", "1000000"))
RECEIPT_WORKER_BATCH = int(os.environ.get("RECEIPT_WORKER_BATCH", "500"))
RECEIPT_WORKER_BLOCK_MS = int(os.environ.get("RECEIPT_WORKER_BLOCK_MS", "2000"))
# Pending entries idle longer than this (a worker crashed mid-batch) are
# reclaimed by another worker via XAUTOCLAIM.
RECEIPT_STREAM_CLAIM_IDLE_MS = int(os.environ.get("RECEIPT_STREAM_CLAIM_IDLE_MS", "60000"))


# --- Outgoing message send queue (services/fanout) ---
# The WebSocket send path no longer writes the message or fans it out inline:
# it XADDs a tiny payload onto this Redis Stream and ACKs {"status":"queued"}
# immediately. A background worker (services/fanout/worker.py) drains it,
# persists each message and runs the fan-out. Two things this buys: the
# request path stops blocking on an N-participant fan-out, and a large group
# send no longer holds a pooled DB connection for the sender's whole ack.
# Unlike the receipt stream, an XADD failure here is NOT swallowed - a lost
# entry means a message the sender thinks was sent; the caller returns a sync
# error instead.
MESSAGE_SEND_STREAM_KEY = os.environ.get("MESSAGE_SEND_STREAM_KEY", "message_send_stream")
MESSAGE_SEND_STREAM_GROUP = os.environ.get("MESSAGE_SEND_STREAM_GROUP", "message_send_writers")
MESSAGE_SEND_STREAM_MAXLEN = int(os.environ.get("MESSAGE_SEND_STREAM_MAXLEN", "1000000"))
SEND_WORKER_BATCH = int(os.environ.get("SEND_WORKER_BATCH", "200"))
SEND_WORKER_BLOCK_MS = int(os.environ.get("SEND_WORKER_BLOCK_MS", "2000"))
SEND_STREAM_CLAIM_IDLE_MS = int(os.environ.get("SEND_STREAM_CLAIM_IDLE_MS", "60000"))
# Shard the send stream by chat_id (FANOUT_REWRITE_PLAN.md step 4) so a single
# write worker stops being the throughput ceiling. shard = chat_id % N keeps
# every message for one chat on one shard / one consumer, preserving order.
# The unsharded key stays the shard-0 key so an in-flight upgrade doesn't
# strand entries. 1 = effectively unsharded.
SEND_STREAM_SHARDS = int(os.environ.get("SEND_STREAM_SHARDS", "4"))


# --- Message fan-out queue (services/fanout, step 2) ---
# The send worker no longer fans a persisted message out inline: it XADDs a
# tiny reference (message id + chat + sender + client_message_id) onto this
# second stream and a separate fan-out worker (services/fanout/fanout_worker.py)
# builds the new_message event, publishes it, and pushes to offline members.
# Two streams on purpose: the DB write and the Redis/network fan-out fail and
# scale differently. Re-running fan-out for a message just re-publishes -
# clients dedupe by message_id - so a redelivered entry is harmless.
MESSAGE_FANOUT_STREAM_KEY = os.environ.get("MESSAGE_FANOUT_STREAM_KEY", "message_fanout_stream")
MESSAGE_FANOUT_STREAM_GROUP = os.environ.get("MESSAGE_FANOUT_STREAM_GROUP", "message_fanout_workers")
MESSAGE_FANOUT_STREAM_MAXLEN = int(os.environ.get("MESSAGE_FANOUT_STREAM_MAXLEN", "1000000"))
FANOUT_WORKER_BATCH = int(os.environ.get("FANOUT_WORKER_BATCH", "200"))
FANOUT_WORKER_BLOCK_MS = int(os.environ.get("FANOUT_WORKER_BLOCK_MS", "2000"))
FANOUT_STREAM_CLAIM_IDLE_MS = int(os.environ.get("FANOUT_STREAM_CLAIM_IDLE_MS", "60000"))
# Sharded like the send stream (FANOUT_REWRITE_PLAN.md step 4), same chat_id % N
# rule. Fan-out ordering per chat matters less than the write stream's but is
# cheap to keep. 1 = effectively unsharded.
FANOUT_STREAM_SHARDS = int(os.environ.get("FANOUT_STREAM_SHARDS", "4"))


# --- Routing layer (FANOUT_REWRITE_PLAN.md step 3) ---
# Instead of every process subscribing to every chat one of its clients is in,
# each process registers itself in Redis as serving a chat (chat_instances:{id}
# set), and the fan-out worker publishes a chat event only to the inbox
# channels (instance_inbox:{server_id}) of the processes that actually have a
# local member. chat_instances entries carry a TTL refreshed by a heartbeat so
# a crashed process's registrations expire instead of lingering forever.
CHAT_INSTANCE_TTL_SECONDS = int(os.environ.get("CHAT_INSTANCE_TTL_SECONDS", "90"))
ROUTING_HEARTBEAT_INTERVAL_SECONDS = int(os.environ.get("ROUTING_HEARTBEAT_INTERVAL_SECONDS", "30"))


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

# --- Time-partition management (scripts/manage_partitions.py, ADR 0005) ---
# messages is RANGE-partitioned by created_at, message_receipt_log by
# occurred_at. A standalone idempotent script creates dated partitions ahead
# of time, reports fill/DEFAULT state, and cold-freezes old ones. These are
# values only - all boundary/scheduling logic lives in the script.
#
# Partition granularity per table. messages: one ISO week (Monday 00:00 UTC).
# message_receipt_log: one calendar day (UTC). Changing these does not
# reshape existing partitions - only new ones.
MESSAGE_PARTITION_INTERVAL = os.environ.get("MESSAGE_PARTITION_INTERVAL", "week")
RECEIPT_LOG_PARTITION_INTERVAL = os.environ.get("RECEIPT_LOG_PARTITION_INTERVAL", "day")

# How far ahead --ensure keeps empty partitions pre-created, so a write is
# never the thing that discovers a missing partition. The daily cron only
# needs to add one at a time; the buffer covers missed runs.
MESSAGE_PARTITION_PRECREATE_WEEKS = int(os.environ.get("MESSAGE_PARTITION_PRECREATE_WEEKS", "6"))
RECEIPT_LOG_PRECREATE_DAYS = int(os.environ.get("RECEIPT_LOG_PRECREATE_DAYS", "10"))

# A messages partition whose whole range is older than this is eligible for
# --cold: VACUUM FREEZE, move to the cold tablespace, autovacuum_enabled=false.
# messages retention is infinite; receipt_log retention stays on
# RECEIPT_LOG_RETENTION_DAYS via scripts/prune_receipt_log.py.
MESSAGE_PARTITION_COLD_AFTER_MONTHS = int(os.environ.get("MESSAGE_PARTITION_COLD_AFTER_MONTHS", "12"))

# Tablespace that --cold moves frozen old `messages` partitions onto. A DBA must
# pre-create it (CREATE TABLESPACE ... LOCATION ...) pointing at cheaper storage.
# Empty string = skip the physical move; --cold still runs VACUUM FREEZE and
# disables autovacuum on the partition.
MESSAGE_PARTITION_COLD_TABLESPACE = os.environ.get("MESSAGE_PARTITION_COLD_TABLESPACE", "")

# Slack applied to a Snowflake-id-derived created_at bound before it is used as
# a partition-pruning predicate on `messages` (crud_message). A message id is
# minted in-process a short moment before its row's server-side created_at
# default fires, so the id's timestamp and the stored created_at can differ by
# up to a few seconds; widening the derived bound by this margin keeps the
# predicate a safe superset (never drops a matching row) while still letting
# Postgres prune whole weekly partitions.
MESSAGE_PARTITION_QUERY_SKEW_HOURS = int(os.environ.get("MESSAGE_PARTITION_QUERY_SKEW_HOURS", "1"))


# Which bucket each kind lands in.
UPLOAD_BUCKET_BY_KIND = {
    "image": S3_BUCKET_MEDIA,
    "video": S3_BUCKET_MEDIA,
    "audio": S3_BUCKET_MEDIA,
    "file": S3_BUCKET_MEDIA,
    "avatar": S3_BUCKET_AVATARS,
}
