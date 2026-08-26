import asyncio

import pytest

from services import realtime_service
from services.connection_manager import ConnectionManager

pytestmark = pytest.mark.asyncio


class FakeWebSocket:
    def __init__(self, fail_on_send: bool = False):
        self.sent = []
        self.fail_on_send = fail_on_send

    async def send_json(self, data):
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

    assert ws.sent == [{"event": "new_message", "message_id": 1}]

    await manager.disconnect("c1")


async def test_broadcast_does_not_leak_to_a_connection_subscribed_to_a_different_chat(manager, redis_db):
    ws_a = FakeWebSocket()
    ws_b = FakeWebSocket()
    await manager.connect(user_id=1, connection_id="c1", websocket=ws_a, chat_ids=[100])
    await manager.connect(user_id=2, connection_id="c2", websocket=ws_b, chat_ids=[200])
    await asyncio.sleep(0.2)

    await realtime_service.publish_event(100, {"event": "new_message", "message_id": 1})
    await asyncio.sleep(0.2)

    assert ws_a.sent == [{"event": "new_message", "message_id": 1}]
    assert ws_b.sent == []

    await manager.disconnect("c1")
    await manager.disconnect("c2")


async def test_two_connections_in_the_same_chat_share_one_listener_task(manager, redis_db):
    await manager.connect(user_id=1, connection_id="c1", websocket=FakeWebSocket(), chat_ids=[100])
    await manager.connect(user_id=2, connection_id="c2", websocket=FakeWebSocket(), chat_ids=[100])

    assert len(manager._chat_listener_tasks) == 1
    assert manager._chat_subscribers[100] == {"c1", "c2"}

    await manager.disconnect("c1")
    await manager.disconnect("c2")


async def test_last_disconnect_from_a_chat_removes_its_subscription_bookkeeping(manager, redis_db):
    await manager.connect(user_id=1, connection_id="c1", websocket=FakeWebSocket(), chat_ids=[100])
    await manager.connect(user_id=2, connection_id="c2", websocket=FakeWebSocket(), chat_ids=[100])

    await manager.disconnect("c1")
    assert 100 in manager._chat_listener_tasks, "one connection remains, the subscription must stay alive"

    await manager.disconnect("c2")
    assert 100 not in manager._chat_listener_tasks
    assert 100 not in manager._chat_subscribers


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

    assert ws2.sent == [{"event": "new_message", "message_id": 2}]
    await manager.disconnect("c2")


async def test_a_dead_connection_is_dropped_without_blocking_delivery_to_others(manager, redis_db):
    dead_ws = FakeWebSocket(fail_on_send=True)
    healthy_ws = FakeWebSocket()
    await manager.connect(user_id=1, connection_id="dead", websocket=dead_ws, chat_ids=[100])
    await manager.connect(user_id=2, connection_id="healthy", websocket=healthy_ws, chat_ids=[100])
    await asyncio.sleep(0.2)

    await realtime_service.publish_event(100, {"event": "new_message", "message_id": 1})
    await asyncio.sleep(0.2)

    assert healthy_ws.sent == [{"event": "new_message", "message_id": 1}]
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
    assert len(manager._chat_listener_tasks) == 1

    await asyncio.gather(*[manager.disconnect(cid) for cid in sockets])

    assert manager.get_local_user_ids() == set()
    assert 100 not in manager._chat_subscribers
    assert 100 not in manager._chat_listener_tasks
