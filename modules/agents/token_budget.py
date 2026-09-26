"""Two rolling token-usage windows per agent (ADR 0059): 5 hours and 7 days,
combined input+output tokens. Two independent Redis fixed-window counters,
following the exact shape of time_budget.py's AGENT_DAILY_ACTIVE_SECONDS_BUDGET
counter (INCRBY a weighted amount + EXPIRE on the first hit in the window) -
infra.ratelimit.service.check_and_increment always adds exactly 1 per call and
can't be reused for a weighted counter. A fixed window (not a sliding log) is
deliberate: "resets in" is just the key's remaining TTL, exact and cheap - a
real sliding window would need a token-weighted Lua variant this codebase
doesn't have (infra/ratelimit's sliding-window script counts entries via
ZCARD, not a weighted sum). The known fixed-window trade-off (up to ~2x burst
at the window boundary) is acceptable for a usage-display feature, not a
security boundary.
"""
from dataclasses import dataclass

from config import settings
from infra.redis.client import redis_client

_KEY_PREFIX_5H = "ratelimit:agent_tokens_5h:"
_KEY_PREFIX_7D = "ratelimit:agent_tokens_7d:"

WINDOWS = ("5h", "7d")


def _key(agent_id: int, window: str) -> str:
    prefix = _KEY_PREFIX_5H if window == "5h" else _KEY_PREFIX_7D
    return f"{prefix}{agent_id}"


def _limit(window: str) -> int:
    return settings.AGENT_TOKEN_BUDGET_5H if window == "5h" else settings.AGENT_TOKEN_BUDGET_7D


def _window_seconds(window: str) -> int:
    return (
        settings.AGENT_TOKEN_BUDGET_5H_WINDOW_SECONDS
        if window == "5h"
        else settings.AGENT_TOKEN_BUDGET_7D_WINDOW_SECONDS
    )


@dataclass
class WindowUsage:
    used: int
    limit: int
    resets_in_seconds: int

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    @property
    def is_blocked(self) -> bool:
        return self.used >= self.limit

    @property
    def percent(self) -> float:
        if self.limit <= 0:
            return 0.0
        return min(100.0, round(self.used / self.limit * 100, 1))


async def _peek(agent_id: int, window: str) -> WindowUsage:
    key = _key(agent_id, window)
    used_raw = await redis_client.get(key)
    used = int(used_raw or 0)
    ttl = await redis_client.ttl(key)
    resets_in = _window_seconds(window) if ttl is None or ttl < 0 else ttl
    return WindowUsage(used=used, limit=_limit(window), resets_in_seconds=resets_in)


async def peek_usage(agent_id: int) -> dict[str, WindowUsage]:
    """Read-only, no side effect - used by GET /agents/me/usage and by the
    post-call MAX_TOKENS check in invoke_worker.py."""
    return {window: await _peek(agent_id, window) for window in WINDOWS}


async def record_tokens(agent_id: int, total_tokens: int) -> None:
    """Adds to both windows' counters. First increment in a window sets its
    TTL so it resets on schedule rather than growing forever."""
    amount = max(0, round(total_tokens))
    if amount == 0:
        return
    for window in WINDOWS:
        key = _key(agent_id, window)
        new_total = await redis_client.incrby(key, amount)
        if new_total == amount:
            await redis_client.expire(key, _window_seconds(window))
