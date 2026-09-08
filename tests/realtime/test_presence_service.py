import asyncio

import pytest

from services import presence_service

pytestmark = pytest.mark.asyncio


async def test_user_is_offline_by_default(redis_db):
    assert await presence_service.is_online(999) is False


async def test_mark_online_then_offline(redis_db):
    await presence_service.mark_online(user_id=1, connection_id="conn-a", server_id="server-1")
    assert await presence_service.is_online(1) is True

    await presence_service.mark_offline(user_id=1, connection_id="conn-a")
    assert await presence_service.is_online(1) is False


async def test_multi_device_stays_online_until_every_connection_drops(redis_db):
    # WhatsApp-style semantics: a user with the app open on two phones is
    # only "offline" once *both* connections are gone, not after the first.
    await presence_service.mark_online(1, "conn-a", "server-1")
    await presence_service.mark_online(1, "conn-b", "server-2")

    await presence_service.mark_offline(1, "conn-a")
    assert await presence_service.is_online(1) is True, "should still be online on the second device"

    await presence_service.mark_offline(1, "conn-b")
    assert await presence_service.is_online(1) is False


async def test_mark_offline_for_a_connection_that_was_never_online_is_a_noop(redis_db):
    await presence_service.mark_offline(user_id=1, connection_id="never-existed")
    assert await presence_service.is_online(1) is False


async def test_presence_expires_without_a_heartbeat(redis_db, monkeypatch):
    # Guards against a client that dies without a clean disconnect (app
    # killed, phone loses signal) staying "online" forever.
    monkeypatch.setattr(presence_service, "_PRESENCE_TTL_SECONDS", 1)

    await presence_service.mark_online(1, "conn-a", "server-1")
    assert await presence_service.is_online(1) is True

    await asyncio.sleep(1.5)
    assert await presence_service.is_online(1) is False


async def test_heartbeat_keeps_presence_alive_past_the_ttl(redis_db, monkeypatch):
    monkeypatch.setattr(presence_service, "_PRESENCE_TTL_SECONDS", 1)

    await presence_service.mark_online(1, "conn-a", "server-1")
    await asyncio.sleep(0.6)
    await presence_service.heartbeat(1, "conn-a")
    await asyncio.sleep(0.6)

    # 1.2s have passed with a 1s TTL, but the heartbeat at 0.6s refreshed it
    assert await presence_service.is_online(1) is True


async def test_last_seen_is_stamped_on_every_device_disconnect(redis_db):
    # "Last seen" follows the most recently connected device: even while the
    # user is still online on another device, the stored timestamp advances
    # each time a device drops, so it is current the instant the last one goes.
    await presence_service.mark_online(1, "conn-a", "server-1")
    await presence_service.mark_online(1, "conn-b", "server-2")

    await presence_service.mark_offline(1, "conn-a")
    assert await presence_service.is_online(1) is True
    after_first_drop = (await presence_service.get_status(1))["last_seen_at"]
    assert after_first_drop is not None

    await asyncio.sleep(0.01)
    await presence_service.mark_offline(1, "conn-b")
    status = await presence_service.get_status(1)
    assert status["status"] == "offline"
    assert status["last_seen_at"] > after_first_drop


async def test_last_seen_advances_on_connect_and_heartbeat(redis_db):
    await presence_service.mark_online(1, "conn-a", "server-1")
    first = (await presence_service.get_status(1))["last_seen_at"]

    await asyncio.sleep(0.01)
    await presence_service.heartbeat(1, "conn-a")
    assert (await presence_service.get_status(1))["last_seen_at"] > first


async def test_get_online_participants_filters_a_mixed_list(redis_db):
    await presence_service.mark_online(1, "conn-a", "server-1")
    await presence_service.mark_online(3, "conn-b", "server-1")
    # user 2 never connects

    online = await presence_service.get_online_participants([1, 2, 3])
    assert online == {1, 3}


async def test_get_online_participants_handles_an_empty_list(redis_db):
    assert await presence_service.get_online_participants([]) == set()


async def test_get_online_participants_at_scale(redis_db):
    # A large group (e.g. a 5,000-member broadcast channel) must resolve in
    # one round trip (a pipeline), not one Redis call per member.
    online_ids = list(range(1, 2001))
    offline_ids = list(range(2001, 4001))

    for i, user_id in enumerate(online_ids):
        await presence_service.mark_online(user_id, f"conn-{i}", "server-1")

    result = await presence_service.get_online_participants(online_ids + offline_ids)
    assert result == set(online_ids)


async def test_get_connections_returns_every_live_connection(redis_db):
    await presence_service.mark_online(1, "conn-a", "server-1")
    await presence_service.mark_online(1, "conn-b", "server-2")

    connections = await presence_service.get_connections(1)
    assert connections == {"conn-a", "conn-b"}


async def test_concurrent_connect_and_disconnect_from_two_devices(redis_db):
    # Simulates two devices for the same user connecting/heartbeating/
    # disconnecting at once - Redis set operations are atomic, so this
    # should never leave presence in an inconsistent state.
    async def connect_then_disconnect(connection_id: str):
        await presence_service.mark_online(1, connection_id, "server-1")
        await presence_service.heartbeat(1, connection_id)
        await presence_service.mark_offline(1, connection_id)

    await asyncio.gather(
        connect_then_disconnect("conn-a"),
        connect_then_disconnect("conn-b"),
    )

    assert await presence_service.is_online(1) is False
