import asyncio
import uuid

import pytest
from fastapi import WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession

from database.connection import session_scope
from database.crud.crud_message import get_chat_messages
from database.crud.crud_user import create_user
from routers.websocket import _dispatch, websocket_endpoint
from services import auth_service, chat_service, presence_service
from services.connection_manager import connection_manager
from services.fanout import worker as send_worker
from services.settings import service as settings_service

pytestmark = pytest.mark.asyncio


async def _send_and_drain(user_id, chat_id, ws, *, content="hi"):
    """
    Dispatch a send_message frame (async / queued now) then drain the send
    worker so the message is actually persisted. Returns its id as a string.
    """
    await _dispatch(
        user_id=user_id, connection_id="c1",
        payload={"type": "send_message", "chat_id": chat_id, "client_message_id": str(uuid.uuid4()), "content": content},
        websocket=ws,
    )
    async with session_scope() as session:
        await send_worker.drain_once(session, claim_stale=False)
        messages = await get_chat_messages(session, chat_id=chat_id, limit=50)
    return str(messages[0].id)

_DISCONNECT = object()


class FakeWebSocket:
    """
    A queue-backed fake so a test can drive a running websocket_endpoint()
    coroutine like a real client would - push messages while it's alive,
    inspect state in between, then signal disconnect.
    """

    def __init__(self):
        self._incoming: asyncio.Queue = asyncio.Queue()
        self.sent = []
        self.accepted = False
        self.closed_code = None

    async def accept(self):
        self.accepted = True

    async def close(self, code=None):
        self.closed_code = code

    async def send_json(self, data):
        self.sent.append(data)

    async def receive_json(self):
        item = await self._incoming.get()
        if item is _DISCONNECT:
            raise WebSocketDisconnect()
        return item

    async def push(self, message: dict):
        await self._incoming.put(message)

    async def push_disconnect(self):
        await self._incoming.put(_DISCONNECT)


async def _make_group(session: AsyncSession, owner_id: int, member_ids) -> int:
    await create_user(session, user_id=owner_id, phone_number=f"+97250{owner_id}")
    for member_id in member_ids:
        await create_user(session, user_id=member_id, phone_number=f"+97250{member_id}")
    group = await chat_service.create_group_chat(session, creator_id=owner_id, title="Test", initial_member_ids=list(member_ids))
    return group.id


# ---------------------------------------------------------------------------
# _dispatch: the per-message routing logic, tested directly
# ---------------------------------------------------------------------------

async def test_dispatch_heartbeat_acks(db_session: AsyncSession, redis_db):
    ws = FakeWebSocket()
    await _dispatch(user_id=1, connection_id="c1", payload={"type": "heartbeat"}, websocket=ws)

    assert ws.sent == [{"type": "heartbeat_ack"}]


async def test_dispatch_unknown_type_returns_an_error(db_session: AsyncSession, redis_db):
    ws = FakeWebSocket()
    await _dispatch(user_id=1, connection_id="c1", payload={"type": "not_a_real_type"}, websocket=ws)

    assert ws.sent[0]["type"] == "error"
    assert ws.sent[0]["code"] == "unknown_type"


async def test_dispatch_send_message_success(db_session: AsyncSession, redis_db):
    chat_id = await _make_group(db_session, 1, [2])
    ws = FakeWebSocket()

    await _dispatch(
        user_id=1,
        connection_id="c1",
        payload={"type": "send_message", "chat_id": chat_id, "client_message_id": str(uuid.uuid4()), "content": "hi"},
        websocket=ws,
    )

    assert ws.sent[0]["type"] == "ack"
    assert ws.sent[0]["for"] == "send_message"
    assert ws.sent[0]["status"] == "queued"

    # The message is persisted by the send worker, not on the request path.
    async with session_scope() as session:
        written = await send_worker.drain_once(session, claim_stale=False)
    assert written == 1


async def test_dispatch_send_message_missing_field_returns_bad_request(db_session: AsyncSession, redis_db):
    ws = FakeWebSocket()

    await _dispatch(
        user_id=1,
        connection_id="c1",
        payload={"type": "send_message", "client_message_id": str(uuid.uuid4())},  # no chat_id
        websocket=ws,
    )

    assert ws.sent[0] == {"type": "error", "code": "bad_request", "message": "Invalid request: 'chat_id'"}


async def test_dispatch_send_message_by_a_non_participant_returns_forbidden(db_session: AsyncSession, redis_db):
    chat_id = await _make_group(db_session, 1, [2])
    await create_user(db_session, user_id=3, phone_number="+972503")
    ws = FakeWebSocket()

    await _dispatch(
        user_id=3,
        connection_id="c1",
        payload={"type": "send_message", "chat_id": chat_id, "client_message_id": str(uuid.uuid4()), "content": "hi"},
        websocket=ws,
    )

    assert ws.sent[0]["type"] == "error"
    assert ws.sent[0]["code"] == "forbidden"


async def test_dispatch_send_message_is_rate_limited(db_session: AsyncSession, redis_db, monkeypatch):
    monkeypatch.setattr("routers.websocket.SEND_MESSAGE_RATE_LIMIT_MAX", 2)

    chat_id = await _make_group(db_session, 1, [2])
    ws = FakeWebSocket()

    for _ in range(2):
        await _dispatch(
            user_id=1, connection_id="c1",
            payload={"type": "send_message", "chat_id": chat_id, "client_message_id": str(uuid.uuid4()), "content": "hi"},
            websocket=ws,
        )
    ws.sent.clear()

    await _dispatch(
        user_id=1, connection_id="c1",
        payload={"type": "send_message", "chat_id": chat_id, "client_message_id": str(uuid.uuid4()), "content": "hi"},
        websocket=ws,
    )

    assert ws.sent == [{"type": "error", "code": "rate_limited", "client_message_id": ws.sent[0]["client_message_id"]}]


async def test_dispatch_edit_message_permission_denied(db_session: AsyncSession, redis_db):
    chat_id = await _make_group(db_session, 1, [2])
    ws = FakeWebSocket()
    message_id = await _send_and_drain(1, chat_id, ws)
    ws.sent.clear()

    await _dispatch(
        user_id=2, connection_id="c2",
        payload={"type": "edit_message", "chat_id": chat_id, "message_id": message_id, "content": "hijacked"},
        websocket=ws,
    )

    assert ws.sent[0]["type"] == "error"
    assert ws.sent[0]["code"] == "forbidden"


async def test_dispatch_delete_and_mark_read(db_session: AsyncSession, redis_db):
    chat_id = await _make_group(db_session, 1, [2])
    ws = FakeWebSocket()
    message_id = await _send_and_drain(1, chat_id, ws)
    ws.sent.clear()

    await _dispatch(user_id=2, connection_id="c2", payload={"type": "mark_read", "chat_id": chat_id, "message_id": message_id}, websocket=ws)
    assert ws.sent[-1] == {"type": "ack", "for": "mark_read"}

    await _dispatch(user_id=1, connection_id="c1", payload={"type": "delete_message", "chat_id": chat_id, "message_id": message_id}, websocket=ws)
    assert ws.sent[-1] == {"type": "ack", "for": "delete_message", "deleted": True}


async def test_dispatch_typing_publishes_event_for_a_participant(db_session: AsyncSession, redis_db, monkeypatch):
    chat_id = await _make_group(db_session, 1, [2])
    published = []

    async def capture(cid, event):
        published.append((cid, event))

    monkeypatch.setattr("routers.websocket.realtime_service.publish_event", capture)
    ws = FakeWebSocket()

    await _dispatch(user_id=1, connection_id="c1", payload={"type": "recording", "chat_id": chat_id}, websocket=ws)

    assert ws.sent == []  # ephemeral, no ack
    assert published == [(chat_id, {"event": "typing", "kind": "recording_audio", "chat_id": str(chat_id), "user_id": "1"})]


async def test_dispatch_typing_by_a_non_participant_returns_forbidden(db_session: AsyncSession, redis_db):
    chat_id = await _make_group(db_session, 1, [2])
    await create_user(db_session, user_id=3, phone_number="+972503")
    ws = FakeWebSocket()

    await _dispatch(user_id=3, connection_id="c1", payload={"type": "typing", "chat_id": chat_id}, websocket=ws)

    assert ws.sent[0]["type"] == "error"
    assert ws.sent[0]["code"] == "forbidden"


async def test_dispatch_typing_suppressed_when_sender_hides_online_status(db_session: AsyncSession, redis_db, monkeypatch):
    # 1:1: user 1 hides their online status (nobody), so their typing indicator
    # must not reach user 2 either - it follows the sender's own privacy.online.
    chat_id = await _make_group(db_session, 1, [2])  # 2 participants -> treated as 1:1
    await settings_service.update_user_settings(db_session, 1, {"privacy": {"online": "nobody"}})

    published = []

    async def capture(cid, event):
        published.append((cid, event))

    monkeypatch.setattr("routers.websocket.realtime_service.publish_event", capture)
    ws = FakeWebSocket()

    await _dispatch(user_id=1, connection_id="c1", payload={"type": "typing", "chat_id": chat_id}, websocket=ws)

    assert ws.sent == []  # silently dropped, no error
    assert published == []


async def test_dispatch_typing_allowed_in_group_regardless_of_privacy(db_session: AsyncSession, redis_db, monkeypatch):
    chat_id = await _make_group(db_session, 1, [2, 3])  # 3 participants -> group, never gated
    await settings_service.update_user_settings(db_session, 1, {"privacy": {"online": "nobody"}})

    published = []

    async def capture(cid, event):
        published.append((cid, event))

    monkeypatch.setattr("routers.websocket.realtime_service.publish_event", capture)
    ws = FakeWebSocket()

    await _dispatch(user_id=1, connection_id="c1", payload={"type": "typing", "chat_id": chat_id}, websocket=ws)

    assert published == [(chat_id, {"event": "typing", "kind": "typing", "chat_id": str(chat_id), "user_id": "1"})]


async def test_dispatch_internal_error_is_reported_not_raised(db_session: AsyncSession, redis_db, monkeypatch):
    # A failed enqueue must surface a synchronous error to the sender - the
    # message is otherwise silently lost while the client thinks it sent.
    chat_id = await _make_group(db_session, 1, [2])

    async def boom(*args, **kwargs):
        raise RuntimeError("stream unreachable")

    monkeypatch.setattr("routers.websocket.send_queue.enqueue_outgoing_message", boom)
    ws = FakeWebSocket()

    await _dispatch(
        user_id=1, connection_id="c1",
        payload={"type": "send_message", "chat_id": chat_id, "client_message_id": "x", "content": "hi"},
        websocket=ws,
    )

    assert ws.sent[0]["type"] == "error"
    assert ws.sent[0]["code"] == "internal_error"
    assert ws.sent[0]["client_message_id"] == "x"
    # The real exception text must never reach the client - it's exactly the
    # kind of thing (a DB error, an internal stack detail) that shouldn't be
    # exposed; it still goes to the server log via logger.exception().
    assert "stream unreachable" not in str(ws.sent[0])


# ---------------------------------------------------------------------------
# websocket_endpoint: the full handshake + receive loop, driven live
# ---------------------------------------------------------------------------

async def test_endpoint_rejects_an_invalid_token(redis_db):
    ws = FakeWebSocket()

    await websocket_endpoint(ws, token="not-a-real-token")

    assert ws.accepted is False
    assert ws.closed_code == 4401


async def test_endpoint_full_session_lifecycle(session_factory, redis_db):
    async with session_factory() as setup:
        chat_id = await _make_group(setup, 1, [2])
    token = auth_service._create_access_token(user_id=1)

    ws = FakeWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws, token=token))
    await asyncio.sleep(0.2)

    assert ws.accepted is True
    assert 1 in connection_manager.get_local_user_ids()
    assert await presence_service.is_online(1) is True

    client_message_id = str(uuid.uuid4())
    await ws.push({"type": "send_message", "chat_id": chat_id, "client_message_id": client_message_id, "content": "hello"})
    await asyncio.sleep(0.2)

    ack = next(m for m in ws.sent if m.get("for") == "send_message")
    assert ack["client_message_id"] == client_message_id
    assert ack["status"] == "queued"

    await ws.push_disconnect()
    await asyncio.wait_for(task, timeout=2.0)

    assert 1 not in connection_manager.get_local_user_ids()
    assert await presence_service.is_online(1) is False

    # No send worker runs in the test process - drain the queue by hand, then
    # confirm the message landed.
    async with session_factory() as verify_session:
        await send_worker.drain_once(verify_session, claim_stale=False)
    async with session_factory() as verify_session:
        messages = await get_chat_messages(verify_session, chat_id=chat_id, limit=50)
    assert any(m.content == "hello" for m in messages)


async def test_endpoint_subscribes_to_every_chat_the_user_is_in(session_factory, redis_db):
    async with session_factory() as setup:
        chat_a = await _make_group(setup, 1, [2])
        chat_b = await chat_service.get_or_create_private_chat(setup, 1, 2)
        chat_b_id = chat_b.id

    token = auth_service._create_access_token(user_id=1)
    ws = FakeWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws, token=token))
    await asyncio.sleep(0.2)

    assert chat_a in connection_manager._chat_subscribers
    assert chat_b_id in connection_manager._chat_subscribers

    await ws.push_disconnect()
    await asyncio.wait_for(task, timeout=2.0)


# ---------------------------------------------------------------------------
# subscribe_presence: gated by the target user's privacy.online setting
# ---------------------------------------------------------------------------

async def _two_users_with_private_chat(session: AsyncSession):
    await create_user(session, user_id=1, phone_number="+972501")
    await create_user(session, user_id=2, phone_number="+972502")
    await chat_service.get_or_create_private_chat(session, 1, 2)


async def _two_users_no_chat(session: AsyncSession):
    await create_user(session, user_id=1, phone_number="+972501")
    await create_user(session, user_id=2, phone_number="+972502")


async def test_subscribe_presence_default_everyone_allowed_without_a_chat(db_session: AsyncSession, redis_db):
    await _two_users_no_chat(db_session)
    ws = FakeWebSocket()
    try:
        await _dispatch(user_id=1, connection_id="c1",
                        payload={"type": "subscribe_presence", "user_id": "2"}, websocket=ws)
        assert ws.sent[0]["type"] == "presence_status"
        assert ws.sent[0]["user_id"] == "2"
    finally:
        await connection_manager.unsubscribe_presence("c1", 2)


async def test_subscribe_presence_contacts_requires_a_private_chat(db_session: AsyncSession, redis_db):
    await _two_users_no_chat(db_session)
    await settings_service.update_user_settings(db_session, 2, {"privacy": {"online": "contacts"}})
    ws = FakeWebSocket()

    await _dispatch(user_id=1, connection_id="c1",
                    payload={"type": "subscribe_presence", "user_id": "2"}, websocket=ws)

    assert ws.sent[0]["type"] == "presence_revoked"
    assert ws.sent[0]["user_id"] == "2"


async def test_subscribe_presence_contacts_allowed_with_a_private_chat(db_session: AsyncSession, redis_db):
    await _two_users_with_private_chat(db_session)
    await settings_service.update_user_settings(db_session, 2, {"privacy": {"online": "contacts"}})
    ws = FakeWebSocket()
    try:
        await _dispatch(user_id=1, connection_id="c1",
                        payload={"type": "subscribe_presence", "user_id": "2"}, websocket=ws)
        assert ws.sent[0]["type"] == "presence_status"
    finally:
        await connection_manager.unsubscribe_presence("c1", 2)


async def test_subscribe_presence_nobody_always_rejected(db_session: AsyncSession, redis_db):
    await _two_users_with_private_chat(db_session)
    await settings_service.update_user_settings(db_session, 2, {"privacy": {"online": "nobody"}})
    ws = FakeWebSocket()

    await _dispatch(user_id=1, connection_id="c1",
                    payload={"type": "subscribe_presence", "user_id": "2"}, websocket=ws)

    assert ws.sent[0]["type"] == "presence_revoked"


async def test_periodic_resubscribe_revokes_when_privacy_tightens(db_session: AsyncSession, redis_db):
    """The heartbeat re-sends subscribe_presence; a since-changed setting is
    re-checked and the watcher is torn down + told via presence_revoked."""
    await _two_users_with_private_chat(db_session)
    ws = FakeWebSocket()
    try:
        await _dispatch(user_id=1, connection_id="c1",
                        payload={"type": "subscribe_presence", "user_id": "2"}, websocket=ws)
        assert ws.sent[-1]["type"] == "presence_status"
        assert 2 in connection_manager._presence_subscribers

        await settings_service.update_user_settings(db_session, 2, {"privacy": {"online": "nobody"}})

        await _dispatch(user_id=1, connection_id="c1",
                        payload={"type": "subscribe_presence", "user_id": "2"}, websocket=ws)
        assert ws.sent[-1]["type"] == "presence_revoked"
        assert 2 not in connection_manager._presence_subscribers
    finally:
        await connection_manager.unsubscribe_presence("c1", 2)


async def test_subscribe_to_own_presence_still_rejected(db_session: AsyncSession, redis_db):
    await create_user(db_session, user_id=1, phone_number="+972501")
    ws = FakeWebSocket()

    await _dispatch(user_id=1, connection_id="c1",
                    payload={"type": "subscribe_presence", "user_id": "1"}, websocket=ws)

    assert ws.sent[0]["type"] == "error"
    assert ws.sent[0]["code"] == "bad_request"


async def test_endpoint_never_crashes_the_session_on_a_bad_message(session_factory, redis_db):
    async with session_factory() as setup:
        chat_id = await _make_group(setup, 1, [2])
    token = auth_service._create_access_token(user_id=1)

    ws = FakeWebSocket()
    task = asyncio.create_task(websocket_endpoint(ws, token=token))
    await asyncio.sleep(0.2)

    await ws.push({"type": "send_message"})  # missing chat_id/client_message_id
    await asyncio.sleep(0.2)
    assert ws.sent[-1]["code"] == "bad_request"

    # The session must still be alive and able to handle a well-formed message afterwards
    await ws.push({"type": "heartbeat"})
    await asyncio.sleep(0.2)
    assert ws.sent[-1] == {"type": "heartbeat_ack"}

    await ws.push_disconnect()
    await asyncio.wait_for(task, timeout=2.0)
