import asyncio

import pytest

from services import realtime_service
from services.connection_manager import ConnectionManager
from services.fanout import routing

pytestmark = pytest.mark.asyncio


class FakeWebSocket:
    def __init__(self, fail_on_send: bool = False, delay: float = 0.0):
        self.sent = []
        self.fail_on_send = fail_on_send
        self.delay = delay

    async def send_json(self, data):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail_on_send:
            raise RuntimeError("simulated dead connection")
        self.sent.append(data)


@pytest.fixture
def manager():
    return ConnectionManager()


async def test_connect_registers_the_user_locally(manager, redis_db):
    ws = FakeWebSocket()
    await manager.connect(user_id=1, connection_id="c1", websocket=ws, chat_ids=[])

    assert manager.get_local_user_ids() == {1}


async def test_disconnect_removes_the_user_locally(manager, redis_db):
    ws = FakeWebSocket()
    await manager.connect(user_id=1, connection_id="c1", websocket=ws, chat_ids=[])
    await manager.disconnect("c1")

    assert manager.get_local_user_ids() == set()


async def test_multi_device_keeps_user_local_until_the_last_connection_leaves(manager, redis_db):
    await manager.connect(user_id=1, connection_id="c1", websocket=FakeWebSocket(), chat_ids=[])
    await manager.connect(user_id=1, connection_id="c2", websocket=FakeWebSocket(), chat_ids=[])

    await manager.disconnect("c1")
    assert manager.get_local_user_ids() == {1}, "second device is still connected"

    await manager.disconnect("c2")
    assert manager.get_local_user_ids() == set()


async def test_disconnecting_an_unknown_connection_is_a_noop(manager, redis_db):
    await manager.disconnect("never-registered")  # must not raise


async def test_broadcast_delivers_a_published_event_to_a_subscribed_connection(manager, redis_db):
    ws = FakeWebSocket()
    await manager.connect(user_id=1, connection_id="c1", websocket=ws, chat_ids=[100])
    await asyncio.sleep(0.2)  # let the background listener's subscribe() actually land

    await realtime_service.publish_event(100, {"event": "new_message", "message_id": 1})
    await asyncio.sleep(0.2)

    assert ws.sent == [{"event": "new_message", "message_id": 1, "chat_id": "100"}]

    await manager.disconnect("c1")


async def test_broadcast_does_not_leak_to_a_connection_subscribed_to_a_different_chat(manager, redis_db):
    ws_a = FakeWebSocket()
    ws_b = FakeWebSocket()
    await manager.connect(user_id=1, connection_id="c1", websocket=ws_a, chat_ids=[100])
    await manager.connect(user_id=2, connection_id="c2", websocket=ws_b, chat_ids=[200])
    await asyncio.sleep(0.2)

    await realtime_service.publish_event(100, {"event": "new_message", "message_id": 1})
    await asyncio.sleep(0.2)

    assert ws_a.sent == [{"event": "new_message", "message_id": 1, "chat_id": "100"}]
    assert ws_b.sent == []

    await manager.disconnect("c1")
    await manager.disconnect("c2")


async def test_two_connections_in_the_same_chat_share_one_instance_registration(manager, redis_db):
    await manager.connect(user_id=1, connection_id="c1", websocket=FakeWebSocket(), chat_ids=[100])
    await manager.connect(user_id=2, connection_id="c2", websocket=FakeWebSocket(), chat_ids=[100])

    # One process-wide inbox listener, not one task per chat.
    assert manager._instance_inbox_task is not None
    assert manager._chat_subscribers[100] == {"c1", "c2"}
    assert await routing.instances_for_chat(100)  # this process is registered

    await manager.disconnect("c1")
    await manager.disconnect("c2")


async def test_last_disconnect_from_a_chat_removes_its_subscription_bookkeeping(manager, redis_db):
    await manager.connect(user_id=1, connection_id="c1", websocket=FakeWebSocket(), chat_ids=[100])
    await manager.connect(user_id=2, connection_id="c2", websocket=FakeWebSocket(), chat_ids=[100])

    await manager.disconnect("c1")
    assert 100 in manager._chat_subscribers, "one connection remains, the chat is still served"

    await manager.disconnect("c2")
    assert 100 not in manager._chat_subscribers
    assert await routing.instances_for_chat(100) == set()


async def test_resubscribing_to_a_chat_after_everyone_left_still_works(manager, redis_db):
    # Guards against the teardown of the first listener task leaving any
    # shared state (e.g. a half-cancelled subscription) that would break a
    # fresh subscribe to the same chat afterwards.
    ws1 = FakeWebSocket()
    await manager.connect(user_id=1, connection_id="c1", websocket=ws1, chat_ids=[100])
    await asyncio.sleep(0.2)
    await manager.disconnect("c1")
    await asyncio.sleep(0.2)

    ws2 = FakeWebSocket()
    await manager.connect(user_id=2, connection_id="c2", websocket=ws2, chat_ids=[100])
    await asyncio.sleep(0.2)

    await realtime_service.publish_event(100, {"event": "new_message", "message_id": 2})
    await asyncio.sleep(0.2)

    assert ws2.sent == [{"event": "new_message", "message_id": 2, "chat_id": "100"}]
    await manager.disconnect("c2")


async def test_a_dead_connection_is_dropped_without_blocking_delivery_to_others(manager, redis_db):
    dead_ws = FakeWebSocket(fail_on_send=True)
    healthy_ws = FakeWebSocket()
    await manager.connect(user_id=1, connection_id="dead", websocket=dead_ws, chat_ids=[100])
    await manager.connect(user_id=2, connection_id="healthy", websocket=healthy_ws, chat_ids=[100])
    await asyncio.sleep(0.2)

    await realtime_service.publish_event(100, {"event": "new_message", "message_id": 1})
    await asyncio.sleep(0.2)

    assert healthy_ws.sent == [{"event": "new_message", "message_id": 1, "chat_id": "100"}]
    # The failed send must have triggered a full disconnect for that connection
    assert 1 not in manager.get_local_user_ids()
    assert 2 in manager.get_local_user_ids()

    await manager.disconnect("healthy")


async def test_many_connections_joining_and_leaving_the_same_chat_concurrently(manager, redis_db):
    # Scale/stress check: 50 connections racing to join and then all leave a
    # single busy chat at once must never corrupt the ref-counting or crash.
    sockets = {f"c{i}": FakeWebSocket() for i in range(50)}

    await asyncio.gather(*[
        manager.connect(user_id=i, connection_id=cid, websocket=ws, chat_ids=[100])
        for i, (cid, ws) in enumerate(sockets.items())
    ])
    assert manager._chat_subscribers[100] == set(sockets.keys())
    assert manager._instance_inbox_task is not None

    await asyncio.gather(*[manager.disconnect(cid) for cid in sockets])

    assert manager.get_local_user_ids() == set()
    assert 100 not in manager._chat_subscribers
    assert manager._instance_inbox_task is None


async def test_concurrent_disconnect_during_an_in_flight_broadcast_does_not_crash(manager, redis_db):
    # Regression test: _broadcast_to_chat used to iterate the live
    # _chat_subscribers set directly. A disconnect from a *different*
    # connection in the same chat, landing while a slow send is still being
    # awaited for another connection, mutated that same set mid-iteration -
    # confirmed separately to raise "RuntimeError: Set changed size during
    # iteration" with this exact interleaving. Broadcasting off a snapshot
    # (and sending concurrently) is what closes this.
    slow_ws = FakeWebSocket(delay=0.1)
    victim_ws = FakeWebSocket()
    await manager.connect(user_id=1, connection_id="slow", websocket=slow_ws, chat_ids=[100])
    await manager.connect(user_id=2, connection_id="victim", websocket=victim_ws, chat_ids=[100])
    await asyncio.sleep(0.2)

    async def disconnect_victim_mid_broadcast():
        await asyncio.sleep(0.03)  # let the broadcast start and reach the slow send
        await manager.disconnect("victim")

    await asyncio.gather(
        realtime_service.publish_event(100, {"event": "new_message", "message_id": 1}),
        disconnect_victim_mid_broadcast(),
    )
    await asyncio.sleep(0.3)  # let the slow send (and the broadcast task) finish

    assert slow_ws.sent == [{"event": "new_message", "message_id": 1, "chat_id": "100"}]
    await manager.disconnect("slow")


async def test_one_slow_connection_does_not_delay_delivery_to_others(manager, redis_db):
    slow_ws = FakeWebSocket(delay=0.3)
    fast_ws = FakeWebSocket()
    await manager.connect(user_id=1, connection_id="slow", websocket=slow_ws, chat_ids=[100])
    await manager.connect(user_id=2, connection_id="fast", websocket=fast_ws, chat_ids=[100])
    await asyncio.sleep(0.2)

    await realtime_service.publish_event(100, {"event": "new_message", "message_id": 1})

    # The fast connection must receive its copy well before the slow one
    # finishes - sends are dispatched concurrently, not queued behind it.
    await asyncio.sleep(0.05)
    assert fast_ws.sent == [{"event": "new_message", "message_id": 1, "chat_id": "100"}]
    assert slow_ws.sent == [], "the slow send should still be in flight at this point"

    await asyncio.sleep(0.4)
    assert slow_ws.sent == [{"event": "new_message", "message_id": 1, "chat_id": "100"}]

    await manager.disconnect("slow")
    await manager.disconnect("fast")


async def test_listener_recovers_from_a_transient_failure_instead_of_dying(manager, redis_db, monkeypatch):
    # Regression test: _listen_to_chat used to only catch CancelledError, so
    # any other error (a dropped Redis connection, or any bug in
    # _broadcast_to_chat itself) permanently killed that chat's delivery -
    # the bookkeeping still showed subscribers, so no new listener task was
    # ever spawned to replace the dead one, even for a brand new connection.
    call_count = {"n": 0}
    original_dispatch = manager._dispatch_inbox_event

    async def flaky_dispatch(event):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise ConnectionError("simulated transient Redis blip")
        await original_dispatch(event)

    monkeypatch.setattr(manager, "_dispatch_inbox_event", flaky_dispatch)

    ws = FakeWebSocket()
    await manager.connect(user_id=1, connection_id="c1", websocket=ws, chat_ids=[100])
    await asyncio.sleep(0.2)

    # This publish drives the injected failure on its first delivery attempt
    await realtime_service.publish_event(100, {"event": "new_message", "message_id": 1})
    await asyncio.sleep(0.8)  # covers the listener's 0.5s backoff plus the resubscribe round trip

    assert manager._instance_inbox_task is not None, "the listener must still be registered, not dead"

    # A second event, after recovery, must still get delivered
    await realtime_service.publish_event(100, {"event": "new_message", "message_id": 2})
    await asyncio.sleep(0.2)

    assert ws.sent == [{"event": "new_message", "message_id": 2, "chat_id": "100"}]
    await manager.disconnect("c1")


# ---------------------------------------------------------------------------
# The per-user personal channel: what makes a chat created *after* connect()
# still reach an already-open connection, instead of only at the next
# reconnect (when get_all_chat_ids_for_user() would pick it up naturally).
# ---------------------------------------------------------------------------

async def test_added_to_chat_event_dynamically_subscribes_the_connection(manager, redis_db):
    ws = FakeWebSocket()
    # Connected with an empty chat_ids list - simulates the chat not having
    # existed yet at connect time.
    await manager.connect(user_id=1, connection_id="c1", websocket=ws, chat_ids=[])
    await asyncio.sleep(0.2)

    assert 200 not in manager._chat_subscribers

    await realtime_service.publish_user_event(1, {"event": "added_to_chat", "chat_id": "200"})
    await asyncio.sleep(0.2)

    # The connection is now live-subscribed to the new chat, with no reconnect needed
    assert "c1" in manager._chat_subscribers.get(200, set())

    # And a message published to that chat right after gets delivered
    await realtime_service.publish_event(200, {"event": "new_message", "message_id": 1})
    await asyncio.sleep(0.2)
    assert {"event": "added_to_chat", "chat_id": "200"} in ws.sent
    assert {"event": "new_message", "message_id": 1, "chat_id": "200"} in ws.sent

    await manager.disconnect("c1")


async def test_added_to_chat_only_affects_the_targeted_user(manager, redis_db):
    ws1 = FakeWebSocket()
    ws2 = FakeWebSocket()
    await manager.connect(user_id=1, connection_id="c1", websocket=ws1, chat_ids=[])
    await manager.connect(user_id=2, connection_id="c2", websocket=ws2, chat_ids=[])
    await asyncio.sleep(0.2)

    await realtime_service.publish_user_event(1, {"event": "added_to_chat", "chat_id": "300"})
    await asyncio.sleep(0.2)

    assert "c1" in manager._chat_subscribers.get(300, set())
    assert "c2" not in manager._chat_subscribers.get(300, set())
    assert ws2.sent == []

    await manager.disconnect("c1")
    await manager.disconnect("c2")


async def test_multi_device_both_connections_get_subscribed(manager, redis_db):
    ws_a = FakeWebSocket()
    ws_b = FakeWebSocket()
    await manager.connect(user_id=1, connection_id="device-a", websocket=ws_a, chat_ids=[])
    await manager.connect(user_id=1, connection_id="device-b", websocket=ws_b, chat_ids=[])
    await asyncio.sleep(0.2)

    await realtime_service.publish_user_event(1, {"event": "added_to_chat", "chat_id": "400"})
    await asyncio.sleep(0.2)

    assert manager._chat_subscribers.get(400) == {"device-a", "device-b"}
    assert {"event": "added_to_chat", "chat_id": "400"} in ws_a.sent
    assert {"event": "added_to_chat", "chat_id": "400"} in ws_b.sent

    await manager.disconnect("device-a")
    await manager.disconnect("device-b")


async def test_user_channel_listener_stops_when_the_last_connection_disconnects(manager, redis_db):
    ws = FakeWebSocket()
    await manager.connect(user_id=1, connection_id="c1", websocket=ws, chat_ids=[])
    await asyncio.sleep(0.2)
    assert 1 in manager._user_channel_listener_tasks

    await manager.disconnect("c1")
    assert 1 not in manager._user_channel_listener_tasks
    assert 1 not in manager._user_channel_subscribers
