import asyncio

import pytest

from services import rate_limit_service

pytestmark = pytest.mark.asyncio


async def test_requests_under_the_limit_are_allowed(redis_db):
    for _ in range(5):
        allowed = await rate_limit_service.check_and_increment(identifier=1, action="send_message", max_per_window=5, window_seconds=10)
        assert allowed is True


async def test_requests_over_the_limit_are_denied(redis_db):
    for _ in range(5):
        await rate_limit_service.check_and_increment(identifier=1, action="send_message", max_per_window=5, window_seconds=10)

    denied = await rate_limit_service.check_and_increment(identifier=1, action="send_message", max_per_window=5, window_seconds=10)
    assert denied is False


async def test_different_users_have_independent_limits(redis_db):
    for _ in range(5):
        await rate_limit_service.check_and_increment(identifier=1, action="send_message", max_per_window=5, window_seconds=10)

    # User 1 is now exhausted, but user 2 has touched nothing yet
    allowed_for_user_2 = await rate_limit_service.check_and_increment(identifier=2, action="send_message", max_per_window=5, window_seconds=10)
    assert allowed_for_user_2 is True


async def test_different_actions_have_independent_limits(redis_db):
    for _ in range(5):
        await rate_limit_service.check_and_increment(identifier=1, action="send_message", max_per_window=5, window_seconds=10)

    # Exhausting "send_message" must not affect an unrelated action like "create_group"
    allowed = await rate_limit_service.check_and_increment(identifier=1, action="create_group", max_per_window=5, window_seconds=10)
    assert allowed is True


async def test_window_resets_after_it_expires(redis_db):
    for _ in range(3):
        await rate_limit_service.check_and_increment(identifier=1, action="send_message", max_per_window=3, window_seconds=1)

    denied = await rate_limit_service.check_and_increment(identifier=1, action="send_message", max_per_window=3, window_seconds=1)
    assert denied is False

    await asyncio.sleep(1.2)

    allowed_again = await rate_limit_service.check_and_increment(identifier=1, action="send_message", max_per_window=3, window_seconds=1)
    assert allowed_again is True


async def test_concurrent_requests_at_the_limit_boundary_never_overcount_by_more_than_the_burst(redis_db):
    # This is exactly the scenario the module's docstring calls out: many
    # requests racing INCR at once. The invariant that must hold under load
    # isn't "exactly max_per_window get through" (a fixed window doesn't
    # promise that) but that the counter itself stays exact - no request is
    # lost or double-counted by the race.
    max_per_window = 20

    results = await asyncio.gather(*[
        rate_limit_service.check_and_increment(identifier=1, action="send_message", max_per_window=max_per_window, window_seconds=10)
        for _ in range(50)
    ])

    allowed_count = sum(1 for r in results if r)
    assert allowed_count == max_per_window

    final_count = int(await redis_db.get(f"{rate_limit_service._KEY_PREFIX}send_message:1"))
    assert final_count == 50, "every one of the 50 concurrent calls must be counted exactly once"
