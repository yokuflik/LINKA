import asyncio
import logging

import pytest

from services import notification_service

pytestmark = pytest.mark.asyncio


async def test_send_push_with_no_registered_device_is_a_noop(redis_db, caplog):
    with caplog.at_level(logging.INFO):
        await notification_service.send_push(user_id=1, title="Hi", body="there")

    assert "No registered device" in caplog.text


async def test_register_and_send_push_delivers_to_the_token(redis_db, monkeypatch):
    delivered = []

    async def fake_deliver(fcm_token, title, body, data):
        delivered.append((fcm_token, title, body, data))

    monkeypatch.setattr(notification_service, "_deliver", fake_deliver)

    await notification_service.register_device_token(1, "token-a")
    await notification_service.send_push(1, title="Hi", body="there", data={"chat_id": "5"})

    assert delivered == [("token-a", "Hi", "there", {"chat_id": "5"})]


async def test_send_push_delivers_to_every_registered_device(redis_db, monkeypatch):
    delivered_tokens = []

    async def fake_deliver(fcm_token, title, body, data):
        delivered_tokens.append(fcm_token)

    monkeypatch.setattr(notification_service, "_deliver", fake_deliver)

    await notification_service.register_device_token(1, "token-a")
    await notification_service.register_device_token(1, "token-b")
    await notification_service.send_push(1, title="Hi", body="there")

    assert set(delivered_tokens) == {"token-a", "token-b"}


async def test_unregister_stops_delivery_to_that_device(redis_db, monkeypatch):
    delivered_tokens = []

    async def fake_deliver(fcm_token, title, body, data):
        delivered_tokens.append(fcm_token)

    monkeypatch.setattr(notification_service, "_deliver", fake_deliver)

    await notification_service.register_device_token(1, "token-a")
    await notification_service.register_device_token(1, "token-b")
    await notification_service.unregister_device_token(1, "token-a")
    await notification_service.send_push(1, title="Hi", body="there")

    assert delivered_tokens == ["token-b"]


async def test_send_push_defaults_data_to_an_empty_dict(redis_db, monkeypatch):
    received_data = {}

    async def fake_deliver(fcm_token, title, body, data):
        received_data.update(data)

    monkeypatch.setattr(notification_service, "_deliver", fake_deliver)

    await notification_service.register_device_token(1, "token-a")
    await notification_service.send_push(1, title="Hi", body="there")  # no data= passed

    assert received_data == {}


async def test_concurrent_registration_of_many_devices_for_one_user(redis_db):
    # A user logged in on many devices at once (or a burst of app restarts
    # all re-registering) shouldn't lose any token to a race on the set.
    await asyncio.gather(*[
        notification_service.register_device_token(1, f"token-{i}")
        for i in range(200)
    ])

    tokens = await redis_db.smembers(notification_service._device_tokens_key(1))
    assert len(tokens) == 200


async def test_broadcast_push_to_a_large_group_delivers_to_every_offline_member(redis_db, monkeypatch):
    delivered_users = []

    async def fake_deliver(fcm_token, title, body, data):
        delivered_users.append(fcm_token)

    monkeypatch.setattr(notification_service, "_deliver", fake_deliver)

    user_ids = list(range(1, 501))
    for user_id in user_ids:
        await notification_service.register_device_token(user_id, f"token-{user_id}")

    await asyncio.gather(*[
        notification_service.send_push(user_id, title="Group message", body="hi")
        for user_id in user_ids
    ])

    assert len(delivered_users) == 500
