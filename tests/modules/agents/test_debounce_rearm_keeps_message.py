"""ADR 0103 - re-arming a popped debounce entry must keep its message_id."""
import pytest

from modules.agents.invoke_debounce import arm_debounce, due_pairs, pop_latest_message_id

pytestmark = pytest.mark.asyncio


async def test_rearm_with_popped_id_survives_next_fire(redis_db):
    await arm_debounce(1, 2, 100)
    assert await pop_latest_message_id(1, 2) == 100  # fire consumed the stash
    await arm_debounce(1, 2, 100, keep_newer=True)  # busy-lock re-arm
    assert await pop_latest_message_id(1, 2) == 100


async def test_rearm_does_not_clobber_newer_message(redis_db):
    await arm_debounce(1, 2, 200)  # newer message arrived meanwhile
    await arm_debounce(1, 2, 100, keep_newer=True)
    assert await pop_latest_message_id(1, 2) == 200
