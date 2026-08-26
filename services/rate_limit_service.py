from services.redis_client import redis_client

_KEY_PREFIX = "ratelimit:"


async def check_and_increment(user_id: int, action: str, max_per_window: int, window_seconds: int) -> bool:
    """
    Fixed-window counter per (user, action) - e.g. action="send_message",
    max_per_window=30, window_seconds=10. Returns True if the action is
    allowed (and counts it), False if the user is over the limit.

    A fixed window (vs. a sliding one) can let a burst of up to 2x the limit
    through right at a window boundary - an accepted trade-off for a single
    INCR+EXPIRE round trip instead of a sorted-set per user.
    """
    key = f"{_KEY_PREFIX}{action}:{user_id}"

    # INCR returns the post-increment count and creates the key at 1 if absent
    current_count = await redis_client.incr(key)

    if current_count == 1:
        # First hit in this window - start the window's TTL now
        await redis_client.expire(key, window_seconds)

    return current_count <= max_per_window
