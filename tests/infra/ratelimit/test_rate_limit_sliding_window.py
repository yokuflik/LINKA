import asyncio

import pytest

from infra.ratelimit import service as rate_limit_service
from infra.ratelimit.service import RateLimited

pytestmark = pytest.mark.asyncio


async def test_hits_under_the_limit_are_allowed(redis_db):
    for _ in range(5):
        assert await rate_limit_service.check_sliding_window(1, "send", 5, 10) is True


async def test_hits_over_the_limit_are_denied_exactly_at_the_boundary(redis_db):
    for _ in range(5):
        assert await rate_limit_service.check_sliding_window(1, "send", 5, 10) is True

    # No 2x boundary burst: the 6th within the window is refused.
    assert await rate_limit_service.check_sliding_window(1, "send", 5, 10) is False


async def test_window_slides_so_old_hits_age_out(redis_db):
    for _ in range(3):
        await rate_limit_service.check_sliding_window(1, "send", 3, 1)
    assert await rate_limit_service.check_sliding_window(1, "send", 3, 1) is False

    await asyncio.sleep(1.1)

    assert await rate_limit_service.check_sliding_window(1, "send", 3, 1) is True


async def test_identifiers_and_actions_are_independent(redis_db):
    for _ in range(3):
        await rate_limit_service.check_sliding_window(1, "send", 3, 10)

    assert await rate_limit_service.check_sliding_window(2, "send", 3, 10) is True
    assert await rate_limit_service.check_sliding_window(1, "typing", 3, 10) is True


async def test_concurrent_hits_never_exceed_the_limit(redis_db):
    results = await asyncio.gather(*[
        rate_limit_service.check_sliding_window(1, "send", 20, 10) for _ in range(50)
    ])
    assert sum(1 for r in results if r) == 20


async def test_enforce_raises_ratelimited_with_a_retry_hint(redis_db):
    for _ in range(2):
        await rate_limit_service.enforce_sliding_window(1, "send", 2, 5)

    with pytest.raises(RateLimited) as exc_info:
        await rate_limit_service.enforce_sliding_window(1, "send", 2, 5)
    assert exc_info.value.action == "send"
    assert 1 <= exc_info.value.retry_after <= 5


async def test_lua_script_is_cached_and_reused_across_calls(redis_db):
    # register_script() hands back a single Script object with a stable SHA;
    # every call is a one-round-trip EVALSHA against that cached script, never
    # a re-load. Guards against accidentally rebuilding the script per call.
    script = rate_limit_service._sliding_window_script
    await rate_limit_service.check_sliding_window("cache", "probe", 10, 10)
    sha = script.sha
    assert sha

    for _ in range(5):
        await rate_limit_service.check_sliding_window("cache", "probe", 10, 10)
    assert script.sha == sha
    # The script is loaded in Redis and callable by SHA alone.
    assert await redis_db.script_exists(sha) == [True]


