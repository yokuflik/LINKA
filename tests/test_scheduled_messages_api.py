"""
REST surface for scheduled messages (ADR 0031):
  POST   /chats/{chat_id}/scheduled-messages
  GET    /scheduled-messages
  PATCH  /scheduled-messages/{id}
  DELETE /scheduled-messages/{id}

Covers auth (403 for a non-sender editing / a non-participant scheduling),
404, the lead-time / limit validation surfaced as HTTP, and the rate limit.
"""
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession

import main as main_module
from modules.auth import service as auth_service
from modules.messaging import router as messaging_router

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _login(client, redis_db, phone_number: str):
    resp = await client.post("/auth/otp/request", json={"phone_number": phone_number})
    assert resp.status_code == 204
    code = await redis_db.get(auth_service._otp_key(phone_number))
    resp = await client.post("/auth/otp/verify", json={"phone_number": phone_number, "code": code})
    assert resp.status_code == 200
    body = resp.json()
    return body["user"], body["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _soon(seconds: int = 3600) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


async def _private_chat(client, token_a, user_b_id: int) -> int:
    resp = await client.post(
        "/chats/private", json={"other_user_id": user_b_id}, headers=_auth(token_a)
    )
    assert resp.status_code == 200
    return resp.json()["id"]


async def test_schedule_list_patch_delete_round_trip(client, db_session: AsyncSession, redis_db):
    user_a, token_a = await _login(client, redis_db, "+972500200001")
    user_b, _ = await _login(client, redis_db, "+972500200002")
    chat_id = await _private_chat(client, token_a, user_b["id"])

    resp = await client.post(
        f"/chats/{chat_id}/scheduled-messages",
        json={"client_message_id": str(uuid.uuid4()), "scheduled_for": _soon(), "content": "later"},
        headers=_auth(token_a),
    )
    assert resp.status_code == 201
    sched = resp.json()
    assert sched["status"] == 0
    assert sched["content"] == "later"

    resp = await client.get("/scheduled-messages", headers=_auth(token_a))
    assert resp.status_code == 200
    assert [s["id"] for s in resp.json()] == [sched["id"]]

    resp = await client.get(
        f"/scheduled-messages?chat_id={chat_id}", headers=_auth(token_a)
    )
    assert [s["id"] for s in resp.json()] == [sched["id"]]

    resp = await client.patch(
        f"/scheduled-messages/{sched['id']}",
        json={"content": "edited", "scheduled_for": _soon(7200)},
        headers=_auth(token_a),
    )
    assert resp.status_code == 200
    assert resp.json()["content"] == "edited"

    resp = await client.delete(f"/scheduled-messages/{sched['id']}", headers=_auth(token_a))
    assert resp.status_code == 204

    resp = await client.get("/scheduled-messages", headers=_auth(token_a))
    assert resp.json() == []


async def test_scheduling_in_a_chat_you_are_not_in_is_403(client, db_session: AsyncSession, redis_db):
    user_a, token_a = await _login(client, redis_db, "+972500200010")
    user_b, _ = await _login(client, redis_db, "+972500200011")
    outsider, outsider_token = await _login(client, redis_db, "+972500200012")
    chat_id = await _private_chat(client, token_a, user_b["id"])

    resp = await client.post(
        f"/chats/{chat_id}/scheduled-messages",
        json={"client_message_id": str(uuid.uuid4()), "scheduled_for": _soon(), "content": "hi"},
        headers=_auth(outsider_token),
    )
    assert resp.status_code == 403


async def test_a_non_sender_cannot_patch_or_delete_someone_elses_scheduled_message(
    client, db_session: AsyncSession, redis_db
):
    user_a, token_a = await _login(client, redis_db, "+972500200020")
    user_b, token_b = await _login(client, redis_db, "+972500200021")
    chat_id = await _private_chat(client, token_a, user_b["id"])

    resp = await client.post(
        f"/chats/{chat_id}/scheduled-messages",
        json={"client_message_id": str(uuid.uuid4()), "scheduled_for": _soon(), "content": "mine"},
        headers=_auth(token_a),
    )
    sched_id = resp.json()["id"]

    resp = await client.patch(
        f"/scheduled-messages/{sched_id}", json={"content": "hijacked"}, headers=_auth(token_b)
    )
    assert resp.status_code == 404

    resp = await client.delete(f"/scheduled-messages/{sched_id}", headers=_auth(token_b))
    assert resp.status_code == 404


async def test_patch_or_delete_of_an_unknown_id_is_404(client, db_session: AsyncSession, redis_db):
    _, token = await _login(client, redis_db, "+972500200030")

    resp = await client.patch(
        "/scheduled-messages/999999", json={"content": "x"}, headers=_auth(token)
    )
    assert resp.status_code == 404
    resp = await client.delete("/scheduled-messages/999999", headers=_auth(token))
    assert resp.status_code == 404


async def test_scheduling_too_soon_is_400(client, db_session: AsyncSession, redis_db):
    user_a, token_a = await _login(client, redis_db, "+972500200040")
    user_b, _ = await _login(client, redis_db, "+972500200041")
    chat_id = await _private_chat(client, token_a, user_b["id"])

    resp = await client.post(
        f"/chats/{chat_id}/scheduled-messages",
        json={"client_message_id": str(uuid.uuid4()), "scheduled_for": _soon(1), "content": "hi"},
        headers=_auth(token_a),
    )
    assert resp.status_code == 400


async def test_pending_limit_is_409(client, db_session: AsyncSession, redis_db, monkeypatch):
    from modules.messaging import scheduled_service
    monkeypatch.setattr(scheduled_service, "SCHEDULED_MAX_PENDING_PER_USER", 1)

    user_a, token_a = await _login(client, redis_db, "+972500200050")
    user_b, _ = await _login(client, redis_db, "+972500200051")
    chat_id = await _private_chat(client, token_a, user_b["id"])

    body = {"client_message_id": str(uuid.uuid4()), "scheduled_for": _soon(), "content": "1"}
    resp = await client.post(
        f"/chats/{chat_id}/scheduled-messages", json=body, headers=_auth(token_a)
    )
    assert resp.status_code == 201

    body["client_message_id"] = str(uuid.uuid4())
    resp = await client.post(
        f"/chats/{chat_id}/scheduled-messages", json=body, headers=_auth(token_a)
    )
    assert resp.status_code == 409


async def test_scheduled_write_is_rate_limited(client, db_session: AsyncSession, redis_db, monkeypatch):
    monkeypatch.setattr(messaging_router, "SCHEDULED_WRITE_RATE_MAX", 2)

    user_a, token_a = await _login(client, redis_db, "+972500200060")
    user_b, _ = await _login(client, redis_db, "+972500200061")
    chat_id = await _private_chat(client, token_a, user_b["id"])

    for _ in range(2):
        resp = await client.post(
            f"/chats/{chat_id}/scheduled-messages",
            json={"client_message_id": str(uuid.uuid4()), "scheduled_for": _soon(), "content": "x"},
            headers=_auth(token_a),
        )
        assert resp.status_code == 201

    resp = await client.post(
        f"/chats/{chat_id}/scheduled-messages",
        json={"client_message_id": str(uuid.uuid4()), "scheduled_for": _soon(), "content": "x"},
        headers=_auth(token_a),
    )
    assert resp.status_code == 429
