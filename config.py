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

# Which bucket each kind lands in.
UPLOAD_BUCKET_BY_KIND = {
    "image": S3_BUCKET_MEDIA,
    "video": S3_BUCKET_MEDIA,
    "audio": S3_BUCKET_MEDIA,
    "file": S3_BUCKET_MEDIA,
    "avatar": S3_BUCKET_AVATARS,
}
