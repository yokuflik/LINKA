import os
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
# Must be unique per running app instance/process in production (e.g. derived
# from a pod ordinal or a Redis-issued lease) - a fixed default only works for
# single-instance/local development.
SNOWFLAKE_MACHINE_ID = int(os.environ.get("SNOWFLAKE_MACHINE_ID", "1"))

# --- Server instance identity ---
# Used to tag presence entries with which instance a connection is on.
# Falls back to a random id per process start when not set (e.g. by the
# orchestrator/pod name in production).
SERVER_ID = os.environ.get("SERVER_ID", str(uuid.uuid4()))

# --- WebSocket rate limiting ---
SEND_MESSAGE_RATE_LIMIT_MAX = int(os.environ.get("SEND_MESSAGE_RATE_LIMIT_MAX", "20"))
SEND_MESSAGE_RATE_LIMIT_WINDOW_SECONDS = int(os.environ.get("SEND_MESSAGE_RATE_LIMIT_WINDOW_SECONDS", "10"))
