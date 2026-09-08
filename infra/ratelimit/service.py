import ipaddress
import time
from typing import Optional, Union

from config import TRUSTED_PROXY_IPS
from infra.redis.client import redis_client

_KEY_PREFIX = "ratelimit:"
_SLIDING_KEY_PREFIX = "rlsw:"


class RateLimited(Exception):
    """
    Raised by the sliding-window limiter (and, over time, by callers of the
    fixed-window one) when an identifier is over its limit. `retry_after` is a
    best-effort whole-seconds hint for the HTTP `Retry-After` header / the WS
    error payload. `main.py` maps this to HTTP 429; the WS path catches it and
    sends `{"type":"error","code":"rate_limited"}`.
    """

    def __init__(self, action: str, retry_after: int = 1):
        self.action = action
        self.retry_after = max(1, int(retry_after))
        super().__init__(f"rate limited: {action}")


async def check_and_increment(identifier: Union[int, str], action: str, max_per_window: int, window_seconds: int) -> bool:
    """
    Fixed-window counter per (identifier, action) - e.g. action="send_message",
    max_per_window=30, window_seconds=10. Returns True if the action is
    allowed (and counts it), False if the identifier is over the limit.

    `identifier` is usually a user_id, but doesn't have to be a logged-in
    user - e.g. auth_service rate-limits OTP requests/attempts by phone
    number, before any account or token exists.

    A fixed window (vs. a sliding one) can let a burst of up to 2x the limit
    through right at a window boundary - an accepted trade-off for a single
    INCR+EXPIRE round trip instead of a sorted-set per identifier. Use
    `check_sliding_window` where that boundary burst matters.
    """
    key = f"{_KEY_PREFIX}{action}:{identifier}"

    # INCR returns the post-increment count and creates the key at 1 if absent
    current_count = await redis_client.incr(key)

    if current_count == 1:
        # First hit in this window - start the window's TTL now
        await redis_client.expire(key, window_seconds)

    return current_count <= max_per_window


# Sorted-set-log sliding window. One atomic round trip:
#   1. drop entries older than (now - window)
#   2. ZCARD the survivors
#   3. if under the limit, ZADD this hit (unique member) and refresh the TTL
# Returns {allowed, retry_after_ms}. retry_after_ms is 0 when allowed, else the
# time until the oldest surviving entry ages out (i.e. when a slot frees up).
# KEYS[1] = zset key   ARGV[1] = now_ms   ARGV[2] = window_ms
# ARGV[3] = max_per_window   ARGV[4] = unique member for this hit
_SLIDING_WINDOW_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, '-inf', now - window)
local count = redis.call('ZCARD', key)

if count < limit then
    redis.call('ZADD', key, now, member)
    redis.call('PEXPIRE', key, window)
    return {1, 0}
end

local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
local retry = window
if oldest[2] then
    retry = (tonumber(oldest[2]) + window) - now
    if retry < 1 then retry = 1 end
end
redis.call('PEXPIRE', key, window)
return {0, retry}
"""

_sliding_window_script = redis_client.register_script(_SLIDING_WINDOW_LUA)

# Monotonic-ish per-process counter so two hits in the same millisecond get
# distinct zset members (score collisions are fine, member collisions lose a
# hit).
_member_seq = 0


def _next_member(now_ms: int) -> str:
    global _member_seq
    _member_seq = (_member_seq + 1) % 1_000_000
    return f"{now_ms}-{_member_seq}"


async def check_sliding_window(
    identifier: Union[int, str],
    action: str,
    max_per_window: int,
    window_seconds: Union[int, float],
) -> bool:
    """
    True if allowed (and records the hit), False if the identifier already has
    `max_per_window` hits inside the trailing `window_seconds`. Unlike the
    fixed window this never lets a 2x boundary burst through - the cost is a
    sorted set per (identifier, action) instead of a bare counter.

    Prefer `enforce_sliding_window` at call sites that want a `RateLimited`
    (with a `Retry-After` hint) rather than a bool.
    """
    now_ms = int(time.time() * 1000)
    window_ms = int(window_seconds * 1000)
    key = f"{_SLIDING_KEY_PREFIX}{action}:{identifier}"

    allowed, _retry_ms = await _sliding_window_script(
        keys=[key],
        args=[now_ms, window_ms, max_per_window, _next_member(now_ms)],
    )
    return bool(allowed)


async def enforce_sliding_window(
    identifier: Union[int, str],
    action: str,
    max_per_window: int,
    window_seconds: Union[int, float],
) -> None:
    """`check_sliding_window` that raises `RateLimited` instead of returning False."""
    now_ms = int(time.time() * 1000)
    window_ms = int(window_seconds * 1000)
    key = f"{_SLIDING_KEY_PREFIX}{action}:{identifier}"

    allowed, retry_ms = await _sliding_window_script(
        keys=[key],
        args=[now_ms, window_ms, max_per_window, _next_member(now_ms)],
    )
    if not allowed:
        raise RateLimited(action, retry_after=(int(retry_ms) + 999) // 1000)


# --- Client IP extraction (proxy-aware) --------------------------------------

_TRUSTED_PROXY_NETS = []
for _entry in TRUSTED_PROXY_IPS:
    try:
        _TRUSTED_PROXY_NETS.append(ipaddress.ip_network(_entry, strict=False))
    except ValueError:
        pass


def _peer_is_trusted_proxy(peer: Optional[str]) -> bool:
    if not peer:
        return False
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(addr in net for net in _TRUSTED_PROXY_NETS)


def client_ip(scope_or_request) -> str:
    """
    Best-effort real client IP for a Starlette Request or WebSocket.

    Trusts the first hop of `X-Forwarded-For` ONLY when the direct peer is a
    configured trusted proxy (`TRUSTED_PROXY_IPS`, default the docker bridge
    range) - otherwise a client could forge the header. Falls back to the raw
    peer address, then to "unknown".
    """
    client = getattr(scope_or_request, "client", None)
    peer = client.host if client else None

    if _peer_is_trusted_proxy(peer):
        xff = scope_or_request.headers.get("x-forwarded-for")
        if xff:
            first = xff.split(",")[0].strip()
            if first:
                return first

    return peer or "unknown"
