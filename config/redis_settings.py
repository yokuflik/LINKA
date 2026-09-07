import os

# --- Redis (presence, pub/sub fanout, rate limiting, OTP, idempotency) ---
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# redis-py defaults to 100 if left unset - too low for a single fan-out to a
# large group (each recipient's presence check + push is its own command) or
# a burst of concurrent logins/registrations. Sized per app instance, same
# caveat as database.connection.POOL_SIZE.
REDIS_MAX_CONNECTIONS = int(os.environ.get("REDIS_MAX_CONNECTIONS", "500"))

__all__ = ["REDIS_URL", "REDIS_MAX_CONNECTIONS"]
