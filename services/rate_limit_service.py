from typing import Union

from services.redis_client import redis_client

_KEY_PREFIX = "ratelimit:"


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
    INCR+EXPIRE round trip instead of a sorted-set per identifier.
    """
    key = f"{_KEY_PREFIX}{action}:{identifier}"

    # INCR returns the post-increment count and creates the key at 1 if absent
    current_count = await redis_client.incr(key)

    if current_count == 1:
        # First hit in this window - start the window's TTL now
        await redis_client.expire(key, window_seconds)

    return current_count <= max_per_window
