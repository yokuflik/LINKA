"""HTTP + SSE surface for message search (ADR 0040).

  GET /chats/{chat_id}/messages/search
  GET /chats/{chat_id}/messages/around/{message_id}
  GET /search/messages
  GET /search/messages/stream   (text/event-stream)

Covers auth (403), the 422 min-length gate, the 429 rate limit, cursor paging
over HTTP, the SSE frames, and the one-stream-per-user 409.
"""
import json

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession

import main as main_module
from modules.auth import service as auth_service
from modules.chats.crud.crud_chat import create_chat
from modules.chats.crud.crud_participant import add_participant_to_chat
from modules.messaging.crud import create_message
from modules.search.limits import SearchLimits
from modules.search.router import get_search_limits
from infra.ids.snowflake import next_id

pytestmark = pytest.mark.asyncio

_MID_BASE = next_id() & ~0x3FFFFF


def _mid(n: int) -> int:
    return _MID_BASE + n


@pytest_asyncio.fixture(autouse=True)
async def _clear_overrides():
    yield
    main_module.app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=main_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _login(client, redis_db, phone: str):
    resp = await client.post("/auth/otp/request", json={"phone_number": phone})
    assert resp.status_code == 204
    code = await redis_db.get(auth_service._otp_key(phone))
    resp = await client.post("/auth/otp/verify", json={"phone_number": phone, "code": code})
    assert resp.status_code == 200
    body = resp.json()
    return body["user"], body["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _seed_chat(db_session: AsyncSession, chat_id: int, member_ids, messages):
    await create_chat(db_session, chat_id=chat_id, is_group=True, title=f"c{chat_id}")
    for uid in member_ids:
        await add_participant_to_chat(db_session, chat_id=chat_id, user_id=uid)
    for mid, sender, content in messages:
        await create_message(db_session, message_id=mid, chat_id=chat_id, sender_id=sender, content=content)


async def test_in_chat_search_round_trip_and_cursor(client, db_session: AsyncSession, redis_db):
    user, token = await _login(client, redis_db, "+972500300001")
    uid = int(user["id"])
    await _seed_chat(
        db_session, 5001, [uid],
        [(_mid(i), uid, f"searchable widget number {i}") for i in range(3)],
    )

    resp = await client.get(f"/chats/5001/messages/search?q=widget&limit=2", headers=_auth(token))
    assert resp.status_code == 200
    body = resp.json()
    assert [r["id"] for r in body["results"]] == [str(_mid(2)), str(_mid(1))]
    assert body["has_more"] is True

    resp = await client.get(
        f"/chats/5001/messages/search?q=widget&limit=2&cursor={body['next_cursor']}", headers=_auth(token)
    )
    assert [r["id"] for r in resp.json()["results"]] == [str(_mid(0))]
    assert resp.json()["has_more"] is False


async def test_search_a_chat_you_are_not_in_is_403(client, db_session: AsyncSession, redis_db):
    owner, owner_token = await _login(client, redis_db, "+972500300010")
    outsider, outsider_token = await _login(client, redis_db, "+972500300011")
    await _seed_chat(db_session, 5002, [int(owner["id"])], [(_mid(10), int(owner["id"]), "private stuff")])

    resp = await client.get("/chats/5002/messages/search?q=private", headers=_auth(outsider_token))
    assert resp.status_code == 403


async def test_short_query_is_422_and_does_not_need_membership(client, db_session: AsyncSession, redis_db):
    _, token = await _login(client, redis_db, "+972500300020")
    resp = await client.get("/chats/999999/messages/search?q=a", headers=_auth(token))
    assert resp.status_code == 422


async def test_rate_limit_returns_429(client, db_session: AsyncSession, redis_db):
    user, token = await _login(client, redis_db, "+972500300030")
    await _seed_chat(db_session, 5003, [int(user["id"])], [(_mid(30), int(user["id"]), "rate limited soon")])
    main_module.app.dependency_overrides[get_search_limits] = lambda: SearchLimits(
        query_rate_max=1, query_rate_window_s=60, query_burst_max=99, query_burst_window_s=60
    )

    first = await client.get("/chats/5003/messages/search?q=rate", headers=_auth(token))
    second = await client.get("/chats/5003/messages/search?q=rate", headers=_auth(token))
    assert first.status_code == 200
    assert second.status_code == 429


async def test_global_search_spans_member_chats_only(client, db_session: AsyncSession, redis_db):
    a, a_token = await _login(client, redis_db, "+972500300040")
    b, _ = await _login(client, redis_db, "+972500300041")
    aid, bid = int(a["id"]), int(b["id"])
    await _seed_chat(db_session, 5004, [aid, bid], [(_mid(40), bid, "shared pineapple note")])
    await _seed_chat(db_session, 5005, [bid], [(_mid(41), bid, "solo pineapple note")])

    resp = await client.get("/search/messages?q=pineapple", headers=_auth(a_token))
    assert resp.status_code == 200
    assert [r["chat_id"] for r in resp.json()["results"]] == ["5004"]


async def test_messages_around_context_window(client, db_session: AsyncSession, redis_db):
    user, token = await _login(client, redis_db, "+972500300050")
    uid = int(user["id"])
    await _seed_chat(db_session, 5006, [uid], [(_mid(50 + i), uid, f"line {i}") for i in range(7)])

    resp = await client.get(f"/chats/5006/messages/around/{_mid(53)}?radius=1", headers=_auth(token))
    assert resp.status_code == 200
    assert [m["id"] for m in resp.json()] == [str(_mid(52)), str(_mid(53)), str(_mid(54))]


async def test_sse_stream_emits_match_then_done(client, db_session: AsyncSession, redis_db):
    user, token = await _login(client, redis_db, "+972500300060")
    uid = int(user["id"])
    await _seed_chat(db_session, 5007, [uid], [(_mid(60 + i), uid, f"streamed mango {i}") for i in range(3)])

    events = []
    async with client.stream("GET", "/search/messages/stream?q=mango", headers=_auth(token)) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        async for line in resp.aiter_lines():
            if line.startswith("event:"):
                events.append(line.split(":", 1)[1].strip())
    assert events == ["match", "match", "match", "done"]


async def test_only_one_stream_per_user(client, db_session: AsyncSession, redis_db):
    user, token = await _login(client, redis_db, "+972500300070")
    await redis_db.set(f"search:stream:active:{user['id']}", "1")
    resp = await client.get("/search/messages/stream?q=anything", headers=_auth(token))
    assert resp.status_code == 409
