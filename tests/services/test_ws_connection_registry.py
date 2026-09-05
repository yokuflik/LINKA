import asyncio

import pytest

from services import ws_connection_registry as reg

pytestmark = pytest.mark.asyncio


async def test_register_under_cap_evicts_nothing(redis_db, monkeypatch):
    monkeypatch.setattr(reg, "WS_CONN_MAX_CONNECTIONS", 3)

    for i in range(3):
        evicted = await reg.register(101, "srv", f"conn-{i}")
        assert evicted == []


async def test_sixth_connection_evicts_the_oldest(redis_db, monkeypatch):
    monkeypatch.setattr(reg, "WS_CONN_MAX_CONNECTIONS", 5)

    for i in range(5):
        assert await reg.register(202, "srv", f"conn-{i}") == []
        await asyncio.sleep(0.002)  # keep scores strictly increasing

    evicted = await reg.register(202, "srv", "conn-5")
    assert evicted == ["srv:conn-0"]


async def test_unregister_frees_a_slot(redis_db, monkeypatch):
    monkeypatch.setattr(reg, "WS_CONN_MAX_CONNECTIONS", 2)

    await reg.register(303, "srv", "a")
    await reg.register(303, "srv", "b")
    await reg.unregister(303, "srv", "a")

    # Back to 1 live -> adding a third is fine, nothing evicted.
    assert await reg.register(303, "srv", "c") == []


async def test_parallel_opens_converge_to_the_cap(redis_db, monkeypatch):
    monkeypatch.setattr(reg, "WS_CONN_MAX_CONNECTIONS", 5)

    results = await asyncio.gather(*(reg.register(404, "srv", f"c{i}") for i in range(12)))

    total_evicted = sum(len(r) for r in results)
    # 12 opened, 5 survive -> exactly 7 evictions across the calls.
    assert total_evicted == 7


async def test_stale_entries_are_swept_on_connect(redis_db, monkeypatch):
    monkeypatch.setattr(reg, "WS_CONN_MAX_CONNECTIONS", 5)
    monkeypatch.setattr(reg, "WS_CONN_MAX_AGE_SECONDS", 0)  # everything already stale

    for i in range(5):
        await reg.register(505, "srv", f"old-{i}")

    # With max-age 0 the prior entries are all swept before the add, so the
    # set never exceeds the cap and nothing is returned as evicted.
    assert await reg.register(505, "srv", "fresh") == []
    assert await redis_db.zcard("ws:conns:505") == 1


async def test_split_member_roundtrips():
    m = reg.member("server-abc", "11111111-2222-3333-4444-555555555555")
    assert reg.split_member(m) == ("server-abc", "11111111-2222-3333-4444-555555555555")
