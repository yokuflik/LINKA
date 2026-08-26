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
OTP_REQUEST_RATE_LIMIT_MAX = int(os.environ.get("OTP_REQUEST_RATE_LIMIT_MAX", "3"))
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
